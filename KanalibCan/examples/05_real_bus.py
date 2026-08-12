#!/usr/bin/env python3
"""
05_real_bus.py - drive an actual bus, and tell you whether anyone answered.

Every other example defaults to internal loopback so it runs on a bench with
nothing attached. This one does not: it transmits for real and then reports the
controller's own verdict.

    py examples/05_real_bus.py
    py examples/05_real_bus.py --listen-only     # observe without acking

READING THE RESULT -- this is the whole point of the example:

  last_error = NONE   somebody ACKNOWLEDGED the frame. There is another live
                      node on the bus.
  last_error = ACK    the frame went out on a real, terminated bus and NOBODY
                      answered. Normal when this is the only node; it is also
                      the proof that the transceiver is driving.
  last_error = BIT0   we drove dominant and read back recessive: the
                      transceiver is not conducting. Check the standby pin,
                      the wiring and the 120 ohm termination.

An ACK error is therefore NOT a failure of this interface. It means the bus
works and you are alone on it.
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
    ap.add_argument("--id", type=lambda s: int(s, 0), default=0x123)
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--listen-only", action="store_true")
    ap.add_argument("--seconds", type=float, default=3.0)
    args = ap.parse_args()

    bus = KanalibCan(bitrate=args.bitrate, loopback=False,
                     listen_only=args.listen_only).open()
    try:
        print(bus.describe())
        print("-" * 66)
        bus.diagnostics()          # reading clears last_error_code
        bus.flush()

        sent = 0
        if not args.listen_only:
            for i in range(args.count):
                bus.send(CanMessage(args.id, bytes([i] * 8)))
                sent += 1
                time.sleep(0.01)

        deadline = time.time() + args.seconds
        rx, echoes = [], []
        while time.time() < deadline:
            msg = bus.recv(timeout=0.1)
            if msg is None:
                continue
            (rx if msg.is_rx else echoes).append(msg)
            print(msg)

        d = bus.diagnostics()
        st = bus.get_state()
        print("-" * 66)
        others = [m for m in rx if m.arbitration_id != args.id]
        own = len(rx) - len(others)
        print(f"  sent {sent}, transmitted (echoes) {len(echoes)}, "
              f"received {len(rx)} ({own} our own id, {len(others)} other ids)")
        if own:
            print("  NOTE: a node DOES get its own frames back in normal mode,")
            print("        but only once they are ACKNOWLEDGED. An "
                  "unacknowledged frame")
            print("        errors out and never reaches the Rx FIFO, so seeing "
                  "your own")
            print("        id here is itself evidence that somebody answered.")
        print(f"  last_error   {d['last_error']}")
        print(f"  tx_err/rx_err {d['tx_err']}/{d['rx_err']}   "
              f"state {bus.state_name()}")
        print()
        if d["last_error"] == "NONE" and sent and echoes:
            print("  -> A SECOND NODE ACKNOWLEDGED. The bus is alive and you "
                  "are not alone on it.")
        elif d["last_error"] == "ACK":
            print("  -> ACK error: the frames went out on a real bus and "
                  "nobody answered.\n     Expected when this is the only "
                  "node. The transceiver IS driving.")
        elif d["last_error"] == "BIT0":
            print("  -> BIT0: dominant out, recessive back. The transceiver is "
                  "not conducting.\n     Check standby, wiring and the 120 ohm "
                  "termination at BOTH ends.")
        elif args.listen_only:
            print("  -> listen-only: nothing was transmitted, by design.")
        if st.state == 3:
            print("  -> BUS_OFF: too many errors. Re-open the channel to "
                  "recover.")
    finally:
        bus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
