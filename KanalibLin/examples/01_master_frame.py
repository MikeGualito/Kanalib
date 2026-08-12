"""
Master mode: publish a frame and poll a slave.

Run:  python examples/01_master_frame.py [COM7]

With no second ECU on the bus you still see your own frame come back: the LIN
transceiver echoes to RX everything it transmits, so the device reports its own
transmission as a received frame. That echo is the proof the break, the sync
field and the data really made it onto the wire.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanaliblin import HeaderOnly, KanaLin, Mode  # noqa: E402

port = sys.argv[1] if len(sys.argv) > 1 else None

with KanaLin.open(port) as lin:
    print(f"connected on {lin.port}")
    lin.reset()
    lin.mode = Mode.MASTER

    # --- publish a frame ourselves -------------------------------------
    frame = lin.master_send_frame(0x10, bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88]))
    if frame is None:
        print("no echo from the bus: check the LIN supply (VBAT) and the transceiver")
    else:
        print(f"published and observed: {frame}")

    # --- ask another node to publish -----------------------------------
    answer = lin.master_send_header(0x11)
    if isinstance(answer, HeaderOnly):
        print("header sent, nobody answered (expected with no second ECU)")
    elif answer is not None:
        print(f"a slave answered: {answer}")

    print(f"status: {lin.get_status()}")
