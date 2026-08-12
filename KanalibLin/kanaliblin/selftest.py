"""
Offline self-test of the framing layer: ``python -m kanaliblin.selftest``.

No hardware and no pyserial needed. It checks the pieces that must agree with
the firmware byte for byte -- CRC, escaping, the decoder's tolerance to dirty
streams, and the LIN PID/checksum arithmetic. If this fails, nothing that
talks to a device can be trusted.
"""

from __future__ import annotations

import sys

from .framing import (
    StreamDecoder,
    crc8_sae_j1850,
    encode,
    lin_checksum,
    lin_pid,
)
from .events import HeaderOnly, LinFrame, Status, UartRegisters
from .protocol import Checksum, Command, Event, default_checksum
from .timestamps import TIMESTAMP_MODULUS, TimestampUnwrapper, delta_us


def _check(condition: bool, label: str, failures: list[str]) -> None:
    if condition:
        print(f"  OK    {label}")
    else:
        print(f"  FAIL  {label}")
        failures.append(label)


def main() -> int:
    failures: list[str] = []
    print("KanaLin framing self-test (no hardware needed)")

    # --- CRC and LIN arithmetic, against published check values -------------
    _check(crc8_sae_j1850(b"123456789") == 0x4B, "CRC-8/SAE-J1850 check value 0x4B", failures)
    _check(lin_pid(0x00) == 0x80, "PID of ID 0x00 is 0x80", failures)
    _check(lin_pid(0x3C) == 0x3C, "PID of the diagnostic ID 0x3C is 0x3C", failures)
    _check(lin_pid(0x10) == 0x50, "PID of ID 0x10 is 0x50", failures)
    _check(
        lin_checksum(0x00, b"\x00" * 8, Checksum.CLASSIC) == 0xFF,
        "classic checksum of eight zero bytes is 0xFF",
        failures,
    )
    _check(
        lin_checksum(0x3C, b"\x01\x02", Checksum.ENHANCED)
        != lin_checksum(0x3C, b"\x01\x02", Checksum.CLASSIC),
        "enhanced and classic checksums differ (the PID is folded in)",
        failures,
    )

    # --- encode/decode round trip, including bytes that need escaping -------
    dec = StreamDecoder()
    payloads = [
        b"",
        b"\x00",
        bytes([0x7E]),
        bytes([0x7D]),
        bytes([0x7E, 0x7D] * 8),
        bytes(range(128)),
    ]
    round_trip_ok = True
    for payload in payloads:
        frames = dec.feed(encode(Command.LOOPBACK, payload))
        if len(frames) != 1 or frames[0].cmd != Command.LOOPBACK or frames[0].payload != payload:
            round_trip_ok = False
    _check(round_trip_ok, f"round trip of {len(payloads)} payloads (0..128 B, with escapes)", failures)

    # --- the decoder must survive a dirty stream ----------------------------
    dec = StreamDecoder()
    good = encode(Command.GET_STATUS)
    corrupt = bytearray(encode(Command.SET_MODE, b"\x01"))
    corrupt[-2] ^= 0xFF  # break the CRC
    stream = b"\xaa\xbb garbage" + good + bytes(corrupt) + good
    frames = dec.feed(stream)
    _check(
        len(frames) == 2 and all(f.cmd == Command.GET_STATUS for f in frames),
        "leading garbage ignored, bad CRC dropped, good frames kept",
        failures,
    )

    dec = StreamDecoder()
    blob = encode(Command.LOOPBACK, b"split me")
    frames = dec.feed(blob[:4]) + dec.feed(blob[4:])
    _check(len(frames) == 1 and frames[0].payload == b"split me", "frame split across chunks", failures)

    # --- event parsing ------------------------------------------------------
    frame = LinFrame.from_payload(b"\x10\x02\xaa\xbb\x01\x40\xe2\x01\x00")
    _check(
        frame.frame_id == 0x10
        and frame.data == b"\xaa\xbb"
        and frame.checksum_ok
        and frame.timestamp_us == 123456,
        "FRAME_RX payload decoded (id, data, checksum flag, timestamp)",
        failures,
    )
    _check(HeaderOnly.from_payload(b"\x21").frame_id == 0x21, "HEADER_ONLY payload decoded", failures)
    status = Status.from_payload(b"\x01\x01\x03")
    _check(
        status.mode == 1 and status.bus_state == 1 and int(status.errors) == 3,
        "STATUS payload decoded",
        failures,
    )
    regs = UartRegisters.from_payload(
        (144_000_000).to_bytes(4, "little")
        + (0).to_bytes(4, "little")
        + (7500).to_bytes(4, "little")
        + (0).to_bytes(4, "little")
        + (19200).to_bytes(4, "little")
    )
    _check(
        abs(regs.actual_baudrate - 19200) < 1 and abs(regs.bit_time_us - 52.08) < 0.1,
        "UART_REGS recomputes 19200 baud (52.1 us per bit) from BRR 7500",
        failures,
    )

    # --- per-identifier checksum default ------------------------------------
    _check(
        default_checksum(0x3C) == Checksum.CLASSIC
        and default_checksum(0x3D) == Checksum.CLASSIC
        and default_checksum(0x10) == Checksum.ENHANCED,
        "diagnostic identifiers default to the classic checksum, others to enhanced",
        failures,
    )

    # --- timestamp arithmetic ------------------------------------------------
    _check(
        delta_us(TIMESTAMP_MODULUS - 1000, 500) == 1500 and delta_us(100, 700) == 600,
        "delta_us() measures a gap correctly across a counter wrap",
        failures,
    )
    timeline = TimestampUnwrapper()
    stamps = [1_000, 2_000, TIMESTAMP_MODULUS - 500, 500]  # the third to fourth wraps
    unwrapped = [timeline.unwrap(s) for s in stamps]
    _check(
        all(b > a for a, b in zip(unwrapped, unwrapped[1:]))
        and unwrapped[0] == 0
        and unwrapped[3] - unwrapped[2] == 1000
        and timeline.wraps == 1,
        "TimestampUnwrapper keeps the timeline increasing across a wrap",
        failures,
    )

    # --- command/event numbering must match the firmware --------------------
    _check(
        (Command.SET_MODE, Command.MASTER_SEND_FRAME, Command.SCHEDULE_STOP) == (0x01, 0x11, 0x33)
        and (Command.SEND_GOTO_SLEEP, Command.SEND_WAKEUP) == (0x40, 0x41)
        and (Event.FRAME_RX, Event.ACK, Event.NACK) == (0x80, 0x8F, 0x8E),
        "command and event numbering is unchanged",
        failures,
    )

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
