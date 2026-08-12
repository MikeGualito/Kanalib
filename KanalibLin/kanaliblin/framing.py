"""
Byte framing and LIN checksum helpers.

Two different checksums live here and they must not be confused:

* :func:`crc8_sae_j1850` protects the *host link* frame (CMD + PAYLOAD), i.e.
  the bytes travelling over USB.
* :func:`lin_checksum` is the checksum of the *LIN bus* frame, computed over
  the data bytes (and the PID, if enhanced).

This module has no dependencies -- it is usable on its own to build or parse
KanaLin frames without opening a port.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .protocol import ESC, ESC_XOR, FLAG, Checksum

__all__ = [
    "crc8_sae_j1850",
    "lin_pid",
    "lin_checksum",
    "encode",
    "RawFrame",
    "StreamDecoder",
]


# --------------------------------------------------------------------------
# Host-link CRC: CRC-8/SAE-J1850 (poly 0x1D, init 0xFF, not reflected,
# xorout 0xFF). Check value: crc8_sae_j1850(b"123456789") == 0x4B.
# --------------------------------------------------------------------------
def crc8_sae_j1850(data: bytes) -> int:
    """Return the CRC-8/SAE-J1850 of ``data`` (used by the host link framing)."""
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc ^ 0xFF


# --------------------------------------------------------------------------
# LIN bus helpers
# --------------------------------------------------------------------------
def lin_pid(frame_id: int) -> int:
    """
    Return the protected identifier: the 6-bit ``frame_id`` plus two parity bits.

    P0 = ID0 ^ ID1 ^ ID2 ^ ID4, P1 = ~(ID1 ^ ID3 ^ ID4 ^ ID5).
    """
    frame_id &= 0x3F
    p0 = (frame_id ^ (frame_id >> 1) ^ (frame_id >> 2) ^ (frame_id >> 4)) & 1
    p1 = ~((frame_id >> 1) ^ (frame_id >> 3) ^ (frame_id >> 4) ^ (frame_id >> 5)) & 1
    return frame_id | (p0 << 6) | (p1 << 7)


def lin_checksum(frame_id: int, data: bytes, checksum: Checksum = Checksum.ENHANCED) -> int:
    """
    Return the LIN checksum of ``data``.

    The enhanced checksum (LIN 2.x) folds the PID in; the classic one does not.
    Diagnostic identifiers 0x3C and 0x3D always use the classic form.
    """
    total = lin_pid(frame_id) if checksum == Checksum.ENHANCED else 0
    for byte in data:
        total += byte
        if total > 0xFF:
            total -= 0xFF
    return (~total) & 0xFF


# --------------------------------------------------------------------------
# Encoder
# --------------------------------------------------------------------------
def _escape(raw: bytes) -> bytes:
    out = bytearray()
    for byte in raw:
        if byte in (FLAG, ESC):
            out += bytes((ESC, byte ^ ESC_XOR))
        else:
            out.append(byte)
    return bytes(out)


def encode(cmd: int, payload: bytes = b"") -> bytes:
    """
    Build one wire frame: ``0x7E LEN CMD PAYLOAD... CRC8 0x7E``.

    ``LEN`` counts ``CMD + PAYLOAD`` (not the CRC), and every ``0x7E``/``0x7D``
    between ``LEN`` and ``CRC8`` is escaped as ``0x7D`` followed by the byte
    XOR 0x20.
    """
    if len(payload) > 253:
        raise ValueError(f"payload too long for the wire format: {len(payload)} > 253")
    body = bytes((cmd,)) + payload
    unescaped = bytes((len(body),)) + body + bytes((crc8_sae_j1850(body),))
    return bytes((FLAG,)) + _escape(unescaped) + bytes((FLAG,))


# --------------------------------------------------------------------------
# Decoder
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class RawFrame:
    """One validated frame straight off the wire, before interpretation."""

    cmd: int
    payload: bytes

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"0x{self.cmd:02X}[{self.payload.hex(' ') or '-'}]"


class StreamDecoder:
    """
    Incremental decoder: feed it arbitrary chunks, get back complete frames.

    It tolerates garbage before the first flag, frames split across chunks and
    a flag shared between two back-to-back frames. Frames with a bad CRC, a
    truncated body or a lying LEN are dropped silently -- exactly like the
    firmware decoder, so both ends agree on what "a frame" is.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._in_frame = False
        self._escaping = False

    def reset(self) -> None:
        """Forget any partially received frame."""
        self._buf.clear()
        self._in_frame = False
        self._escaping = False

    def feed(self, chunk: bytes) -> list[RawFrame]:
        """Consume ``chunk`` and return every frame completed by it."""
        frames: list[RawFrame] = []
        for byte in chunk:
            if byte == FLAG:
                if self._in_frame and self._buf:
                    frame = self._finalize()
                    if frame is not None:
                        frames.append(frame)
                self._buf.clear()
                self._in_frame = True
                self._escaping = False
            elif not self._in_frame:
                continue  # garbage before the first flag
            elif self._escaping:
                self._buf.append(byte ^ ESC_XOR)
                self._escaping = False
            elif byte == ESC:
                self._escaping = True
            else:
                self._buf.append(byte)
        return frames

    def _finalize(self) -> RawFrame | None:
        if len(self._buf) < 3:  # LEN + CMD + CRC at the very least
            return None
        length = self._buf[0]
        if len(self._buf) < length + 2:  # body + CRC must both be there
            return None
        body = bytes(self._buf[1 : 1 + length])
        if crc8_sae_j1850(body) != self._buf[1 + length]:
            return None
        return RawFrame(body[0], body[1:])


# --------------------------------------------------------------------------
# Command builders -- handy when you want the bytes without a live device
# (batching several commands into a single write, unit tests, replay files).
# --------------------------------------------------------------------------
def build_set_mode(mode: int) -> bytes:
    from .protocol import Command

    return encode(Command.SET_MODE, bytes((mode,)))


def build_set_baudrate(baudrate: int) -> bytes:
    from .protocol import Command

    return encode(Command.SET_BAUDRATE, struct.pack("<I", baudrate))


def build_master_send_header(frame_id: int) -> bytes:
    from .protocol import Command

    return encode(Command.MASTER_SEND_HEADER, bytes((frame_id & 0x3F,)))


def build_master_send_frame(
    frame_id: int, data: bytes, checksum: Checksum = Checksum.ENHANCED
) -> bytes:
    from .protocol import Command

    payload = bytes((frame_id & 0x3F, len(data))) + bytes(data) + bytes((int(checksum),))
    return encode(Command.MASTER_SEND_FRAME, payload)


def build_slave_set_response(
    frame_id: int, data: bytes, checksum: Checksum = Checksum.ENHANCED
) -> bytes:
    from .protocol import Command

    payload = bytes((frame_id & 0x3F, len(data))) + bytes(data) + bytes((int(checksum),))
    return encode(Command.SLAVE_SET_RESPONSE, payload)


def build_slave_clear_response(frame_id: int) -> bytes:
    from .protocol import Command

    return encode(Command.SLAVE_CLEAR_RESPONSE, bytes((frame_id & 0x3F,)))


def build_schedule_load(entries: list[tuple[int, int]]) -> bytes:
    from .protocol import Command

    payload = bytearray((len(entries),))
    for frame_id, delay_ms in entries:
        payload += bytes((frame_id & 0x3F,)) + struct.pack("<H", delay_ms)
    return encode(Command.SCHEDULE_LOAD, bytes(payload))
