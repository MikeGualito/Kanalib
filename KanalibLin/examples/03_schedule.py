"""
Embedded schedule table: periodic headers with firmware timing.

Run:  python examples/03_schedule.py [COM7]

The scheduler runs inside the MCU, so its cadence does not depend on USB or on
host scheduling. Measure periods with the frame timestamps (captured in the
USART interrupt handler), never with the PC clock: on Windows the host-side
jitter is of the same order as a typical LIN period.
"""

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanaliblin import KanaLin, LinFrame, Mode  # noqa: E402

port = sys.argv[1] if len(sys.argv) > 1 else None
SLOTS = [(0x10, 30), (0x11, 30)]  # (id, pause after the slot, in ms) -> ~60 ms per id

with KanaLin.open(port) as lin:
    lin.reset()
    # Slots only send headers. Registering responses here makes this node
    # publish the data too, so the frames come back with payload.
    lin.slave_set_response(0x10, b"\xa0\xa1\xa2\xa3")
    lin.slave_set_response(0x11, b"\xb0\xb1")
    lin.mode = Mode.MASTER

    timestamps = defaultdict(list)
    with lin.schedule(SLOTS):
        for event in lin.events(timeout=1.0):
            if isinstance(event, LinFrame):
                timestamps[event.frame_id].append(event.timestamp_us)

    for frame_id, stamps in sorted(timestamps.items()):
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        period = min(gaps) if gaps else 0
        print(f"ID 0x{frame_id:02X}: {len(stamps)} frames, period {period} us (MCU clock)")

    print(f"status: {lin.get_status()}")
    print(f"link stats: {lin.stats}")
