# KanalibCan

Python API for the **CAN / CAN FD** channel of a KanaLin USB interface.

> The LIN channel of the same board is a **separate library**,
> [`KanalibLin`](../KanalibLin/README.md). They are independent and can run at
> the same time: CAN goes through the WinUSB vendor interface, LIN through the
> CDC serial port. Neither imports the other.

---

## Why this exists

The board speaks **gs_usb**, so `python-can` can already drive it (with a shim).
So why another library?

**Because `python-can` cannot do CAN FD here.** Its `gs_usb` backend is classic
only — the frame type holds 8 data bytes and the bus reports `CAN_20`. The
firmware does FD with BRS up to 64 bytes, confirmed on an oscilloscope
(1998 ns per arbitration bit and 497 ns per data bit *inside the same frame*),
and none of it is reachable through that path.

KanalibCan talks gs_usb straight over `pyusb`, so:

| | KanalibCan | python-can + gs_usb |
|---|---|---|
| Classic CAN | yes | yes |
| **CAN FD, BRS, up to 64 B** | **yes** | **no** |
| Needs a compatibility shim | no | yes (4 patches) |
| Bit timing read back from the registers | yes | no |
| KanaLin diagnostics (test mode, standby, error codes) | yes | no |
| `.asc`/`.blf` logging, `Notifier`, `can-isotp`, `udsoncan` | no | **yes** |

**Use python-can for classic-CAN work that benefits from its ecosystem. Use
this when you need FD, the diagnostics, or no shim.** Both are legitimate; they
just cannot hold the device at the same time (see the one-handle rule below).

---

## Install

```
pip install -e KanalibCan
```

Dependencies: `pyusb` and `libusb-package`. `python-can` is **not** a
dependency.

On Windows the CAN channel is interface 2 of the composite device and must be
bound to **WinUSB**, which happens automatically by WCID — no driver install
and no Zadig. The board also appears as a COM port; that is the LIN channel and
says nothing about whether the CAN interface bound correctly.

Run this first, always:

```
py KanalibCan/examples/00_preflight.py
```

---

## Quick start

```python
from kanalibcan import KanalibCan, CanMessage

with KanalibCan(bitrate=500_000) as bus:
    bus.send(CanMessage(0x123, b"\x01\x02\x03"))
    msg = bus.recv(timeout=1.0)
    print(msg)
```

CAN FD, which is the point:

```python
with KanalibCan(bitrate=500_000, fd=True, data_bitrate=2_000_000) as bus:
    bus.send(CanMessage(0x321, bytes(range(64)),
                        is_fd=True, bitrate_switch=True))
    for msg in bus.iter_messages(timeout=1.0):
        print(msg)
```

**By default the channel runs on the REAL bus.** Pass `loopback=True` for
internal loopback, which needs no wiring and no second node — that is what the
examples default to.

---

## Gotchas, all of them measured

### 1. One handle at a time

libusb on Windows gives exactly **one** handle to the WinUSB interface. A second
`KanalibCan`, a `python-can` bus, or another script touching the CAN channel
fails with `DeviceBusy` — and if you force it, libusb can throw an access
violation while cleaning up. Close one before opening the other.

The **LIN channel is not affected**: it is a serial port, so `KanalibLin` and
`KanalibCan` run happily side by side.

### 2. The echo carries no payload

Every transmitted frame comes back twice on a loopback bus: the **echo**
(`msg.is_echo`) and the **received copy** (`msg.is_rx`).

⚠️ **An echo's `data` is all zeros regardless of what you sent.** The firmware
builds it from the FDCAN Tx Event FIFO, which stores the header but not the
data. That is not corruption — an echo means *"this frame really went out on
the bus"*, and its value is the confirmation and the timestamp. For the bytes,
read the received copy.

```python
echo_id = bus.send(msg)
for m in bus.iter_messages(timeout=0.5):
    if m.is_echo and m.echo_id == echo_id:
        print("transmitted at", m.timestamp_us)   # do not read m.data
    elif m.is_rx:
        print("payload:", m.data.hex(" "))
```

Set `receive_own_messages=False` to drop echoes entirely.

### 3. `is_extended_id` defaults to **False**

`python-can` defaults it to `True`, which quietly turns `0x123` into a 29-bit
identifier. Here the default is standard, and an identifier that does not fit
raises `FrameError` instead of being **masked**:

```python
CanMessage(0x800).to_host_frame(0)   # FrameError: does not fit an 11-bit id
```

A truncated identifier is a frame that goes out under the wrong name and is
answered by the wrong node, with nothing anywhere saying so.

### 4. Bit rates must be exact, or they are refused

`solve()` accepts only rates the kernel clock divides exactly. On 144 MHz that
covers 10k/20k/50k/100k/125k/250k/500k/800k/1M nominal and 1M/2M/4M/8M data.
**5 Mbit/s is refused** (144/5 = 28.8). A bus quietly running at the wrong rate
is worse than an error.

