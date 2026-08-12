"""
Offline test of the device layer, against a simulated firmware.

Run:  python tests/test_offline.py        (no hardware, no pyserial needed)

A fake serial port implements enough of the firmware's behaviour -- ACK/NACK
per command, mode preconditions, the slave response table, the transceiver echo
that turns your own transmission into a received frame -- to exercise the parts
of :mod:`kanaliblin.device` that are easy to break: reply matching, event routing,
the reader thread and the local argument checks.

For the framing layer see ``python -m kanaliblin.selftest``.
"""

from __future__ import annotations

import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanaliblin import framing
from kanaliblin.protocol import (
    GOTO_SLEEP_DATA,
    MASTER_REQUEST_ID,
    Checksum,
    Command,
    Event,
    Mode,
)

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"  {'OK   ' if condition else 'FAIL '} {label}")
    if not condition:
        FAILURES.append(label)


class FakeSerial:
    """A stand-in for ``serial.Serial`` that answers like the KanaLin firmware."""

    def __init__(self, port: str, baudrate: int = 0, timeout: float = 0.02) -> None:
        self.port = port
        self.is_open = True
        self._out = bytearray()
        self._decoder = framing.StreamDecoder()
        self._lock = threading.Lock()
        self.mode = 0
        self.responses: dict[int, bytes] = {}
        self.schedule_running = False
        self.baudrate_lin = 19200
        self.timestamp = 1000
        self.last_checksum: dict[int, int] = {}  # frame_id -> checksum type sent

    # --- host -> device --------------------------------------------------
    def write(self, data: bytes) -> int:
        for frame in self._decoder.feed(data):
            self._handle(frame.cmd, frame.payload)
        return len(data)

    def flush(self) -> None:
        pass

    # --- device -> host --------------------------------------------------
    def read(self, size: int) -> bytes:
        time.sleep(0.005)  # stand in for the USB poll interval
        with self._lock:
            chunk, self._out = bytes(self._out[:size]), self._out[size:]
        return chunk

    def close(self) -> None:
        self.is_open = False

    # --- firmware behaviour ---------------------------------------------
    def _push(self, cmd: int, payload: bytes = b"") -> None:
        with self._lock:
            self._out += framing.encode(cmd, payload)

    def _ack(self, cmd: int, accepted: bool) -> None:
        self._push(Event.ACK if accepted else Event.NACK, bytes((cmd,)))

    def _echo_frame(self, frame_id: int, data: bytes) -> None:
        """The transceiver returns everything transmitted, so a frame comes back."""
        self.timestamp += 6848  # a full 8-byte frame at 19200 baud
        self._push(
            Event.FRAME_RX,
            bytes((frame_id, len(data))) + data + b"\x01" + struct.pack("<I", self.timestamp),
        )

    def _handle(self, cmd: int, payload: bytes) -> None:
        if cmd == Command.SET_MODE:
            accepted = len(payload) == 1 and payload[0] <= 2
            if accepted:
                self.mode = payload[0]
                self.schedule_running = False
            self._ack(cmd, accepted)

        elif cmd == Command.SET_BAUDRATE:
            rate = struct.unpack("<I", payload)[0] if len(payload) == 4 else 0
            accepted = 1000 <= rate <= 20000 and not self.schedule_running
            if accepted:
                self.baudrate_lin = rate
            self._ack(cmd, accepted)

        elif cmd == Command.MASTER_SEND_FRAME:
            frame_id, dlc = payload[0], payload[1]
            accepted = self.mode == Mode.MASTER and 1 <= dlc <= 8 and len(payload) == dlc + 3
            if accepted:
                self.last_checksum[frame_id] = payload[2 + dlc]
            self._ack(cmd, accepted)
            if accepted:
                self._echo_frame(frame_id, payload[2 : 2 + dlc])

        elif cmd == Command.MASTER_SEND_HEADER:
            accepted = self.mode == Mode.MASTER and len(payload) == 1
            self._ack(cmd, accepted)
            if accepted:
                frame_id = payload[0]
                if frame_id in self.responses:
                    self._echo_frame(frame_id, self.responses[frame_id])
                else:
                    self._push(Event.HEADER_ONLY, bytes((frame_id,)))

        elif cmd == Command.SLAVE_SET_RESPONSE:
            frame_id, dlc = payload[0], payload[1]
            accepted = 1 <= dlc <= 8 and len(payload) == dlc + 3
            if accepted:
                self.responses[frame_id] = payload[2 : 2 + dlc]
                self.last_checksum[frame_id] = payload[2 + dlc]
            self._ack(cmd, accepted)

        elif cmd == Command.SLAVE_CLEAR_RESPONSE:
            self.responses.pop(payload[0], None)
            self._ack(cmd, len(payload) == 1)

        elif cmd == Command.GET_STATUS:
            self._push(Event.STATUS, bytes((self.mode, 0, 0)))

        elif cmd == Command.SCHEDULE_LOAD:
            self._ack(cmd, not self.schedule_running and len(payload) == 1 + 3 * payload[0])

        elif cmd == Command.SCHEDULE_START:
            self.schedule_running = self.mode == Mode.MASTER
            self._ack(cmd, self.schedule_running)

        elif cmd == Command.SCHEDULE_STOP:
            self.schedule_running = False
            self._ack(cmd, True)

        elif cmd == Command.SEND_GOTO_SLEEP:
            # An ordinary frame: 0x3C with 00 FF..FF, echoed back like any other.
            accepted = self.mode == Mode.MASTER and len(payload) == 0
            self._ack(cmd, accepted)
            if accepted:
                self._echo_frame(MASTER_REQUEST_ID, GOTO_SLEEP_DATA)

        elif cmd == Command.SEND_WAKEUP:
            # No frame comes back: a wake-up pulse is not a frame.
            self._ack(cmd, self.mode != Mode.IDLE and not self.schedule_running)

        elif cmd == Command.LOOPBACK:
            self._push(Event.LOOPBACK, payload)

        elif cmd == Command.GET_UART_REGS:
            self._push(
                Event.UART_REGS,
                struct.pack("<5I", 144_000_000, 0, 144_000_000 // self.baudrate_lin, 0,
                            self.baudrate_lin),
            )
        else:
            self._ack(cmd, False)


