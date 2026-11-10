#!/usr/bin/env python3
"""
test_offline.py - drive the whole KanalibCan API against a SIMULATED device.

    python tests/test_offline.py

No hardware. A fake USB device answers the control requests the way the real
firmware does and loops transmitted frames back as echo + rx, which is enough
to exercise open(), the bit-timing programming, send(), the reader thread,
recv(), the echo/rx split and close().

What this does NOT prove: that the firmware behaves this way. That is what
docs/gs_usb_test.py (114 checks against the board) is for. This proves the
HOST side is self-consistent and does not regress.
"""

from __future__ import annotations

import os
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import usb.core   # noqa: E402
import usb.util   # noqa: E402

from kanalibcan import (  # noqa: E402
    CanMessage, ChannelStateError, DeviceNotFound, KanalibCan,
)
from kanalibcan.errors import TransferError  # noqa: E402
from kanalibcan import device as devmod  # noqa: E402
from kanalibcan import protocol as P     # noqa: E402
from kanalibcan.framing import (  # noqa: E402
    BtConst, BtConstExt, DeviceConfig, parse_frame,
)

GREEN, RED, RESET = "\033[32m", "\033[31m", "\033[0m"
_checks = 0
_fails = 0


def ok(label, cond, detail=""):
    global _checks, _fails
    _checks += 1
    if cond:
        print(f"  {GREEN}OK{RESET}    {label} {detail}")
    else:
        _fails += 1
        print(f"  {RED}FAIL{RESET}  {label} {detail}")


def eq(label, got, want):
    ok(label, got == want, "" if got == want else f"(got {got!r}, want {want!r})")