The solver is tuned to reproduce the timings this project **measured on a
scope** — 500 k lands on 72 quanta with prescaler 4, 2 M data on 18 with
prescaler 4 — and the self-test asserts it. Other rates use the same rule but
have never been on a scope.

Confirm what actually reached the silicon:

```python
print(bus.read_bit_timing_registers())
```

That reads NBTP/DBTP back. gs_usb has no way to ask a device what timing it is
using, so without it there is no way to tell *"programmed"* from *"accepted and
thrown away"* — a bug that once lived in this firmware for an entire stage.

### 5. Not every payload length exists in CAN FD

FD has no length between 32 and 48. A 40-byte payload goes out as **48** with
the tail zero-filled, and the receiver cannot tell. `msg.is_padded` says so. If
a length matters to your protocol, carry it yourself.

### 6. An ACK error is not a fault

On a real bus with no other node, the controller reports `ACK`: the frame went
out and nobody answered. That is **proof the transceiver is driving**. The
failure to worry about is `BIT0` — dominant out, recessive back, meaning the
transceiver is not conducting. `examples/05_real_bus.py` spells this out.

### 7. A crashed run leaves the channel started

Bit timing can only be programmed while the channel is stopped, because
NBTP/DBTP are writable only with the peripheral held in initialisation.
`open()` therefore always sends `MODE(RESET)` first, so a previous run that died
badly does not turn into a mystifying pipe error.

### 8. A burst can outrun the bus, and `send()` will not tell you

A 64-byte FD frame at 500 kbit/s nominal with a 2 Mbit/s data phase occupies
about 250 us of wire. USB delivers faster than that, so a tight `for` loop of
`send()` calls hands the device more than it can transmit. The device queues
what it can (64 frames) and **counts the rest**; `send()` returns normally
either way, because it has already handed the frame over.

Check `diagnostics()["tx_dropped"]` after any burst. A non-zero value means
frames you believe you sent do not exist as far as the other node is concerned
-- which looks exactly like the other node ignoring you.

Measured: a back-to-back burst of 100 frames goes out intact; 300 loses about
15%, 600 about 27%, all of it counted. Spacing sends by 2 ms removes the loss
entirely. Earlier firmware discarded them **silently**, with no counter at all,
so a device reporting `tx_dropped = 0` on a large burst is old firmware rather
than a clean run.

### 9. `ERROR_PASSIVE` survives across runs

The FDCAN error counters are not cleared by opening the device. A run where
nobody acknowledged leaves the node at `tx_err = 128` and `ERROR_PASSIVE`, and
`open()` does not reset it -- only 128 **successful** transmissions bring it
back down, one per frame.

If a session starts behaving oddly, check `get_state()` before anything else.
And note the related trap: with no other node acknowledging, retransmission
means **nothing** goes out at all, not merely some frames.

---

## API

### `KanalibCan(...)`

| Argument | Default | Meaning |
|---|---|---|
| `bitrate` | `500_000` | nominal (arbitration) rate |
| `fd` | `False` | enable CAN FD |
| `data_bitrate` | `2_000_000` when `fd` | data-phase rate, used when BRS is set |
| `loopback` | `False` | internal loopback: nothing reaches CAN_H/CAN_L |
| `listen_only` | `False` | never transmit, **not even an ACK** |
| `one_shot` | `False` | do not retransmit on error/arbitration loss |
| `hw_timestamp` | `True` | MCU timestamps on every frame |
| `receive_own_messages` | `True` | deliver echoes as well as received frames |
| `rx_queue_size` | `8192` | host-side buffer; oldest are dropped when full |
| `serial` | `None` | pick a specific board by USB serial; required once two are attached |
| `sample_point` | `None` | nominal sample point as a fraction; `None` uses 87.5% |
| `data_sample_point` | `None` | data-phase sample point; `None` uses 83.33% |

Two boards share VID/PID and their COM numbers move between plug-ins, so
`serial` is the only stable way to choose one. Each board derives its serial
from its MCU unique device ID; `list_devices()` reports what is attached.

The sample points exist so two nodes can be **deliberately detuned** against
each other. The data-phase default of 83.33% is outside the 70-80% that
CiA 601-3 suggests, and an ECU is likely to use 75% or 80%; overriding it is how
you find out whether that matters on your bus rather than guessing.

Methods: `open()`, `close()`, `send(msg, timeout)`, `recv(timeout)`,
`iter_messages(timeout)`, `flush()`, `get_state()`, `state_name()`,
`get_timestamp_us()`, `features()`, `describe()`,
`read_bit_timing_registers()`, `diagnostics()`, `set_test_mode()`,
`set_standby()`. Properties: `pending`, `dropped`, `nominal_timing`,
`data_timing`, `fclk_hz`, `bt_const`, `bt_const_ext`, `device_config`.