class _FakeSerialModule:
    Serial = FakeSerial

    class SerialException(Exception):
        pass


def main() -> int:
    import kanaliblin.device as device_module

    device_module.serial = _FakeSerialModule  # type: ignore[assignment]

    from kanaliblin import (
        CommandRejected,
        HeaderOnly,
        KanaLin,
        LinFrame,
        SleepRequested,
        is_goto_sleep,
    )

    print("KanaLin device-layer test (simulated firmware)")
    events: list = []

    with KanaLin("FAKE1", on_event=events.append) as lin:
        check(lin.ping(), "ping() completes a loopback round trip")

        status = lin.reset()
        check(status.mode == Mode.IDLE, "reset() leaves the channel in IDLE")

        lin.mode = Mode.MASTER
        check(lin.mode == Mode.MASTER, "the mode property tracks set_mode()")

        frame = lin.master_send_frame(0x10, b"\x11\x22\x33\x44")
        check(
            isinstance(frame, LinFrame)
            and frame.frame_id == 0x10
            and frame.data == b"\x11\x22\x33\x44"
            and frame.checksum_ok,
            "master_send_frame() returns the frame observed on the bus",
        )

        answer = lin.master_send_header(0x11)
        check(
            isinstance(answer, HeaderOnly) and answer.frame_id == 0x11,
            "master_send_header() with no responder returns HeaderOnly",
        )

        lin.slave_set_response(0x11, b"\xde\xad\xbe\xef")
        answer = lin.master_send_header(0x11)
        check(
            isinstance(answer, LinFrame) and answer.data == b"\xde\xad\xbe\xef",
            "a registered response is returned as a LinFrame",
        )

        # --- bus sleep / wake-up ----------------------------------------
        sleep_seen: list = []
        lin.on_sleep_requested = sleep_seen.append

        frame = lin.send_goto_sleep()
        check(
            isinstance(frame, LinFrame)
            and frame.frame_id == MASTER_REQUEST_ID
            and frame.data == GOTO_SLEEP_DATA,
            "send_goto_sleep() puts 0x3C 00 FF..FF on the bus",
        )
        check(
            len(sleep_seen) == 1
            and isinstance(sleep_seen[0], SleepRequested)
            and sleep_seen[0].frame_id == MASTER_REQUEST_ID,
            "the go-to-sleep echo fires the on_sleep_requested callback",
        )
        check(
            any(isinstance(e, LinFrame) and e.frame_id == MASTER_REQUEST_ID for e in events),
            "the sleep frame is ALSO delivered as an ordinary bus event",
        )
        check(lin.stats.sleep_requests == 1, "stats counts the recognised sleep request")

        lin.on_sleep_requested = None
        lin.send_goto_sleep(wait=0)
        request = lin.wait_sleep_request(timeout=1.0)
        check(
            isinstance(request, SleepRequested) and request.data == GOTO_SLEEP_DATA,
            "wait_sleep_request() picks the request out of the event queue",
        )

        lin.send_wakeup()
        check(True, "send_wakeup() is accepted with the bus quiet")
        check(
            lin.wait_sleep_request(timeout=0.05) is None,
            "a wake-up pulse produces no frame (it is not a frame)",
        )

        # Only data byte 0 identifies the command; the LIN standard reserves
        # bytes 1..7 and requires slaves to look at the first one only.
        check(
            is_goto_sleep(0x3C, bytes([0x00] + [0x55] * 7))
            and is_goto_sleep(0x3C, GOTO_SLEEP_DATA)
            and not is_goto_sleep(0x3C, bytes([0x01] + [0xFF] * 7))
            and not is_goto_sleep(0x10, GOTO_SLEEP_DATA)
            and not is_goto_sleep(0x3C, b"\x00\xff\xff"),
            "is_goto_sleep() keys on identifier, length and data byte 0 only",
        )

        lin.mode = Mode.IDLE
        try:
            lin.send_goto_sleep()
            check(False, "send_goto_sleep() outside master mode is rejected")
        except CommandRejected:
            check(True, "send_goto_sleep() outside master mode is rejected")
        try:
            lin.send_wakeup()
            check(False, "send_wakeup() with the channel idle is rejected")
        except CommandRejected:
            check(True, "send_wakeup() with the channel idle is rejected")
        lin.mode = Mode.MASTER

        try:
            lin.master_send_frame(0x10, b"")
            check(False, "empty data is rejected before hitting the wire")
        except ValueError:
            check(True, "empty data is rejected before hitting the wire")

        try:
            lin.set_baudrate(50)
            check(False, "an out-of-range baud rate is rejected locally")
        except ValueError:
            check(True, "an out-of-range baud rate is rejected locally")

        lin.mode = Mode.IDLE
        try:
            lin.master_send_frame(0x10, b"\x01")
            check(False, "a NACK raises CommandRejected")
        except CommandRejected as exc:
            check("MASTER_SEND_FRAME" in str(exc), f"a NACK raises CommandRejected ({exc})")

        lin.mode = Mode.MASTER
        with lin.schedule([(0x10, 30), (0x11, 30)]):
            pass
        check(True, "the schedule() context manager loads, starts and stops")

        regs = lin.read_uart_registers()
        check(
            abs(regs.actual_baudrate - 19200) < 100,
            f"read_uart_registers() recomputes the rate ({regs.actual_baudrate:.0f} baud)",
        )

        lin.master_send_frame(0x10, b"\x01\x02", wait=0)
        check(
            isinstance(lin.read_event(timeout=1.0), LinFrame),
            "wait=0 leaves the event for read_event()",
        )

        # --- per-identifier checksum default, checked on the wire -----------
        device: FakeSerial = lin._serial  # type: ignore[assignment]
        lin.master_send_frame(0x3C, b"\x64\x02\x3e\x00\xff\xff\xff\xff", wait=0)
        lin.master_send_frame(0x20, b"\x01\x02", wait=0)
        check(
            device.last_checksum[0x3C] == Checksum.CLASSIC,
            "a frame on 0x3C goes out with the classic checksum without being asked",
        )
        check(
            device.last_checksum[0x20] == Checksum.ENHANCED,
            "an ordinary identifier still defaults to the enhanced checksum",
        )
        lin.master_send_frame(0x3C, b"\x01\x02", checksum=Checksum.ENHANCED, wait=0)
        check(
            device.last_checksum[0x3C] == Checksum.ENHANCED,
            "an explicit checksum overrides the default",
        )
        lin.slave_set_response(0x3D, b"\x64\x02\x7e\x00\xff\xff\xff\xff")
        check(
            device.last_checksum[0x3D] == Checksum.CLASSIC,
            "a slave response on 0x3D also defaults to classic",
        )
        lin.slave_clear_response(0x3D)
        lin.flush_events()

        check(len(events) >= 4, f"the on_event callback saw every bus event ({len(events)})")
        check(
            lin.stats.events_dropped == 0 and lin.stats.malformed == 0,
            f"no dropped or malformed events ({lin.stats})",
        )

    check(True, "closing the device stops the reader thread cleanly")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
