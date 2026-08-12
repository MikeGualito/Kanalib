#!/usr/bin/env python3
"""
02_send_classic.py - send classic CAN frames and read them back.

Sends a small periodic PDU with packed signals, decodes what comes back, and
measures the period from the MCU's own timestamps rather than the host clock --
Windows scheduling jitter is of the same order as the period, so a host-side
measurement would mostly measure Windows.

    py examples/02_send_classic.py
    py examples/02_send_classic.py --real --count 100
"""

from __future__ import annotations

import argparse
import struct
import sys
import time

import _bootstrap  # noqa: F401,E402

from kanalibcan import CanMessage, KanalibCan  # noqa: E402

PDU_ID = 0x18A


def encode(rpm: float, coolant_c: int, throttle_pct: int, state: int) -> bytes:
    """rpm in 0.25 steps, coolant with a -40 offset, throttle in 0.5% steps."""
    return struct.pack("<HBBBBBB", int(rpm * 4) & 0xFFFF,
                       (coolant_c + 40) & 0xFF, int(throttle_pct * 2) & 0xFF,
                       state & 0xFF, 0, 0, 0)


def decode(data: bytes) -> str:
    rpm, coolant, throttle, state = struct.unpack("<HBBB", data[:5])
    return (f"EngineSpeed={rpm / 4:g}rpm  CoolantTemp={coolant - 40}degC  "
            f"ThrottlePos={throttle / 2:g}%  EngineState={state}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bitrate", type=int, default=500_000)
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--count", type=int, default=50)
    ap.add_argument("--period", type=float, default=0.1)
    args = ap.parse_args()

    bus = KanalibCan(bitrate=args.bitrate, loopback=not args.real).open()
    print(f"{'REAL BUS' if args.real else 'internal loopback'} at "
          f"{args.bitrate} bit/s, {args.count} PDUs every "
          f"{args.period * 1000:.0f} ms")
    print("-" * 62)

    stamps = []
    received = 0
    try:
        for i in range(args.count):
            payload = encode(800 + i * 137.5, 20 + i // 4, (i * 6) % 100,
                             1 + (i // 25))
            bus.send(CanMessage(PDU_ID, payload))
            print(f"  tx {payload.hex(' ')}", end="")

            deadline = time.time() + args.period
            shown = False
            while time.time() < deadline:
                msg = bus.recv(timeout=0.02)
                if msg is None or not msg.is_rx or msg.arbitration_id != PDU_ID:
                    continue
                received += 1
                stamps.append(msg.timestamp_us)
                if not shown:
                    print(f"   decoded rx: {decode(msg.data)}")
                    shown = True
            if not shown:
                print("   (nothing came back)")
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        bus.close()

    print("-" * 62)
    print(f"{received} PDUs received back")
    if len(stamps) >= 2:
        deltas = [(stamps[i + 1] - stamps[i]) & 0xFFFFFFFF
                  for i in range(len(stamps) - 1)]
        deltas.sort()
        print(f"period from the MCU timestamps: min {min(deltas) / 1000:.2f} ms, "
              f"median {deltas[len(deltas) // 2] / 1000:.2f} ms "
              f"(nominal {args.period * 1000:.0f} ms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
