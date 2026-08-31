#!/usr/bin/env python3
"""
04_diagnostics.py - what the device says about itself.

These are KanaLin extensions, NOT gs_usb. They exist because gs_usb can only
tell a device what to do, never ask what it is actually doing, and that
distinction has repeatedly been the difference between a real diagnosis and a
wrong one -- a bit rate that was accepted and thrown away looks exactly like
one that was programmed.

    py examples/04_diagnostics.py
    py examples/04_diagnostics.py --bitrate 250000
"""

from __future__ import annotations

import argparse
import sys

import _bootstrap  # noqa: F401,E402

from kanalibcan import KanalibCan, describe_supported  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bitrate", type=int, default=500_000)
    ap.add_argument("--data-bitrate", type=int, default=2_000_000)
    ap.add_argument("--serial", default=None,
                    help="USB serial of the board to use; with two "
                         "attached, pick one (list_devices())")
    args = ap.parse_args()

    bus = KanalibCan(bitrate=args.bitrate, fd=True,
                     data_bitrate=args.data_bitrate, loopback=True,
                     serial=args.serial).open()
    try:
        print(bus.describe())

        print("\n-- bit timing READ BACK from NBTP / DBTP --")
        regs = bus.read_bit_timing_registers()
        print(f"  fclk     {regs['fclk']} Hz")
        for phase in ("nominal", "data"):
            r = regs[phase]
            print(f"  {phase:<8} prescaler={r['prescaler']} sjw={r['sjw']} "
                  f"seg1={r['seg1']} seg2={r['seg2']} -> {r['bitrate']} bit/s")
        asked = {"nominal": args.bitrate, "data": args.data_bitrate}
        for phase, want in asked.items():
            got = regs[phase]["bitrate"]
            verdict = "matches what was asked" if got == want else "!! DIFFERS"
            print(f"  {phase:<8} {verdict} (asked {want})")

        print("\n-- device state --")
        st = bus.get_state()
        print(f"  state    {bus.state_name()}  rx_err={st.rxerr} "
              f"tx_err={st.txerr}")

        print("\n-- KanaLin diagnostic block --")
        d = bus.diagnostics()
        print(f"  test_mode        {d['test_mode']}")
        print(f"  stby requested   {d['stby_drive']}")
        print(f"  PE2 pin reads    {d['pe2_level']}  "
              "(read from the PIN, not from the variable)")
        print(f"  last_error       {d['last_error']}")
        print(f"  data_last_error  {d['data_last_error']}  "
              "(DATA phase of a BRS frame)")
        print(f"  tx_err / rx_err  {d['tx_err']} / {d['rx_err']}")
        print(f"  iso_mode         {d['iso']}")
        print(f"  tdc_value        {d['tdc_value']}  "
              "(0 = transmitter delay compensation disabled)")
        print(f"  tx_dropped       {d['tx_dropped']}  "
              "(frames asked for that never reached the bus)")
        print("\n  NOTE on PE2: on the MCP2562FD, HIGH means the transceiver is"
              "\n  ASLEEP. That is the opposite of the LIN transceiver's SLP_N,"
              "\n  where high means awake. Confirmed by measurement: awake gives"
              "\n  an ACK error on a bus with nobody to answer, asleep gives"
              "\n  BIT0 (we drive dominant and read back recessive).")

        print("\n-- the MCU clock (same base the frames are stamped with) --")
        t1 = bus.get_timestamp_us()
        t2 = bus.get_timestamp_us()
        print(f"  {t1} us -> {t2} us")

        print("\n-- rates this 144 MHz clock can produce EXACTLY --")
        for sol in describe_supported(bus.fclk_hz, data_phase=False):
            print(f"  nominal  {sol.describe()}")
        for sol in describe_supported(bus.fclk_hz, data_phase=True):
            print(f"  data     {sol.describe()}")
        print("  Anything not listed is refused rather than rounded: a bus"
              "\n  quietly running at the wrong rate is worse than an error.")
    finally:
        bus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