Module level: `find_devices()` returns the raw pyusb devices, and
`list_devices()` returns the USB serial of each attached board -- the value to
pass as `serial`.

#### What `diagnostics()` returns

| Key | Meaning |
|---|---|
| `test_mode`, `stby_drive`, `pe2_level` | the KanaLin test controls; `pe2_level` is read from the pin, not from the variable |
| `last_error`, `last_error_code` | protocol error code. Reading **clears** it, so read once to clear, act, then read again |
| `data_last_error`, `data_last_error_code` | the same, for the **data phase** of a BRS frame. This is the one that moves when a frame fails after the bit rate switch, and the only way to tell that from a failure while arbitrating |
| `tx_err`, `rx_err` | FDCAN error counters |
| `tx_dropped` | frames handed over that never reached the bus (gotcha 8). Cumulative, not cleared by reading |
| `iso_mode`, `iso` | 1 for ISO 11898-1:2015 CRC, 0 for the original Bosch CAN FD. Two nodes that disagree trade classic frames perfectly and fail **every** FD frame |
| `tdc_value` | transmitter delay measured through the real transceiver, in data-phase quanta. 0 while the firmware leaves TDC disabled |

Use it as a context manager; `close()` stops the channel and releases the
handle, which matters because of the one-handle rule.

### `CanMessage`

`arbitration_id`, `data`, `is_extended_id`, `is_remote_frame`,
`is_error_frame`, `is_fd`, `bitrate_switch`, `error_state_indicator`,
`timestamp_us`, `is_rx`, `is_echo`, `overflow`, `echo_id`, `channel`, plus the
derived `dlc` and `is_padded`.

Field names follow `python-can` so code moves between them with little
friction, with the two deliberate differences noted above
(`is_extended_id` default, and no silent masking).

### Bit timing

```python
from kanalibcan import solve, describe_supported

sol = solve(500_000)                       # nominal
sol = solve(2_000_000, data_phase=True)    # data phase
print(sol.describe())
for s in describe_supported():
    print(s.describe())
```

---

## Examples

| File | What it shows |
|---|---|
| `00_preflight.py` | Install, device, timing read-back, a frame round trip, and FD. **Run first.** |
| `01_monitor.py` | Watch the bus; `--real`, `--listen-only`, `--demo` |
| `02_send_classic.py` | Periodic PDU with packed signals, period measured from MCU timestamps |
| `03_send_fd.py` | **Every valid FD length, 0..64, with BRS** — the reason this library exists |
| `04_diagnostics.py` | Timing read back from the registers, device state, PE2, achievable rates |
| `05_real_bus.py` | Transmit on a real bus and interpret the controller's verdict |

All default to internal loopback except `05_real_bus.py`, so they run with
nothing attached to CAN_H/CAN_L.

**With two boards attached, say which one**: `01`-`05` take `--serial`, and
`00_preflight.py` takes it as a positional argument (and reports every board it
can see, so run it first to learn the serials).

```
py examples/00_preflight.py 003200386528
py examples/01_monitor.py --serial 003200386528 --real
```

---

## Testing

```
python -m kanalibcan.selftest      # 55 checks, no hardware
python tests/test_offline.py       # 45 checks against a SIMULATED device
python examples/00_preflight.py    # needs the board
```

The self-test checks the framing against the values the firmware's own
differential test pinned down, and checks that the bit-timing solver reproduces
the scope-measured timings rather than some other arithmetically valid answer.
`test_offline.py` drives the whole API — open, timing, send, reader thread,
echo/rx split, FD, diagnostics, close — against a fake device.

**What none of that proves** is that the firmware behaves this way. That is what
`docs/gs_usb_test.py` (114 checks against the board) is for.

---

## Scope and limits

- **Classic CAN and CAN FD, one channel.** The device reports `icount = 0`.
- **No `.asc`/`.blf` logging, no `Notifier`, no ISO-TP, no UDS.** Deliberate:
  `python-can` plus `can-isotp`/`udsoncan` already do that well for classic
  CAN, and reimplementing it would be a worse version of a solved problem.
- **No automatic reconnection.** After the device disappears, build a new
  instance.
- **A sustained burst still loses frames**, because the host can outrun the bus
  indefinitely and no queue fixes that. What the library guarantees is that the
  loss is *visible*: see `tx_dropped` in gotcha 8.
- **Bit timing is per bus, not per node.** `sample_point`/`data_sample_point`
  let you detune one node deliberately; they are a diagnostic tool, not
  something to leave set in production code.
- **Termination is a manual jumper** (JP9 on the board), not software.
- **`IDENTIFY`, `USER_ID`, `TERMINATION` and `BERR_REPORTING` are not
  advertised by the firmware** and are therefore not exposed.
- Types are exported (`py.typed`) but **not verified** with mypy/pyright.
