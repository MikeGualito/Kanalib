#!/usr/bin/env python3
"""
00_preflight.py - run this FIRST, and whenever anything stops working.

Checks the install, finds the board, reports what it advertises, and proves a
frame goes round. Every failure here points at one cause, which a bare
"Entity not found" from libusb never does.

    py examples/00_preflight.py
"""

from __future__ import annotations

import pathlib
import sys
import time

# Run straight from a checkout, without installing the package first.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from kanalibcan import (  # noqa: E402
    BackendMissing, CanMessage, DeviceBusy, DeviceNotFound, KanalibCan,
    find_devices, solve,
)
from kanalibcan.protocol import EXPECTED_FCLK_HZ  # noqa: E402

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
fails = 0


def check(label, cond, detail=""):
    global fails
    if cond:
        print(f"  {GREEN}ok  {RESET} {label}  {detail}")
    else:
        fails += 1
        print(f"  {RED}FAIL{RESET} {label}  {detail}")
    return cond


def note(text):
    print(f"  {YELLOW}note{RESET} {text}")


def main() -> int:
    print("== 1. the install ==")
    try:
        import usb.core          # noqa: F401
        check("pyusb is importable", True)
    except ImportError:
        check("pyusb is importable", False, "pip install pyusb")
        return 1
    try:
        import libusb_package    # noqa: F401
        check("libusb-package is importable", True)
    except ImportError:
        note("libusb-package missing; on Windows pyusb ships no libusb DLL")
        check("libusb-package is importable", False,
              "pip install libusb-package")

    print("\n== 2. the device ==")
    try:
        devices = find_devices()
    except BackendMissing as exc:
        check("a libusb backend is available", False, str(exc))
        return 1
    check("a libusb backend is available", True)
    if not check("a KanaLin device is attached", bool(devices),
                 f"({len(devices)} found)"):
        note("Plugged in? Did WinUSB bind to interface 2? The CDC port (the "
             "LIN channel) showing up as a COM port does NOT prove it.")
        return 1

    print("\n== 3. open the channel ==")
    try:
        bus = KanalibCan(bitrate=500_000, fd=True, data_bitrate=2_000_000,
                         loopback=True).open()
    except DeviceBusy as exc:
        check("the CAN interface can be claimed", False, str(exc))
        return 1
    except Exception as exc:
        check("the channel opens", False, f"{type(exc).__name__}: {exc}")
        return 1
    check("the channel opens", True)

    try:
        print()
        for line in bus.describe().splitlines():
            print("   ", line)
        print()

        check("kernel clock is the expected one", bus.fclk_hz == EXPECTED_FCLK_HZ,
              f"({bus.fclk_hz} Hz)")
        check("the device advertises FD", "FD" in bus.features())
        check("the device advertises hardware timestamps",
              "HW_TIMESTAMP" in bus.features())

        print("\n== 4. did the bit rate really reach the silicon? ==")
        regs = bus.read_bit_timing_registers()
        print(f"    nominal  {regs['nominal']}")
        print(f"    data     {regs['data']}")
        check("the nominal rate in NBTP is the one requested",
              regs["nominal"]["bitrate"] == 500_000,
              f"({regs['nominal']['bitrate']} bit/s)")
        check("the data rate in DBTP is the one requested",
              regs["data"]["bitrate"] == 2_000_000,
              f"({regs['data']['bitrate']} bit/s)")
        note("read back from the registers, not from what we asked for -- "
             "gs_usb cannot otherwise tell 'programmed' from 'accepted and "
             "thrown away'")

        print("\n== 5. a frame goes round (internal loopback) ==")
        bus.flush()
        bus.send(CanMessage(0x123, b"\x01\x02\x03"))
        time.sleep(0.2)
        got = list(bus.iter_messages(timeout=0.4))
        rx = [m for m in got if m.is_rx]
        echo = [m for m in got if m.is_echo]
        check("the frame came back", len(rx) == 1)
        check("the echo came back (proof it really was transmitted)",
              len(echo) == 1)
        if rx:
            check("identifier survived", rx[0].arbitration_id == 0x123,
                  f"(0x{rx[0].arbitration_id:X})")
            check("payload survived", rx[0].data == b"\x01\x02\x03",
                  rx[0].data.hex(" "))
            check("standard id stayed standard", not rx[0].is_extended_id)
            check("an MCU timestamp came with it", rx[0].timestamp_us > 0)

        print("\n== 6. CAN FD, which python-can cannot reach ==")
        bus.flush()
        payload = bytes(range(64))
        bus.send(CanMessage(0x321, payload, is_fd=True, bitrate_switch=True))
        time.sleep(0.2)
        got = list(bus.iter_messages(timeout=0.4))
        rx = [m for m in got if m.is_rx]
        check("a 64-byte FD frame came back", bool(rx))
        if rx:
            check("all 64 bytes survived", rx[0].data == payload,
                  f"({len(rx[0].data)} bytes)")
            check("the FD flag survived", rx[0].is_fd)
            check("the BRS flag survived", rx[0].bitrate_switch)
        note("the gs_usb backend of python-can is classic only, so this frame "
             "is unreachable through it")
    finally:
        bus.close()

    print("\n" + "-" * 62)
    if fails:
        print(f"{RED}{fails} check(s) failed{RESET}")
        return 1
    print(f"{GREEN}ALL OK{RESET} - KanalibCan can drive the CAN channel")
    print("Next: 01_monitor.py, 02_send_classic.py, 03_send_fd.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
