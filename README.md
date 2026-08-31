# Kanalib

Python libraries for the **KanaLin USB interface** — a composite USB device that
carries a **LIN** channel and a **CAN / CAN FD** channel at the same time.

One board, two channels, two independent libraries:

| | [`KanalibLin`](KanalibLin/README.md) | [`KanalibCan`](KanalibCan/README.md) |
|---|---|---|
| Bus | LIN | Classic CAN and **CAN FD** |
| USB transport | CDC ACM (virtual COM port) | WinUSB vendor interface (gs_usb) |
| Import | `from kanaliblin import KanaLin, Mode` | `from kanalibcan import KanalibCan, CanMessage` |
| Dependency | `pyserial` | `pyusb`, `libusb-package` |
| Driver setup | none | none — WinUSB binds by WCID, no Zadig |

They are **fully independent**: neither imports the other, they share no state,
and they can run in the same process or in two separate scripts at the same
time. The split follows the hardware — the two channels are two different USB
interfaces with two different host stacks, and merging them would force every
LIN user to install a USB backend they never touch.

---

## Layout

```
Kanalib/
├── KanalibLin/          LIN channel
│   ├── kanaliblin/      package (device, framing, protocol, events, timestamps)
│   ├── examples/        6 runnable examples
│   ├── tests/           offline test suite (simulated firmware)
│   └── README.md        full API reference
└── KanalibCan/          CAN / CAN FD channel
    ├── kanalibcan/      package (device, framing, protocol, bittiming, message)
    ├── examples/        6 runnable examples
    ├── tests/           offline test suite (simulated device)
    └── README.md        full API reference
```

Each folder is a standalone installable project with its own `pyproject.toml`.
Install only the channel you need.

---

## Install

Python 3.9+.

```bash
pip install -e KanalibLin        # LIN  -> pulls pyserial
pip install -e KanalibCan        # CAN  -> pulls pyusb + libusb-package
```

Both ship a PEP 561 `py.typed` marker, so annotations reach mypy and Pylance in
your own code.

Before the first CAN session, run the preflight — it checks the install, the
device, the bit timing read back from the registers, and a frame round trip:

```bash
py KanalibCan/examples/00_preflight.py
```

> The board also shows up as a COM port. That is the **LIN** channel, and it
> says nothing about whether the CAN interface bound to WinUSB correctly.

---

## Choosing a board

With one board attached, nothing is needed. With two, pass the USB serial —
each board derives it from its MCU unique device ID, and it is the only stable
identity they have:

```python
from kanalibcan import KanalibCan, list_devices
from kanaliblin import KanaLin

list_devices()                 # ['003200386528', '009600536528']
KanaLin.list_devices()         # [('COM12', '00320...'), ('COM14', '00960...')]

# The ORDER is whatever enumeration returned -- never index by position,
# match on the serial.

bus = KanalibCan(serial="003200386528")
lin = KanaLin.open(serial_number="003200386528")
```

⚠️ A board reports its serial and answers on both channels **whether or not its
LIN transceiver is fitted**. The USB side does not depend on it, so "the port is
there" does not mean "the LIN hardware is there".

---

## Quick start

**LIN**

```python
from kanaliblin import KanaLin, Mode

with KanaLin.open() as lin:              # finds the port by USB VID/PID
    lin.reset()                          # known state: IDLE, no schedule, 19200 baud
    lin.mode = Mode.MASTER
    print(lin.master_send_frame(0x10, b"\x01\x02\x03\x04"))
```

**CAN FD**

```python
from kanalibcan import KanalibCan, CanMessage

with KanalibCan(bitrate=500_000, fd=True, data_bitrate=2_000_000) as bus:
    bus.send(CanMessage(0x321, bytes(range(64)), is_fd=True, bitrate_switch=True))
    for msg in bus.iter_messages(timeout=1.0):
        print(msg)
```

Both channels at once, from a single script:

```python
with KanaLin.open() as lin, KanalibCan(bitrate=500_000) as bus:
    ...
```

---

## Why not `python-can`?