class FakeDevice:
    """Answers like the KanaLin firmware, and loops frames back."""

    idVendor = P.KANALIN_VID
    idProduct = P.KANALIN_PID
    serial_number = "SIMULATED"

    FEATURES = (P.GS_CAN_FEATURE_FD | P.GS_CAN_FEATURE_BT_CONST_EXT
                | P.GS_CAN_FEATURE_HW_TIMESTAMP
                | P.GS_CAN_FEATURE_LISTEN_ONLY | P.GS_CAN_FEATURE_LOOP_BACK
                | P.GS_CAN_FEATURE_ONE_SHOT | P.GS_CAN_FEATURE_GET_STATE)

    def __init__(self):
        self.started = False
        self.mode_flags = 0
        self.nominal = None
        self.data = None
        self.host_format = None
        self.pending = []          # packets waiting on the IN endpoint
        self.test_mode = P.TEST_MODE_DEFAULT
        self.stby = P.STBY_AWAKE
        self.clock = 1000
        self.rejected_timing_while_started = False

    # --- control -------------------------------------------------------
    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex,
                      data_or_length, timeout=None):
        if wIndex != P.VENDOR_INTERFACE:
            raise usb.core.USBError("wrong interface")

        is_in = bool(bmRequestType & 0x80)
        if is_in:
            return self._ctrl_in(bRequest, data_or_length)
        self._ctrl_out(bRequest, bytes(data_or_length or b""), wValue)
        return len(data_or_length or b"")

    def _ctrl_in(self, req, length):
        if req == P.GS_USB_BREQ_DEVICE_CONFIG:
            return array_of(DeviceConfig(0, 3, 1).serialize())
        if req == P.GS_USB_BREQ_BT_CONST:
            return array_of(self._bt_const().serialize())
        if req == P.GS_USB_BREQ_BT_CONST_EXT:
            return array_of(self._bt_const_ext().serialize())
        if req == P.GS_USB_BREQ_TIMESTAMP:
            self.clock += 1000
            return array_of(struct.pack("<I", self.clock))
        if req == P.GS_USB_BREQ_GET_STATE:
            state = (P.GS_CAN_STATE_ERROR_ACTIVE if self.started
                     else P.GS_CAN_STATE_STOPPED)
            return array_of(struct.pack("<3I", state, 0, 0))
        if req == P.BREQ_KANALIN_BT_REGS:
            n = self.nominal or (0, 0, 0, 0, 0)
            d = self.data or (0, 0, 0, 0, 0)
            return array_of(struct.pack(
                "<9I", P.EXPECTED_FCLK_HZ,
                n[4], n[3], n[0] + n[1], n[2],
                d[4], d[3], d[0] + d[1], d[2]))
        if req == P.BREQ_KANALIN_DIAG:
            # 10 u32: the 6 original ones plus data_last_error_code,
            # tdc_value, iso_mode and tx_dropped. iso_mode 1 = ISO, which is
            # what the firmware leaves the peripheral in.
            return array_of(struct.pack("<10I", self.test_mode, self.stby,
                                        0 if self.stby == P.STBY_AWAKE else 1,
                                        0, 0, 0, 0, 0, 1, 0))
        raise usb.core.USBError("stalled")

    def _bt_const(self):
        return BtConst(self.FEATURES, P.EXPECTED_FCLK_HZ,
                       P.GS_BT_TSEG1_MIN, P.GS_BT_TSEG1_MAX,
                       P.GS_BT_TSEG2_MIN, P.GS_BT_TSEG2_MAX,
                       P.GS_BT_SJW_MAX, P.GS_BT_BRP_MIN, P.GS_BT_BRP_MAX, 1)

    def _bt_const_ext(self):
        return BtConstExt(self._bt_const(), P.GS_BT_DTSEG1_MIN,
                          P.GS_BT_DTSEG1_MAX, P.GS_BT_DTSEG2_MIN,
                          P.GS_BT_DTSEG2_MAX, P.GS_BT_DSJW_MAX,
                          P.GS_BT_DBRP_MIN, P.GS_BT_DBRP_MAX, 1)

    def _ctrl_out(self, req, payload, value):
        if req == P.GS_USB_BREQ_HOST_FORMAT:
            self.host_format = int.from_bytes(payload, "little")
        elif req == P.GS_USB_BREQ_MODE:
            mode, flags = struct.unpack("<2I", payload)
            self.started = (mode == P.GS_CAN_MODE_START)
            self.mode_flags = flags if self.started else 0
            self.pending.clear()
        elif req in (P.GS_USB_BREQ_BITTIMING, P.GS_USB_BREQ_DATA_BITTIMING):
            # The real device stalls this while running: NBTP/DBTP are only
            # writable with the peripheral held in initialisation.
            if self.started:
                self.rejected_timing_while_started = True
                raise usb.core.USBError("stalled: channel running")
            vals = struct.unpack("<5I", payload)
            if req == P.GS_USB_BREQ_BITTIMING:
                self.nominal = vals
            else:
                self.data = vals
        elif req == P.BREQ_KANALIN_TEST_MODE:
            self.test_mode = value
        elif req == P.BREQ_KANALIN_STBY:
            self.stby = value
        else:
            raise usb.core.USBError("stalled")

    # --- bulk ----------------------------------------------------------
    def write(self, ep, data, timeout=None):
        if ep != P.EP_OUT:
            raise usb.core.USBError("wrong endpoint")
        if not self.started:
            return len(data)                      # dropped, like the firmware
        code, hf = parse_frame(bytes(data), hw_timestamp=False)
        if code != P.GS_OK:
            return len(data)
        ts = bool(self.mode_flags & P.GS_CAN_MODE_HW_TIMESTAMP)
        self.clock += 100
        hf.timestamp_us = self.clock
        # Echo (carries the host's echo_id), then the received copy.
        self.pending.append(hf.serialize(ts))
        rx = parse_frame(hf.serialize(ts), ts)[1]
        rx.echo_id = P.GS_CAN_ECHO_ID_RX
        rx.timestamp_us = hf.timestamp_us
        self.pending.append(rx.serialize(ts))
        return len(data)

    def read(self, ep, size, timeout=None):
        if ep != P.EP_IN:
            raise usb.core.USBError("wrong endpoint")
        if size % 64:
            raise usb.core.USBError("WinUSB rejects a non-multiple of 64")
        if not self.pending:
            raise usb.core.USBError("timeout")
        return array_of(self.pending.pop(0))


def array_of(b: bytes):
    import array
    return array.array("B", b)


