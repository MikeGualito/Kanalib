"""
Slave mode: answer headers sent by another node.

Run:  python examples/02_slave_response.py [COM7]

The first part registers a response and leaves the device listening, which is
what you want with a real master on the bus.

The second part is the trick that makes this testable with a single node: the
device answers any header for a registered identifier, including headers it
sends itself. So registering a response and then sending the matching header
exercises the whole slave path -- table lookup, precomputed checksum, response
chained inside the LIN timing window -- with nothing else on the bus.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanaliblin import KanaLin, Mode  # noqa: E402

port = sys.argv[1] if len(sys.argv) > 1 else None
SLAVE_ID = 0x11
RESPONSE = bytes([0xDE, 0xAD, 0xBE, 0xEF])

with KanaLin.open(port) as lin:
    lin.reset()
    lin.slave_set_response(SLAVE_ID, RESPONSE)

    # --- 1) listen for a real master ------------------------------------
    lin.mode = Mode.SLAVE
    print(f"answering ID 0x{SLAVE_ID:02X} with {RESPONSE.hex(' ')}; listening for 3 s...")
    seen = 0
    for event in lin.events(timeout=3.0):
        print(f"  {event}")
        seen += 1
    if not seen:
        print("  nothing on the bus (no master is polling)")

    # --- 2) self-poll: prove the response path works, alone -------------
    lin.mode = Mode.MASTER
    answer = lin.master_send_header(SLAVE_ID)
    if answer is None:
        print("no bus echo at all: check the transceiver and the LIN supply")
    elif getattr(answer, "data", None) == RESPONSE:
        print(f"the device answered its own header: {answer}")
    else:
        print(f"unexpected: {answer}")

    lin.slave_clear_response(SLAVE_ID)
    print(f"after clearing, the same header gives: {lin.master_send_header(SLAVE_ID)}")
