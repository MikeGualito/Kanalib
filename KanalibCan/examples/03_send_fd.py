#!/usr/bin/env python3
"""
03_send_fd.py - CAN FD: the reason this library exists.

python-can drives this board through its `gs_usb` backend, which is CLASSIC
ONLY: its frame type holds 8 data bytes and its bus reports CAN_20. The
firmware does FD with BRS up to 64 bytes -- measured on a scope, 1998 ns per
arbitration bit and 497 ns per data bit inside the same frame -- and none of
that is reachable through python-can. This example is.

    py examples/03_send_fd.py
    py examples/03_send_fd.py --real --data-bitrate 4000000
    py examples/03_send_fd.py --no-brs        # FD without the rate switch

WHAT TO NOTICE
  - Every valid FD length is exercised: 0..8, 12, 16, 20, 24, 32, 48, 64.
  - A length that is not one of those (say 40) is PADDED UP to the next one,
    and the receiver cannot tell the difference. is_padded says so.
  - With BRS the data phase runs at data_bitrate; without it, the whole frame
    stays at the nominal rate.
"""

from __future__ import annotations

import argparse
import sys
import time

import _bootstrap  # noqa: F401,E402

from kanalibcan import CanMessage, KanalibCan  # noqa: E402

FD_LENGTHS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bitrate", type=int, default=500_000)
    ap.add_argument("--data-bitrate", type=int, default=2_000_000)
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--no-brs", action="store_true",
                    help="send FD frames without the bit rate switch")
    ap.add_argument("--serial", default=None,
                    help="USB serial of the board to use; with two "
                         "attached, pick one (list_devices())")
    args = ap.parse_args()

    brs = not args.no_brs
    bus = KanalibCan(bitrate=args.bitrate, fd=True,
                     data_bitrate=args.data_bitrate,
                     loopback=not args.real, serial=args.serial).open()
    try:
        print(bus.describe())
        print("-" * 74)
        print(f"{'sent':>6}  {'got':>6}  {'dlc':>3}  match  notes")
        print("-" * 74)

        good = 0
        for n in FD_LENGTHS:
            payload = bytes((i * 7 + n) & 0xFF for i in range(n))
            msg = CanMessage(0x321, payload, is_fd=True, bitrate_switch=brs)
            bus.flush()
            bus.send(msg)
            time.sleep(0.05)

            rx = [m for m in bus.iter_messages(timeout=0.3) if m.is_rx]
            if not rx:
                print(f"{n:>6}  {'-':>6}  {'-':>3}  {'NO':>5}  nothing came back")
                continue
            got = rx[0]
            same = got.data == payload
            good += same
            notes = []
            if got.is_fd:
                notes.append("FD")
            if got.bitrate_switch:
                notes.append("BRS")
            if got.error_state_indicator:
                notes.append("ESI")
            print(f"{n:>6}  {len(got.data):>6}  {got.dlc:>3}  "
                  f"{'yes' if same else 'NO':>5}  {' '.join(notes)}")

        print("-" * 74)
        print(f"{good} of {len(FD_LENGTHS)} lengths came back byte-identical")

        print("\n-- a length CAN FD cannot represent --")
        odd = bytes(range(40))
        msg = CanMessage(0x322, odd, is_fd=True, bitrate_switch=brs)
        print(f"40 bytes -> DLC code {msg.dlc} = "
              f"{len(msg.to_host_frame(0).data)} bytes on the wire; "
              f"is_padded={msg.is_padded}")
        bus.flush()
        bus.send(msg)
        time.sleep(0.05)
        rx = [m for m in bus.iter_messages(timeout=0.3) if m.is_rx]
        if rx:
            print(f"received {len(rx[0].data)} bytes: the first 40 are the "
                  f"payload ({'intact' if rx[0].data[:40] == odd else 'CHANGED'}), "
                  "the rest is padding the receiver cannot distinguish")
            print("If a length matters to your protocol, carry it yourself.")
    finally:
        bus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
