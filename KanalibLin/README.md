# KanalibLin — Python API for the LIN channel

Python library for the **LIN** channel of a KanaLin USB interface. It turns
the device's byte protocol into ordinary Python calls: open the port, pick a
role, send frames, read what happened on the bus.

> The CAN / CAN FD channel of the same board is a **separate library**,
> [`KanalibCan`](../KanalibCan/README.md). They are independent and can run at
> the same time: LIN goes through the CDC serial port, CAN through the WinUSB
> vendor interface. Neither imports the other.
>
> ⚠️ **This replaces `LibKanaLin`**, which is obsolete. The package was renamed
> `kanalin` → `kanaliblin`, so update your imports:
> `from kanaliblin import KanaLin, Mode`. Nothing else changed — the API, the
> behaviour and the tests are the same validated code.

```python
from kanaliblin import KanaLin, Mode

with KanaLin.open() as lin:          # finds the port by USB VID/PID
    lin.reset()                      # known state: IDLE, no schedule, 19200 baud
    lin.mode = Mode.MASTER
    frame = lin.master_send_frame(0x10, b"\x01\x02\x03\x04")
    print(frame)                     # ID 0x10 [4] 01 02 03 04 @123456 us
```

---

## Table of contents

- [Install](#install)
- [Concepts you need before writing code](#concepts-you-need-before-writing-code)
- [Getting started](#getting-started)
- [API reference](#api-reference)
  - [KanaLin — connection](#kanalin--connection)
  - [Channel configuration](#channel-configuration)
  - [Master](#master)
  - [Slave](#slave)
  - [Bus sleep and wake-up](#bus-sleep-and-wake-up)
  - [Schedule table](#schedule-table)
  - [Bus events](#bus-events)
  - [Diagnostics](#diagnostics)
  - [Timestamps](#timestamps)
  - [Event objects](#event-objects)
  - [Enumerations and constants](#enumerations-and-constants)
  - [Exceptions](#exceptions)
  - [Low-level framing](#low-level-framing)
- [Recipes](#recipes)
- [Threading model](#threading-model)
- [Logging](#logging)
- [Reconnecting](#reconnecting)
- [Troubleshooting](#troubleshooting)
- [Testing without hardware](#testing-without-hardware)
- [Scope and limits](#scope-and-limits)

---

## Install

Requires Python 3.9+ and [pyserial](https://pypi.org/project/pyserial/).

```bash
pip install pyserial
pip install -e KanalibLin        # optional: makes `import kanaliblin` work anywhere
```

The package ships type annotations and a `py.typed` marker, so mypy and Pylance
pick them up in your own code — with one caveat noted under
[Scope and limits](#not-yet-implemented-in-this-library).

Without installing, add the folder to the path:

```python
import sys; sys.path.insert(0, "KanalibLin")
import kanaliblin
```

The device enumerates as a composite USB device. This library uses its **CDC ACM
interface** (a virtual COM port, VID `0x0483` / PID `0x5710`); the other
interface is the vendor/WinUSB channel reserved for CAN and is not touched here.
No driver installation is needed on Windows.

---

## Concepts you need before writing code

Four behaviours of the device explain most surprises. They are not bugs.

**1. The device remembers.** Mode, schedule table and slave responses survive
closing the COM port — closing a port does not deconfigure USB, so the firmware
never sees a disconnect. Two runs of the same script are *not* independent.
Always start with [`reset()`](#reset).

**2. You see your own traffic.** The LIN transceiver echoes back on RX
everything it transmits, so a frame you publish comes back to you as a received
frame. This is why `master_send_frame()` can return the frame it just sent — and
that echo is the proof the break really reached the wire. It also means a single
device can test its own slave path, with nothing else on the bus.

**3. A registered response answers in any mode except IDLE.** The slave response
table is not tied to `Mode.SLAVE`: any header for a registered identifier gets
answered, including headers this device sends itself. That mirrors a real master
that publishes some of its own frames, and it is what makes single-node testing
possible.

**4. Error flags are sticky and clear on read.** `get_status()` reports
everything since the previous call, then resets the flags. A `TIMEOUT` after a
bare header is *normal*: it is how the firmware marks "header sent, nobody
answered".

<a name="checksum-flavour"></a>
**5. Checksum flavour is per identifier, and getting it wrong is silent.** LIN
has two checksums — classic (data only) and enhanced (data + PID) — and the
standard requires the **classic** one on the diagnostic identifiers `0x3C` and
`0x3D`, whatever the rest of the bus uses. Send a request on `0x3C` with an
enhanced checksum and the ECU discards it without a word; worse, the local echo
still reports `checksum_ok=True`, because the device verifies frames it
published against the flavour it was told to use. The symptom is "the ECU does
not answer", with nothing pointing at the cause.

The library applies the rule for you: leave `checksum` unset and it picks
classic on `0x3C`/`0x3D` and enhanced elsewhere. Overriding is still allowed,
with a logged warning on the diagnostic identifiers.

---

## Getting started

```python
from kanaliblin import KanaLin, Mode, LinFrame

with KanaLin.open() as lin:
    lin.reset()

    # --- as master: publish data -------------------------------------
    lin.mode = Mode.MASTER
    lin.master_send_frame(0x10, b"\x11\x22\x33\x44")

    # --- as master: poll another node --------------------------------
    answer = lin.master_send_header(0x21)
    if isinstance(answer, LinFrame):
        print("slave answered:", answer.data.hex(" "))

    # --- as slave: answer a real master ------------------------------
    lin.slave_set_response(0x30, b"\xde\xad\xbe\xef")
    lin.mode = Mode.SLAVE
    for event in lin.events(timeout=5.0):
        print(event)
```

Runnable examples live in [`examples/`](examples):

| File | Shows |
|---|---|
| `01_master_frame.py` | publishing a frame and polling a header |
| `02_slave_response.py` | registering a response, and self-polling to validate it |
| `03_schedule.py` | the embedded scheduler and measuring the period from MCU timestamps |
| `04_monitor.py` | passive monitoring with an `on_event` callback |
| `05_diagnostics.py` | health check, baud rates, raw USART registers |
| `06_uds_diagnostics.py` | UDS on the LIN transport layer: TesterPresent, extended session |

---

## API reference

### `KanaLin` — connection

#### `KanaLin.open(port=None, *, serial_number=None, **kwargs) -> KanaLin`

Open a device. With `port=None` it finds the port by USB VID/PID.

```python
lin = KanaLin.open()                            # auto-detect
lin = KanaLin.open("COM7")                      # explicit port
lin = KanaLin.open(serial_number="003200386528")  # explicit BOARD
```

⚠️ **With more than one board attached, pass `serial_number`.** They share
VID/PID, and a COM number is not an identity: Windows can reassign it between
plug-ins, so the port that was your board last week may be the other one today.
Each board derives its USB serial from its MCU unique device ID, which is the
only thing that stays put. Without it, `open()` takes the first port found and
logs which one it picked.

Raises [`DeviceNotFound`](#exceptions) if nothing matches or the port cannot be
opened. Keyword arguments are passed to the constructor:

| Argument | Default | Meaning |
|---|---|---|
| `timeout` | `1.0` | seconds to wait for a command reply |
| `on_event` | `None` | callable invoked for every bus event (see [Threading model](#threading-model)) |
| `event_queue_size` | `4096` | bus events buffered for `read_event()` |

#### `KanaLin.find_ports(vid=0x0483, pid=0x5710, serial_number=None) -> list[str]`

Every port matching the KanaLin VID/PID, optionally narrowed to one board by its
USB serial.

#### `KanaLin.list_devices(vid=0x0483, pid=0x5710) -> list[tuple[str, str]]`

`(port, serial_number)` for every attached board — use it to find out which
serial to pass to `open()`.

```python
>>> KanaLin.list_devices()
[('COM12', '003200386528'), ('COM14', '009600536528')]
```

> A board whose LIN transceiver is not fitted still enumerates, still reports a
> serial, and still answers `ping()` and `loopback()` — it just never sees
> anything on the bus. Do not read "the port is there" as "the LIN hardware is
> there".

#### `close()` / context manager

```python
with KanaLin.open() as lin:
    ...                         # close() runs on exit, even on exception
```

`close()` stops the reader thread and closes the port; it is safe to call twice.
It does **not** reset the device — see the first concept above.

#### Properties

| Property | Type | Notes |
|---|---|---|
| `port` | `str` | the COM port / tty in use |
| `is_open` | `bool` | link still usable |
| `stats` | [`Stats`](#stats) | counters from the reader thread |
| `registered_responses` | `frozenset[int]` | identifiers *this instance* registered |

---

### Channel configuration

<a name="reset"></a>
#### `reset(*, baudrate=19200, clear_responses=True) -> Status`

Put the device in a known state: `IDLE`, empty schedule, no slave responses,
default baud rate, error flags cleared. Returns the resulting `Status`.

**Call this at the start of every script.** Without it a run inherits whatever
the previous one left behind — a running schedule, or a registered response
answering headers you expected to go unanswered. The commands are pipelined into
a single write, so it costs one round trip, not seventy.

Pass `clear_responses=False` to keep the response table (faster, and correct
when you know the device is yours).

#### `mode` — property, and `set_mode(mode)`

```python
lin.mode = Mode.MASTER          # same as lin.set_mode(Mode.MASTER)
print(lin.mode)                 # cached; get_status() re-reads it from the device
```

| Mode | The channel… |
|---|---|
| `Mode.IDLE` | neither transmits nor reports bus events |
| `Mode.MASTER` | may send headers/frames and run the schedule |
| `Mode.SLAVE` | listens, and answers registered identifiers |

Any mode change **stops the schedule**. The loaded table is kept, so
`schedule_start()` relaunches it.

#### `baudrate` — property, and `set_baudrate(baudrate)`

```python
lin.set_baudrate(9600)
```

Reconfigures UART5 at runtime, 1000–20000 baud. The device verifies the rate by
reading it back from the peripheral, so an accepted command means the USART
really was reprogrammed — not merely that the value was accepted.

Rejected (`CommandRejected`) while the schedule is running or a transmission is
in flight. **The change is volatile:** a device reset returns to 19200.

> Below roughly **2200 baud** the USART divider overflows on this board (UART5
> runs off PCLK1 at 144 MHz with prescaler DIV1). The firmware detects it and
> rejects the command, so nothing breaks silently — but do not expect 1200 baud.

#### `get_status() -> Status`

Mode, bus state and the sticky error flags. **Reading clears the flags.**

```python
status = lin.get_status()
if status.errors:
    print("bus errors since last check:", status.errors.name)
```

---

### Master

#### `master_send_frame(frame_id, data, *, checksum=None, wait=None) -> BusEvent | None`

Publish a complete frame: break + sync + PID + data + checksum. Requires
`Mode.MASTER` and 1–8 data bytes.

`checksum=None` picks the flavour by identifier: **classic** on the diagnostic
identifiers `0x3C`/`0x3D`, **enhanced** everywhere else. Pass a value to
override — that is what negative testing would need, and the library logs a
warning if you force enhanced on a diagnostic identifier, because that fails
silently on the bus (see [Checksum flavour](#checksum-flavour)).

Returns the frame **as observed on the bus** (your own echo), or `None` if no
echo arrived within `wait` seconds. `wait=None` picks a sensible value from the
current baud rate; `wait=0` returns immediately and leaves the event in the
queue.

```python
frame = lin.master_send_frame(0x10, bytes([1, 2, 3, 4]))
if frame is None:
    print("no echo — check the LIN supply and the transceiver")
```

#### `master_send_header(frame_id, *, wait=None) -> BusEvent | None`

Send a header and let another node publish the data. Requires `Mode.MASTER`.

Returns a [`LinFrame`](#linframe) if somebody answered, a
[`HeaderOnly`](#headeronly) if nobody did, or `None` if the device reported
nothing.

```python
answer = lin.master_send_header(0x21)
match answer:
    case LinFrame():   print("data:", answer.data.hex(" "))
    case HeaderOnly(): print("nobody answered")
    case None:         print("the device saw nothing at all")
```

---

### Slave

#### `slave_set_response(frame_id, data, *, checksum=None)`

Register the response this device publishes when it sees `frame_id`. 1–8 bytes.
`checksum` follows the same per-identifier default as `master_send_frame()`.

The checksum is computed by the firmware **at registration time**, not when
answering, so the response goes out inside the LIN timing window.

The table does not require `Mode.SLAVE`, survives mode changes, and survives
closing the port. To validate the response path without a second ECU, register
it and send the matching header yourself:

```python
lin.slave_set_response(0x21, b"\xde\xad\xbe\xef")
lin.mode = Mode.MASTER
print(lin.master_send_header(0x21))     # the device answers its own header
```

#### `slave_clear_response(frame_id)`

Stop answering one identifier.

#### `slave_clear_all()`

Clear all 64 identifiers, including responses left by other scripts. Pipelined
into a single write.

---

### Bus sleep and wake-up

These act on the **bus**, at your request. KanaLin itself never sleeps: its
transceiver stays in Normal mode permanently and nothing here touches the
`SLP_N` pin. They exist to exercise the sleep handling of the ECU under test.

#### `send_goto_sleep(*, wait=None) -> BusEvent | None`

Send the standard LIN go-to-sleep command: identifier `0x3C` carrying
`00 FF FF FF FF FF FF FF` with the classic checksum.

It is an ordinary frame in every respect, so it comes back through the
transceiver echo — the returned `LinFrame` is your own transmission, and its
arrival is the proof it reached the wire.

Requires `MASTER` mode; ordering the bus to sleep is the master's job.

```python
lin.mode = Mode.MASTER
echo = lin.send_goto_sleep()
print(echo)          # ID 0x3C [8] 00 ff ff ff ff ff ff ff @... us
```

#### `send_wakeup()`

Put a wake-up pulse on the bus: dominant for `WAKEUP_PULSE_US` (1000 µs), then
recessive. **Not a byte transmission** — the firmware drives the TX pin as a
plain GPIO, so the width does not move with the baud rate. The LIN window is
250 µs to 5 ms, and a byte-based trick would drift out of it at low rates.

Does **not** require master mode (a slave may request a wake-up too), but it
does require the channel not to be `IDLE` and the bus to be quiet: no schedule
running, nothing in flight. A 1 ms dominant dropped on a frame in progress would
destroy it, so the firmware answers `NACK` instead of queuing the pulse.

Nothing is reported back for it — a wake-up pulse is not a frame.

#### Detecting a sleep request

A sleep request is **not a separate event on the wire**: it *is* the `0x3C`
frame, which the firmware already reports in full. The library recognises it,
which costs the MCU nothing:

```python
def on_sleep(req):                       # called from the reader thread
    print("bus asked to sleep at", req.timestamp_us)

with KanaLin.open(on_sleep_requested=on_sleep) as lin:
    ...

# or poll for it
request = lin.wait_sleep_request(timeout=2.0)
```

The matching frame is **also** delivered through the normal event queue and
`on_event`, so a bus monitor still sees exactly what crossed the wire. The
counter `stats.sleep_requests` tracks how many were recognised.

Match criteria (`kanaliblin.protocol.is_goto_sleep`): identifier `0x3C`, 8 data
bytes, `data[0] == 0x00`, and a good checksum. **Data bytes 1..7 are not
checked** — the standard fills them with `0xFF` but declares them reserved and
requires slaves to evaluate only the first byte, so demanding all eight would be
stricter than the standard.

> ⚠️ **There is no wake-up detection.** The device *emits* wake-up pulses but
> cannot tell you about somebody else's. This is a hardware limit, not an
> omission: the USART reports a wake-up pulse as an ordinary LIN break and
> exposes no way to measure the dominant width, and the two genuinely overlap
> (at 19200 a break is ≥ 573 µs and a wake-up pulse may be 250 µs – 5 ms). See
> section 3.6 of the protocol document for the measurements behind this.

---

### Schedule table

The scheduler runs **entirely in firmware**, so its cadence does not depend on
USB or host scheduling latency. Use it whenever timing matters; a Python loop
calling `master_send_frame()` is for interactive work.

#### `schedule_load(entries)`

`entries` is a sequence of `(frame_id, delay_ms)` tuples or
[`ScheduleEntry`](#scheduleentry) objects. `delay_ms` is the pause **after** that
slot; the table is circular, so two slots of 30 ms repeat each identifier every
~60 ms. An empty sequence clears the table.

Slots send **headers only**. For a slot to carry data, some node must publish a
response for that identifier — including this one, via `slave_set_response()`.

Rejected while the schedule is running: stop it first. There is **no way to read
the table back** from the device.

#### `schedule_start()` / `schedule_stop()`

`schedule_start()` requires `Mode.MASTER` and a non-empty table.
`schedule_stop()` is idempotent, and a mode change stops the scheduler too.

#### `schedule(entries)` — context manager

Load, start, and stop on exit (the table stays loaded):

```python
with lin.schedule([(0x10, 30), (0x11, 30)]):
    for event in lin.events(timeout=2.0):
        print(event)
```

---

### Bus events

Bus events arrive asynchronously. The reader thread never blocks, so they keep
being collected while your code does something else. There are three ways to
consume them — pick one per event stream:

| Way | Use when |
|---|---|
| return value of `master_send_frame()` / `master_send_header()` | request/response style, one frame at a time |
| `read_event()` / `events()` | you drive the loop |
| `on_event=` callback | you want everything, including events another call consumes |

#### `read_event(timeout=0.0) -> BusEvent | None`

Pop the next event. `timeout=0` polls, `None` blocks forever.

#### `events(timeout=None, *, idle_timeout=0.5) -> Iterator[BusEvent]`

Iterate for `timeout` seconds (`None` = forever), stopping early if nothing
arrives for `idle_timeout`:

```python
for event in lin.events(timeout=5.0):
    print(event)
```

#### `wait_frame(frame_id=None, timeout=1.0) -> BusEvent | None`

Wait for the next event, optionally for one identifier. Events for other
identifiers seen while waiting are **discarded** (counted in
`stats.events_discarded`).

#### `flush_events() -> int`

Drop everything queued; returns how many.

> **One consumer per stream.** The calls that return a frame consume from the
> same queue as `read_event()`. If a schedule is running, or another thread is
> reading events, use `wait=0` on those calls and collect everything yourself —
> or use `on_event`, which always sees every event regardless.

---

### Diagnostics

#### `ping(timeout=0.5) -> bool`

`True` if the device answers a loopback. Touches the USB link only, never the
LIN bus.

#### `loopback(payload=b"KanaLin") -> bytes`

Echo up to 128 bytes through the device's own framing code. A full round trip
through both encoders, without touching the bus — the fastest way to tell "the
link is broken" from "the bus is quiet".

#### `read_uart_registers() -> UartRegisters`

Dump the raw UART5 baud-rate registers. Use it when a rate is accepted but the
bus looks wrong: it lets you recompute the real rate **from the silicon**,
instead of trusting the same HAL arithmetic that programmed it.

```python
regs = lin.read_uart_registers()
print(regs.actual_baudrate, regs.bit_time_us)   # 19200.0  52.1
```

---

### Timestamps

`LinFrame.timestamp_us` is a **32-bit** microsecond counter, so it wraps back to
zero every ~71.6 minutes. Under that horizon you can subtract two values
directly; over it you cannot, and a long capture sees time jump backwards.

#### `delta_us(earlier, later) -> int`

Stateless gap between two timestamps, correct across one wrap. Use it for
periods and durations.

```python
from kanaliblin import delta_us

gap = delta_us(previous.timestamp_us, frame.timestamp_us)
```

#### `TimestampUnwrapper`

Stateful: turns the wrapping counter into a continuous timeline, taking the
first value it sees as the origin.

```python
from kanaliblin import TimestampUnwrapper

timeline = TimestampUnwrapper()
for frame in lin.events():
    print(f"{timeline.seconds(frame.timestamp_us):9.6f}  {frame}")
```

| Member | |
|---|---|
| `unwrap(ts)` / `__call__(ts)` | continuous microseconds since the first value |
| `seconds(ts)` | the same, in seconds |
| `wraps` | how many times the counter has rolled over |
| `reset()` | forget the timeline — **call it after reconnecting** |

Feed it every frame **in arrival order**: a value lower than the previous one is
read as a wrap. Frames arrive in order from the device, so consuming them as
they come is fine. Instances are thread-safe.

> A device reset is indistinguishable from a wrap — the counter simply starts
> near zero again — and would add a phantom ~71 minutes to the timeline. Call
> `reset()` whenever you reconnect.

Also exported: `TIMESTAMP_MODULUS` (2³²) and `TIMESTAMP_WRAP_SECONDS` (≈4295 s).

---

### Event objects

#### `LinFrame`

A complete frame observed on the bus.

| Attribute | Type | |
|---|---|---|
| `frame_id` | `int` | 6-bit identifier |
| `data` | `bytes` | 1–8 data bytes |
| `dlc` | `int` | `len(data)` |
| `checksum_ok` | `bool` | LIN checksum verified by the device |
| `timestamp_us` | `int` | µs, captured **in the USART interrupt** at the frame's break |

`timestamp_us` comes from the MCU, not the host, so it is free of USB/Windows
jitter — always measure periods with it. It wraps every ~71 minutes.

For frames published by another node the device cannot know which checksum
flavour was used, so it accepts **classic or enhanced** (except identifiers
`0x3C`/`0x3D`, always classic by LIN rule).

#### `HeaderOnly`

A header went out and no node published a response. Has `frame_id`. Without a
second ECU this is the expected result of `master_send_header()` for an
unregistered identifier; it also sets the sticky `TIMEOUT` flag, which is normal
here.

#### `Status`

`mode` ([`Mode`](#enumerations-and-constants)), `bus_state` (`BusState.IDLE` /
`ACTIVE` — a frame in the last second), `errors` ([`ErrorFlags`](#errorflags)).

#### `UartRegisters`

`kernel_clock_hz`, `prescaler`, `brr`, `cr1`, `requested_baudrate`, plus computed
`actual_baudrate`, `bit_time_us`, `error_percent`, `over8`, `usartdiv`,
`prescaler_div`.

#### `Stats`

`bytes_rx`, `bytes_tx`, `frames_decoded`, `events_received`, `events_dropped`
(queue full — nobody consuming), `events_discarded` (consumed by a waiting call
that did not want them), `stale_replies` (answer to a call that already timed
out), `malformed`.

#### `ScheduleEntry`

`ScheduleEntry(frame_id, delay_ms)` — a named alternative to a plain tuple.

---

### Enumerations and constants

```python
Mode.IDLE / MASTER / SLAVE
Checksum.CLASSIC / ENHANCED
BusState.IDLE / ACTIVE
ErrorFlags.TIMEOUT / CHECKSUM / FRAMING / SHORT_BREAK      # IntFlag, combinable
Command.<name>, Event.<name>                               # raw protocol numbering
```

| Constant | Value | |
|---|---|---|
| `USB_VID` / `USB_PID` | `0x0483` / `0x5710` | CDC ACM interface |
| `DEFAULT_BAUDRATE` | `19200` | boot rate, restored on host disconnect |
| `BAUDRATE_MIN` / `BAUDRATE_MAX` | `1000` / `20000` | accepted range |
| `MAX_SCHEDULE_ENTRIES` | `32` | schedule capacity |

#### `ErrorFlags`

| Flag | Meaning |
|---|---|
| `TIMEOUT` | no response to a header, or frame closed by silence. **Normal** after `master_send_header()` with no responder |
| `CHECKSUM` | frame received with a bad LIN checksum |
| `FRAMING` | framing/bit error on the wire |
| `SHORT_BREAK` | break shorter than the configured 11 bits |

---

### Exceptions

All derive from `KanaLinError`.

| Exception | Raised when |
|---|---|
| `DeviceNotFound` | no device matches the VID/PID, or the port cannot be opened |
| `CommandRejected` | the device answered NACK. `.command` says which command; the message includes the usual reasons |
| `ResponseTimeout` | no reply within `timeout` |
| `LinkError` | the serial link failed or was closed mid-call |

Bad arguments raise plain `ValueError` **before** anything is sent (bad
identifier, 0 or >8 data bytes, out-of-range baud rate, oversized schedule).

```python
from kanaliblin import CommandRejected

try:
    lin.master_send_frame(0x10, b"\x01\x02")
except CommandRejected as exc:
    print(exc)   # device rejected MASTER_SEND_FRAME (the channel is not in MASTER mode, ...)
```

---

### Low-level framing

Usable without a device — build or parse KanaLin frames offline, batch several
commands into one write, or decode a captured stream.

```python
from kanaliblin import encode, StreamDecoder, crc8_sae_j1850, lin_pid, lin_checksum

raw = encode(0x30)                      # GET_STATUS on the wire
frames = StreamDecoder().feed(raw)      # [RawFrame(cmd=0x30, payload=b'')]
```

Wire format: `0x7E LEN CMD PAYLOAD… CRC8 0x7E`, with `0x7E`/`0x7D` escaped as
`0x7D` + (byte XOR 0x20), `LEN` counting `CMD + PAYLOAD`, and `CRC8` =
CRC-8/SAE-J1850 over `CMD + PAYLOAD`.

`kanaliblin.framing` also exposes `build_*` helpers (`build_set_mode`,
`build_master_send_frame`, `build_schedule_load`, …) that return the encoded
bytes for one command.

---

## Recipes

**Poll a set of identifiers and print what answers**

```python
lin.mode = Mode.MASTER
for frame_id in (0x10, 0x11, 0x12):
    answer = lin.master_send_header(frame_id)
    print(f"0x{frame_id:02X}: {answer if answer else 'no reply'}")
```

**Emulate an ECU that answers a real master**

```python
lin.reset()
lin.slave_set_response(0x30, b"\x01\x02\x03\x04")
lin.slave_set_response(0x31, b"\xff")
lin.mode = Mode.SLAVE
for event in lin.events():          # runs until interrupted
    print(event)
```

**Measure the real frame duration (MCU timestamps, not the PC clock)**

```python
stamps = []
lin.mode = Mode.MASTER
lin.slave_set_response(0x10, b"\x00" * 8)
with lin.schedule([(0x10, 0)]):     # delay 0: the pace becomes the frame duration
    for event in lin.events(timeout=1.0):
        if isinstance(event, LinFrame) and event.frame_id == 0x10:
            stamps.append(event.timestamp_us)

gaps = [b - a for a, b in zip(stamps, stamps[1:])]
print("frame duration:", min(gaps), "us")   # min, not median — see below
```

Use the **minimum**, not the median: if the host misses an event, that gap comes
out doubled and drags a median with it. The minimum stays right as long as two
consecutive frames arrive.

**Talk UDS to a slave ECU**

Diagnostics ride on the LIN transport layer: identifier `0x3C` carries the
request, `0x3D` the response, both 8 bytes with the **classic** checksum, and
the service bytes are preceded by the node address and a PCI byte. The slave
only answers when polled, so a request is "publish on 0x3C, then poll 0x3D".

```python
NAD = 0x64
lin.mode = Mode.MASTER

# TesterPresent (3E 00) as a single frame, padded to 8 bytes.
# No checksum argument needed: 0x3C defaults to classic, as the standard requires.
request = bytes([NAD, 0x02, 0x3E, 0x00]) + b"\xff" * 4
lin.master_send_frame(0x3C, request)

deadline = time.monotonic() + 1.5          # the ECU's P2 timeout
while time.monotonic() < deadline:
    answer = lin.master_send_header(0x3D)
    if isinstance(answer, LinFrame) and answer.data[0] == NAD:
        print("response:", answer.data[2:2 + (answer.data[1] & 0x0F)].hex(" "))
        break
    time.sleep(0.017)
```

`examples/06_uds_diagnostics.py` wraps this into a small UDS client that also
handles negative responses, the "response pending" code, and multi-frame
replies.

> Requests are limited to **6 payload bytes**, the most a single frame carries.
> Longer ones need segmentation, which this library deliberately leaves to you —
> see [What you would have to write yourself](#what-you-would-have-to-write-yourself).

**Change baud rate and confirm it on the wire**

```python
lin.set_baudrate(9600)
regs = lin.read_uart_registers()
print(f"{regs.actual_baudrate:.0f} baud, {regs.bit_time_us:.1f} us per bit")
```

**Log every frame to CSV from the callback**

```python
import csv

writer = csv.writer(open("bus.csv", "w", newline=""))

def log(event):
    if isinstance(event, LinFrame):
        writer.writerow([event.timestamp_us, f"0x{event.frame_id:02X}",
                         event.data.hex(), int(event.checksum_ok)])

with KanaLin.open(on_event=log) as lin:
    lin.reset()
    lin.mode = Mode.SLAVE
    time.sleep(60)
```

---

## Threading model

- A **daemon reader thread** owns the serial port for reading and runs from
  construction to `close()`.
- `on_event` is called **on that thread**, before the event is queued. Keep it
  short — print, append, put on a queue. Exceptions in it are logged and
  swallowed so a broken callback cannot kill the reader.
- Public methods are safe to call from several threads: each command holds an
  internal lock across its write and its reply, so two threads cannot steal each
  other's answers.
- If the link dies, the reader stops and every subsequent call raises
  `LinkError` instead of hanging.

---

## Logging

The library logs through the standard `logging` module, under the `kanalin`
logger. You do **not** need to configure anything to see the important messages:
Python's last-resort handler prints `WARNING` and above to stderr on its own.
That covers the ones that matter — an enhanced checksum on a diagnostic
identifier, a callback that raised, a reader thread that died.

To route them somewhere else, or to see the quieter `INFO` messages (such as
"several devices found, using the first"):

```python
import logging

logging.basicConfig(level=logging.INFO)
logging.getLogger("kanalin").setLevel(logging.DEBUG)
```

To silence the library entirely:

```python
logging.getLogger("kanalin").setLevel(logging.CRITICAL)
```

---

## Reconnecting

> **Not implemented yet.** There is no automatic reconnection, and an instance
> is not reusable after the link drops. This section describes what an
> application has to do today, so that code written now keeps working when the
> library grows a `reopen()`.

When the device is unplugged (or the port disappears) the reader thread stops
and every later call raises [`LinkError`](#exceptions). Recovery means building
a new instance:

```python
def connect() -> KanaLin:
    lin = KanaLin.open()
    lin.reset()
    lin.set_baudrate(19230)
    lin.mode = Mode.MASTER
    return lin

lin = connect()
while True:
    try:
        frame = lin.master_send_frame(0x10, payload)
    except LinkError:
        lin.close()                 # releases the port and the reader thread
        timeline.reset()            # the MCU's clock restarted; see Timestamps
        time.sleep(1.0)             # let the device re-enumerate
        lin = connect()
```

Three things to get right, and they are easy to miss:

1. **Reapply your configuration.** Unplugging *does* deconfigure USB, which is
   exactly what makes the firmware drop its state: back to `IDLE`, schedule
   table cleared, slave responses cleared, baud rate back to 19200. This is the
   opposite of merely closing the port, which changes nothing. So a reconnect
   needs the same setup as a first connect — that is why the snippet puts it in
   a function.
2. **Reset any `TimestampUnwrapper`.** The MCU's counter starts over, and an
   unwrapper reads that as a wrap.
3. **Re-register your `on_event` callback** by passing it to the new instance;
   nothing is carried over.

Detecting the drop *without* sending anything: `lin.is_open` goes false, and
`ping()` returns `False` rather than raising. A monitoring application that only
listens should poll one of those, since it may not issue a command for minutes.

---

## Troubleshooting

**`DeviceNotFound`** — the device is not enumerated, or another program holds
the port. Check `KanaLin.find_ports()`, and Device Manager for a COM port under
VID `0483` / PID `5710`.

**`ping()` works but nothing appears on the bus** — the USB side is fine and the
LIN side is not. In order: LIN supply (VBAT/VSUP) and the bus pull-up; the
transceiver's enable pin; then the wiring. The firmware drives `SLP_N` (PB2)
high at start-up, and with it low the node cannot even see its own frames.

**`master_send_frame()` returns `None`** — no echo came back. Same list as
above; the command was still accepted, so the break was requested.

**Every frame shows `checksum_ok=False`** — the publisher is probably using the
other checksum flavour. Pass `checksum=Checksum.CLASSIC` when you publish.
Identifiers `0x3C`/`0x3D` are always classic.

**`CommandRejected` on something that worked before** — most rejections are a
state precondition, not a malformed payload: not in `MASTER` mode, the schedule
still running, a transmission in flight. The exception message lists them.

**Odd results across runs** — you skipped `reset()`. The device kept the previous
run's mode, schedule and slave responses.

**`stats.events_dropped` climbing** — nobody is consuming events. Use `on_event`,
consume faster, or raise `event_queue_size`. Note the *firmware* also drops
events if the host stops reading the port; the reader thread here prevents that
as long as the process is alive.

---

## Testing without hardware

```bash
python -m kanaliblin.selftest       # framing, checksums, event parsing
python tests/test_offline.py     # the API against a simulated firmware
```

Both run offline: the first checks the framing layer against known values (CRC
check value, PID parity, escaping, a decoder fed deliberately dirty streams);
the second drives the full API against a fake serial port that answers like the
firmware, covering reply matching, event routing and the reader thread.

---

## Scope and limits

Read this before designing an application around the library. Nothing here is a
bug report — these are deliberate boundaries, and knowing where they are saves
you from discovering one halfway through a project.

### What you can build with it today

Master applications sending your own PDUs; a UDS client; an ECU simulator
answering as a slave; a passive bus logger or monitor; a test bench driving
periodic traffic from the embedded scheduler; bus and baud-rate diagnostics.
All of that uses the API as documented above, with no extra plumbing.

### What you would have to write yourself

**Multi-frame transmission (segmentation).** The LIN transport layer splits
payloads longer than 6 bytes into a first frame plus consecutive frames. This
library sends **raw frames only**, deliberately, so that it serves your own PDU
formats and UDS equally well. Practical consequence: you can send a UDS request
of at most **6 payload bytes** as a single frame. Anything longer — a
`WriteDataByIdentifier`, a `SecurityAccess` key, a `RoutineControl` with
parameters — needs segmentation code you write on top. *Reception* is different:
`examples/06_uds_diagnostics.py` already reassembles multi-frame responses, so
only the sending direction is missing.

### What the device cannot do

These are firmware limits. No amount of Python works around them:

- **No deliberate error injection.** You cannot emit a frame with a wrong
  checksum, a short break or a framing error. If your test plan includes
  verifying that an ECU *detects* corrupted traffic, this device cannot produce
  it.
- **Wake-up detection does not exist.** The device *sends* the go-to-sleep frame
  and the wake-up pulse (see [Bus sleep and wake-up](#bus-sleep-and-wake-up)),
  and it recognises a sleep request on the bus — but it cannot tell you that
  *another* node requested a wake-up. The USART reports a wake-up pulse as an
  ordinary LIN break and gives no way to measure the dominant width, and the two
  overlap: at 19200 a break is ≥ 573 µs while a wake-up pulse may be anywhere in
  250 µs – 5 ms. A pulse shorter than 573 µs would not even register as a break.
  If your test plan depends on observing an ECU-initiated wake-up, you need
  external instrumentation.
- **Event loss is silent.** The device drops bus events if its ~1 KB output
  buffer fills, and reports no counter for it. `stats` only counts losses on the
  host side, so a monitoring application cannot tell "the bus was quiet" from "I
  missed frames". At LIN speeds this needs the host to stall for hundreds of
  milliseconds, which does happen on Windows.
- **No firmware version query**, so an application cannot check what it is
  talking to.
- **The schedule table cannot be read back** — track what you loaded.
- **No autobaud.** Set the rate explicitly with `set_baudrate()`.

### Fixed limits

| | |
|---|---|
| Data bytes per frame | 1–8 (`dlc = 0` is rejected on purpose) |
| Schedule slots | 32, headers only, no reload while running |
| Baud rate | 1000–20000 accepted; below ~2200 the divider overflows and the command is rejected. Volatile: a device reset returns to 19200 |
| Frame identifiers | 0x00–0x3F; `0x3C`/`0x3D` default to the classic checksum |
| Loopback payload | 128 bytes |

### Not yet implemented in this library

- **Automatic reconnection** — see [Reconnecting](#reconnecting) for the pattern
  to use meanwhile.
- **Type annotations are shipped but not machine-verified.** They all resolve at
  runtime (no undefined or circular references), which is what `py.typed`
  consumers need to avoid import errors, but no mypy or pyright run has checked
  them for consistency. If your project relies on them, run a type checker once
  against the package.

### Validation status of the device

The firmware was developed and validated with a **single node on the bus**,
using the transceiver's echo of its own transmissions. Master mode, framing, the
scheduler and baud-rate switching are confirmed on hardware. The slave path and
the reception of frames published by *another* node are implemented but have
only ever run against this node's own traffic. Treat the first session against a
real ECU as new territory, not as a regression check — and reach for
`get_status()` and the `-v` style frame dumps early if something looks off.