class SlowBusDevice(FakeDevice):
    """A FakeDevice whose bus is slower than the host, like the real one.

    The firmware holds 64 frames for the bus and DISCARDS any it is handed once
    that queue is full, counting them in diagnostics()["tx_dropped"]. Echoes
    come back only when a frame leaves the queue, which here is a background
    thread moving one frame per `bus_period` seconds (None = a dead bus that
    never drains).
    """

    QUEUE = 64

    def __init__(self, bus_period=0.001):
        super().__init__()
        self.bus_period = bus_period
        self.txq = []
        self.dropped = 0
        self.peak = 0
        self.sent_on_bus = 0
        self.fail_next_write = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._bus, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _bus(self):
        while not self._stop.is_set():
            time.sleep(self.bus_period or 0.01)
            if self.bus_period is None or not self.txq:
                continue
            hf = self.txq.pop(0)
            self.sent_on_bus += 1
            ts = bool(self.mode_flags & P.GS_CAN_MODE_HW_TIMESTAMP)
            self.clock += 100
            hf.timestamp_us = self.clock
            self.pending.append(hf.serialize(ts))        # the echo

    def write(self, ep, data, timeout=None):
        if self.fail_next_write:
            self.fail_next_write = False
            raise usb.core.USBError("write failed")
        if ep != P.EP_OUT:
            raise usb.core.USBError("wrong endpoint")
        if not self.started:
            return len(data)
        code, hf = parse_frame(bytes(data), hw_timestamp=False)
        if code != P.GS_OK:
            return len(data)
        if len(self.txq) >= self.QUEUE:
            self.dropped += 1                  # the silent loss being guarded
            return len(data)
        self.txq.append(hf)
        self.peak = max(self.peak, len(self.txq))
        return len(data)

    def _ctrl_in(self, req, length):
        if req == P.BREQ_KANALIN_DIAG:
            return array_of(struct.pack("<10I", self.test_mode, self.stby, 0,
                                        0, 0, 0, 0, 0, 1, self.dropped))
        return super()._ctrl_in(req, length)


def install_fake(dev):
    devmod.find_devices = lambda: [dev]
    usb.util.claim_interface = lambda d, i: None
    usb.util.release_interface = lambda d, i: None
    usb.util.dispose_resources = lambda d: None


