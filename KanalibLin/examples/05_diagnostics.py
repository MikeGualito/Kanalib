"""
Health check and baud-rate diagnostics.

Run:  python examples/05_diagnostics.py [COM7]

Prints what the USART is really doing, recomputed from its registers rather
than from the HAL call that programmed it -- the only way to tell "the rate was
programmed wrong" from "my measurement is wrong".
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanaliblin import KanaLin  # noqa: E402

port = sys.argv[1] if len(sys.argv) > 1 else None

with KanaLin.open(port) as lin:
    print(f"port:      {lin.port}")
    print(f"alive:     {lin.ping()}")
    print(f"status:    {lin.get_status()}")

    lin.reset()
    for baudrate in (19200, 9600, 4800):
        lin.set_baudrate(baudrate)
        regs = lin.read_uart_registers()
        print(f"\nrequested {baudrate} baud")
        print(f"  {regs}")
        print(f"  bit time on the wire: {regs.bit_time_us:.1f} us (probe PB6, 3.3 V logic)")

    lin.set_baudrate(19200)
    print(f"\nback to 19200. {lin.stats}")
