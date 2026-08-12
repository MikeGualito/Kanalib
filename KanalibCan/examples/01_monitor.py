#!/usr/bin/env python3
"""
01_monitor.py - watch the bus and print every frame.

    py examples/01_monitor.py                 # internal loopback + demo traffic
    py examples/01_monitor.py --real          # a real bus, acknowledging
    py examples/01_monitor.py --real --listen-only
    py examples/01_monitor.py --seconds 0     # until Ctrl-C

ABOUT --real: without it the FDCAN runs in INTERNAL LOOPBACK, so nothing
reaches CAN_H/CAN_L and you only see what this program sends. That default is
deliberate: the examples must run on a bench with no second node.

ABOUT --listen-only: a monitor that acknowledges is a participant, not an
observer. Listen-only never sends a dominant bit, not even an ACK. The flip
side is that nobody acknowledges the OTHER node either, so a two-node bus where
one node is us in listen-only will see the transmitter report ACK errors.
"""

from __future__ import annotations

import argparse
import sys
import time

import _bootstrap  # noqa: F401,E402

from kanalibcan import CanMessage, KanalibCan  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bitrate", type=int, default=500_000)
    ap.add_argument("--data-bitrate", type=int, default=2_000_000)
    ap.add_argument("--fd", action="store_true", help="accept CAN FD frames")
    ap.add_argument("--real", action="store_true",
                    help="drive the real bus instead of internal loopback")
    ap.add_argument("--listen-only", action="store_true",
                    help="never transmit, not even an ACK (real bus only)")
    ap.add_argument("--demo", action="store_true",
                    help="inject traffic so there is something to watch")
    ap.add_argument("--seconds", type=float, default=5.0,
                    help="0 means until Ctrl-C")
    args = ap.parse_args()

    if args.listen_only and not args.real:
        print("--listen-only only makes sense with --real")
        return 2
    demo = args.demo or not args.real

    bus = KanalibCan(bitrate=args.bitrate, fd=args.fd,
                     data_bitrate=args.data_bitrate if args.fd else None,
                     loopback=not args.real,
                     listen_only=args.listen_only).open()
    where = "REAL BUS" if args.real else "internal loopback (nothing on CAN_H/L)"
    print(f"listening on {where} at {args.bitrate} bit/s"
          + ("  [listen-only]" if args.listen_only else ""))
    print("-" * 78)

    seen = {}
    total = 0
    errors = 0
    started = time.time()
    next_demo = started
    seq = 0

    try:
        while args.seconds == 0 or time.time() - started < args.seconds:
            if demo and not args.listen_only and time.time() >= next_demo:
                next_demo += 0.05
                bus.send(CanMessage(0x100 + (seq % 3), bytes([seq & 0xFF] * 8)))
                seq += 1

            msg = bus.recv(timeout=0.05)
            if msg is None:
                continue
            total += 1
            if msg.is_error_frame:
                errors += 1
            if msg.is_rx:
                seen[msg.arbitration_id] = seen.get(msg.arbitration_id, 0) + 1
            print(msg)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        bus.close()

    elapsed = time.time() - started
    print("-" * 78)
    print(f"{total} frames, {len(seen)} distinct IDs, {errors} error frames")
    if elapsed > 0:
        print(f"over {elapsed:.3f} s -> {total / elapsed:.1f} frames/s")
    for ident, count in sorted(seen.items()):
        print(f"   0x{ident:03X}  {count:>6}")
    if bus.dropped:
        print(f"host queue dropped {bus.dropped} frames (it filled up)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
