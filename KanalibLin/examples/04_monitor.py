"""
Passive bus monitor with a callback.

Run:  python examples/04_monitor.py [COM7] [--baud 9600]

The device reports every frame it observes as soon as it is in SLAVE mode with
no responses registered -- it transmits nothing, it only listens. Press Ctrl-C
to stop.

The callback runs on the library's reader thread, so keep it short: print,
append to a list, put it on a queue. Anything slow belongs on your own thread.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanaliblin import KanaLin, LinFrame, Mode  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("port", nargs="?")
parser.add_argument("--baud", type=int, default=19200)
args = parser.parse_args()

start = time.monotonic()


def show(event) -> None:
    flag = "" if getattr(event, "checksum_ok", True) else "  <-- bad checksum"
    kind = "FRAME " if isinstance(event, LinFrame) else "HEADER"
    print(f"{time.monotonic() - start:8.3f}  {kind}  {event}{flag}")


with KanaLin.open(args.port, on_event=show) as lin:
    lin.reset()
    lin.set_baudrate(args.baud)
    lin.mode = Mode.SLAVE  # listen only: no headers, no responses
    print(f"monitoring {lin.port} at {args.baud} baud -- Ctrl-C to stop")
    try:
        while True:
            time.sleep(1.0)
            status = lin.get_status()
            if status.errors:
                print(f"          errors since last check: {status.errors.name}")
    except KeyboardInterrupt:
        print(f"\nstopped. {lin.stats}")