The board speaks gs_usb, so `python-can` can drive the CAN channel with a shim —
but **its gs_usb backend is classic only**, so CAN FD is unreachable through it,
even though the firmware does FD with BRS up to 64 bytes (measured on a scope).

Use `python-can` for classic-CAN work that benefits from its ecosystem
(`.asc`/`.blf` logging, `Notifier`, `can-isotp`, `udsoncan`); use `KanalibCan`
when you need FD, the bit-timing read-back, the KanaLin diagnostics, or no shim.
They cannot hold the device at the same time — see the one-handle rule in the
[CAN README](KanalibCan/README.md#gotchas-all-of-them-measured).

There is no LIN equivalent to compare against: `KanalibLin` is the only way to
drive this channel.

---

## Things that bite, in one line each

Both READMEs document these in full, with the measurement behind each one. The
short version, because they are the failures that look like something else:

- **LIN — the device remembers.** Closing the COM port does not deconfigure USB,
  so mode, schedule and slave responses survive across runs. Start every script
  with `lin.reset()`.
- **LIN — checksum flavour is per identifier and fails silently.** `0x3C`/`0x3D`
  require the *classic* checksum; get it wrong and the ECU discards the frame
  while your own echo still reports `checksum_ok=True`. The library applies the
  rule for you.
- **CAN — an echo carries no payload.** A transmitted frame comes back as an
  echo (confirmation + timestamp, data all zeros) *and* as a received copy.
  Read the bytes from the received copy.
- **CAN — bit rates must divide the clock exactly.** 5 Mbit/s is refused on
  purpose; a bus quietly running at the wrong rate is worse than an error.
- **CAN — `is_extended_id` defaults to `False`**, unlike `python-can`, and an
  identifier that does not fit raises instead of being silently masked.
- **CAN — a burst can outrun the bus, and `send()` will not tell you.** The
  device queues what it can and counts the rest; check
  `diagnostics()["tx_dropped"]` after any burst, or space the sends.
- **CAN — `ERROR_PASSIVE` survives across runs** and `open()` does not clear it.
  A run where nobody acknowledged leaves the node at `tx_err = 128`; check
  `get_state()` when a session starts behaving oddly.
- **Both — two boards are told apart ONLY by their USB serial.** They share
  VID/PID and their COM numbers move between plug-ins. `KanalibCan.list_devices()`
  and `KanaLin.list_devices()` report what is attached.

---

## Testing without hardware

Everything below runs offline, against simulated firmware:

```bash
py -m kanaliblin.selftest         &&  py KanalibLin/tests/test_offline.py
py -m kanalibcan.selftest         &&  py KanalibCan/tests/test_offline.py   # 55 + 45
```

The self-tests check the framing layers against known values (CRC check value,
PID parity, escaping, gs_usb frame layout) and check that the CAN bit-timing
solver reproduces the **scope-measured** timings rather than some other
arithmetically valid answer. The `test_offline.py` suites drive the full public
API — open, configure, send, reader thread, events, diagnostics, close —
against a fake device.

What none of it proves is that the *firmware* behaves this way; only hardware
runs do that.

---

## Status

Version 1.1.0 for both packages, adding board selection by USB serial to both,
and to `KanalibCan` the data-phase diagnostics (`data_last_error`, `iso_mode`,
`tdc_value`, `tx_dropped`) and overridable sample points. `KanalibLin`
**replaces `LibKanaLin`**, which is obsolete: the package was renamed
`kanalin` → `kanaliblin` and imports must be updated, but the API, the behaviour
and the tests are the same validated code.

Both are backward compatible: everything added is a new keyword argument, a new
function, or a new key in the `diagnostics()` dictionary.

Known boundaries are documented per library, and they are deliberate — see
*Scope and limits* in [KanalibLin](KanalibLin/README.md#scope-and-limits) and
[KanalibCan](KanalibCan/README.md#scope-and-limits). The headlines: no
reconnection logic yet, no error injection or wake-up detection on LIN, no
logging/ISO-TP/UDS layer on CAN.

License: proprietary, all rights reserved (placeholder — no terms granted until
decided).