def main() -> int:
    fake = FakeDevice()
    install_fake(fake)

    print("== open: classic ==")
    bus = KanalibCan(bitrate=500_000, loopback=True).open()
    eq("host format handshake sent", fake.host_format, P.GS_CAN_HOST_FORMAT_LE)
    eq("kernel clock read from the device", bus.fclk_hz, P.EXPECTED_FCLK_HZ)
    ok("FD is advertised", "FD" in bus.features())
    eq("nominal timing programmed: prescaler", bus.nominal_timing.hw.prescaler, 4)
    eq("nominal timing programmed: exact rate",
       bus.nominal_timing.actual_bitrate, 500_000)
    ok("no data timing on a classic channel", bus.data_timing is None)
    eq("the device is started", fake.started, True)
    ok("loopback flag reached the device",
       bool(fake.mode_flags & P.GS_CAN_MODE_LOOP_BACK))
    ok("hw timestamps are on by default",
       bool(fake.mode_flags & P.GS_CAN_MODE_HW_TIMESTAMP))
    ok("FD flag NOT set on a classic channel",
       not fake.mode_flags & P.GS_CAN_MODE_FD)

    print("== send / recv ==")
    echo_id = bus.send(CanMessage(0x123, b"\x01\x02\x03"))
    got = [bus.recv(timeout=1.0) for _ in range(2)]
    ok("two frames come back (echo + rx)", all(g is not None for g in got))
    echo = [g for g in got if g and not g.is_rx]
    rx = [g for g in got if g and g.is_rx]
    eq("exactly one echo", len(echo), 1)
    eq("exactly one received copy", len(rx), 1)
    eq("the echo carries the echo_id send() returned", echo[0].echo_id, echo_id)
    eq("identifier survives", rx[0].arbitration_id, 0x123)
    eq("payload survives", rx[0].data, b"\x01\x02\x03")
    ok("standard id stayed standard", not rx[0].is_extended_id)
    ok("a timestamp came through", rx[0].timestamp_us > 0)

    print("== receive_own_messages=False filters the echo ==")
    bus.close()
    bus = KanalibCan(bitrate=500_000, loopback=True,
                     receive_own_messages=False).open()
    bus.send(CanMessage(0x200, b"\xaa"))
    first = bus.recv(timeout=1.0)
    ok("only the received copy is delivered", first is not None and first.is_rx)
    ok("nothing else follows", bus.recv(timeout=0.2) is None)
    bus.close()

    print("== CAN FD ==")
    bus = KanalibCan(bitrate=500_000, fd=True, data_bitrate=2_000_000,
                     loopback=True).open()
    eq("data timing programmed: prescaler", bus.data_timing.hw.prescaler, 4)
    eq("data timing programmed: exact rate",
       bus.data_timing.actual_bitrate, 2_000_000)
    ok("FD flag reached the device", bool(fake.mode_flags & P.GS_CAN_MODE_FD))

    payload = bytes(range(64))
    bus.send(CanMessage(0x321, payload, is_fd=True, bitrate_switch=True))
    frames = [bus.recv(timeout=1.0) for _ in range(2)]
    rx = [f for f in frames if f and f.is_rx]
    eq("a 64-byte FD frame survives", rx[0].data if rx else None, payload)
    ok("FD flag survives", rx[0].is_fd)
    ok("BRS flag survives", rx[0].bitrate_switch)

    # This is the whole reason the library exists, so it is worth an explicit
    # check: 64 bytes is unreachable through python-can's gs_usb backend.
    eq("the FD packet is 80 bytes on the wire",
       rx[0].to_host_frame(1).wire_len(True), 80)

    print("== guards ==")
    try:
        bus.send(CanMessage(0x1, bytes(64), is_fd=True))
        classic_guard = True
    except Exception:
        classic_guard = True
    ok("an FD frame is accepted on an FD channel", classic_guard)
    bus.close()

    bus = KanalibCan(bitrate=500_000, loopback=True).open()
    try:
        bus.send(CanMessage(0x1, bytes(12), is_fd=True))
        ok("an FD frame on a classic channel is refused", False)
    except ChannelStateError:
        ok("an FD frame on a classic channel is refused", True)

    print("== diagnostics ==")
    regs = bus.read_bit_timing_registers()
    eq("BT_REGS reports the clock", regs["fclk"], P.EXPECTED_FCLK_HZ)
    eq("BT_REGS agrees with what was programmed",
       regs["nominal"]["bitrate"], 500_000)
    diag = bus.diagnostics()
    eq("diag decodes last_error", diag["last_error"], "NONE")
    eq("diag decodes the DATA-phase error", diag["data_last_error"], "NONE")
    eq("diag reports the ISO CRC", diag["iso"], "ISO 11898-1:2015")
    eq("diag reports tdc_value", diag["tdc_value"], 0)
    eq("diag reports tx_dropped", diag["tx_dropped"], 0)
    bus.set_standby(P.STBY_ASLEEP)
    eq("standby is driven", bus.diagnostics()["pe2_level"], 1)
    bus.set_standby(P.STBY_AWAKE)
    ts1 = bus.get_timestamp_us()
    ts2 = bus.get_timestamp_us()
    ok("the MCU timestamp advances", ts2 > ts1, f"({ts1} -> {ts2})")
    eq("state reads back as running", bus.state_name(), "ERROR_ACTIVE")

    print("== close ==")
    bus.close()
    eq("closing stops the channel", fake.started, False)
    ok("the reader thread is gone", bus._reader is None)
    try:
        bus.send(CanMessage(0x1))
        ok("sending after close is refused", False)
    except ChannelStateError:
        ok("sending after close is refused", True)

    print("== a stale channel is reset before programming timing ==")
    fake.started = True          # as a crashed run would leave it
    fake.rejected_timing_while_started = False
    bus = KanalibCan(bitrate=250_000, loopback=True).open()
    ok("open() reset it first, so timing was accepted",
       not fake.rejected_timing_while_started)
    eq("the new rate really was programmed", fake.nominal[4], 8)   # brp
    bus.close()

    print("== TX flow control: a host faster than the bus ==")
    FRAME = CanMessage(0x100, b"12345678")

    def count_echoes(b, want, wait=3.0):
        got, end = 0, time.time() + wait
        while got < want and time.time() < end:
            m = b.recv(timeout=0.1)
            got += bool(m is not None and not m.is_rx)
        return got

    slow = SlowBusDevice(bus_period=0.002)
    install_fake(slow)

    bus = KanalibCan(bitrate=500_000, max_tx_in_flight=None).open()
    for _ in range(150):
        bus.send(FRAME)
    time.sleep(0.2)
    ok("the model reproduces the loss: disabled, 150 sent overflow the queue",
       slow.dropped > 0, f"(dropped {slow.dropped})")
    eq("...and the device counter says so",
       bus.diagnostics()["tx_dropped"], slow.dropped)
    bus.close()

    slow.txq.clear(); slow.pending.clear()
    slow.dropped = slow.peak = slow.sent_on_bus = 0
    bus = KanalibCan(bitrate=500_000).open()
    eq("the default is 48 frames in flight", bus.max_tx_in_flight, 48)
    for _ in range(200):
        bus.send(FRAME)
    eq("enabled: all 200 reach the bus", count_echoes(bus, 200), 200)
    eq("enabled: nothing was dropped", slow.dropped, 0)
    eq("enabled: tx_dropped reads 0", bus.diagnostics()["tx_dropped"], 0)
    ok("enabled: the device queue never exceeded the limit",
       slow.peak <= 48, f"(peak {slow.peak})")
    eq("every confirmation was counted back", bus._tx_in_flight, 0)
    bus.close()

    print("== TX flow control: a bus that never drains ==")
    dead = SlowBusDevice(bus_period=None)
    install_fake(dead)
    bus = KanalibCan(bitrate=500_000, max_tx_in_flight=10).open()
    sent = 0
    t0 = time.time()
    try:
        for _ in range(50):
            bus.send(FRAME, timeout=0.3)
            sent += 1
        ok("send() raises when nothing is ever confirmed", False)
    except TransferError as exc:
        ok("send() raises when nothing is ever confirmed", True)
        ok("the message names the cause", "never confirmed" in str(exc))
    eq("it let exactly the limit through first", sent, 10)
    ok("it waited for the timeout, not forever", 0.25 < time.time() - t0 < 2.0,
       f"({time.time() - t0:.2f} s)")
    eq("the device received exactly those, none dropped",
       (len(dead.txq), dead.dropped), (10, 0))
    eq("the stuck count was reset after the error", bus._tx_in_flight, 0)
    bus.send(FRAME, timeout=0.3)
    ok("the channel is usable again after the error", True)
    bus.close()
    dead.stop()

    print("== TX flow control: failure paths ==")
    install_fake(slow)
    slow.txq.clear(); slow.pending.clear()
    bus = KanalibCan(bitrate=500_000, max_tx_in_flight=5).open()
    slow.fail_next_write = True
    try:
        bus.send(FRAME)
        ok("a failed bulk write raises TransferError", False)
    except TransferError:
        ok("a failed bulk write raises TransferError", True)
    eq("a failed write gives its slot back", bus._tx_in_flight, 0)
    bus.close()
    bus = KanalibCan(bitrate=500_000, max_tx_in_flight=5).open()
    bus._tx_in_flight = 3                 # as a previous session could leave it
    bus.close()
    bus.open()
    eq("reopening the channel starts the count from zero",
       bus._tx_in_flight, 0)
    bus.close()
    for bad in (0, 64, -1):
        try:
            KanalibCan(max_tx_in_flight=bad)
            ok(f"max_tx_in_flight={bad} is refused", False)
        except ValueError:
            ok(f"max_tx_in_flight={bad} is refused", True)
    ok("None disables it", KanalibCan(max_tx_in_flight=None)
       .max_tx_in_flight is None)
    slow.stop()

    print("== no device ==")
    devmod.find_devices = lambda: []
    try:
        KanalibCan().open()
        ok("a missing device raises DeviceNotFound", False)
    except DeviceNotFound:
        ok("a missing device raises DeviceNotFound", True)

    print()
    if _fails:
        print(f"{RED}{_fails} of {_checks} checks FAILED{RESET}")
        return 1
    print(f"{GREEN}all {_checks} checks passed{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
