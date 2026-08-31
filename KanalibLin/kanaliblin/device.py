"""
High-level interface to the KanaLin LIN channel.

The device exposes LIN over a USB CDC ACM interface (a virtual COM port); this
module turns that byte stream into ordinary Python calls. A background thread
reads the port continuously, so bus events are never missed while your code is
doing something else.
"""

from __future__ import annotations

import logging
import queue
import struct
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, Sequence

from . import framing
from .errors import CommandRejected, DeviceNotFound, KanaLinError, LinkError, ResponseTimeout
from .events import BusEvent, HeaderOnly, LinFrame, SleepRequested, Status, UartRegisters
from .framing import RawFrame, StreamDecoder
from .protocol import (
    BAUDRATE_MAX,
    BAUDRATE_MIN,
    DEFAULT_BAUDRATE,
    DIAGNOSTIC_FRAME_IDS,
    HOST_SERIAL_BAUDRATE,
    MASTER_REQUEST_ID,
    MAX_DATA_LEN,
    MAX_FRAME_ID,
    MAX_PAYLOAD,
    MAX_SCHEDULE_ENTRIES,
    MIN_DATA_LEN,
    USB_PID,
    USB_VID,
    Checksum,
    Command,
    Event,
    Mode,
    default_checksum,
    is_goto_sleep,
)

try:  # pyserial is only needed to talk to real hardware
    import serial
    import serial.tools.list_ports as list_ports
except ImportError:  # pragma: no cover
    serial = None  # type: ignore[assignment]
    list_ports = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

__all__ = ["KanaLin", "ScheduleEntry", "Stats"]


@dataclass(frozen=True)
class ScheduleEntry:
    """
    One slot of the firmware schedule table.

    ``delay_ms`` is the pause *after* sending this slot, before the next one.
    The table is circular, so two slots of 30 ms make each identifier repeat
    every ~60 ms.
    """

    frame_id: int
    delay_ms: int


@dataclass
class Stats:
    """Counters kept by the reader thread. Useful when something looks off."""

    bytes_rx: int = 0
    bytes_tx: int = 0
    frames_decoded: int = 0
    events_received: int = 0
    events_dropped: int = 0  #: queue full: nobody was consuming events
    events_discarded: int = 0  #: consumed by a waiting call that did not want them
    stale_replies: int = 0  #: replies to a call that had already timed out
    malformed: int = 0  #: well-framed but undecodable payloads
    sleep_requests: int = 0  #: go-to-sleep commands recognised on the bus

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"rx={self.bytes_rx}B tx={self.bytes_tx}B frames={self.frames_decoded} "
            f"events={self.events_received} dropped={self.events_dropped} "
            f"discarded={self.events_discarded} stale={self.stale_replies} "
            f"malformed={self.malformed} sleep_requests={self.sleep_requests}"
        )


class KanaLin:
    """
    A connected KanaLin device.

    ::

        from kanaliblin import KanaLin, Mode

        with KanaLin.open() as lin:
            lin.reset()
            lin.mode = Mode.MASTER
            frame = lin.master_send_frame(0x10, b"\\x01\\x02\\x03\\x04")
            print(frame)

    Always :meth:`reset` at the start of a script unless you deliberately want
    to inherit state: mode, schedule table and slave responses survive closing
    the COM port, because closing a port does not deconfigure USB.
    """

    def __init__(
        self,
        port: str,
        *,
        timeout: float = 1.0,
        on_event: Callable[[BusEvent], None] | None = None,
        on_sleep_requested: Callable[[SleepRequested], None] | None = None,
        event_queue_size: int = 4096,
    ) -> None:
        """
        :param port: COM port / tty of the CDC ACM interface (``KanaLin.open()``
            finds it for you).
        :param timeout: default seconds to wait for a command reply.
        :param on_event: called for **every** bus event, from the reader thread,
            before it is queued. Keep it short; exceptions in it are logged and
            swallowed so they cannot kill the reader.
        :param on_sleep_requested: called when a go-to-sleep command is seen on
            the bus, *in addition to* the ordinary frame event -- the frame is
            still delivered through ``on_event`` and the queue. Purely passive:
            KanaLin never sleeps and never touches the transceiver.
        :param event_queue_size: bus events buffered for :meth:`read_event`.
            When it overflows the oldest event is dropped and
            :attr:`stats`\\ ``.events_dropped`` counts it.
        """
        if serial is None:  # pragma: no cover
            raise KanaLinError("pyserial is required to talk to a device: pip install pyserial")

        self.timeout = timeout
        self.on_event = on_event
        self.on_sleep_requested = on_sleep_requested
        self.stats = Stats()

        self._serial = serial.Serial(port, baudrate=HOST_SERIAL_BAUDRATE, timeout=0.02)
        self._decoder = StreamDecoder()
        self._events: queue.Queue[tuple[float, BusEvent]] = queue.Queue(event_queue_size)
        self._replies: queue.Queue[RawFrame] = queue.Queue()
        self._write_lock = threading.Lock()
        self._call_lock = threading.RLock()
        self._stop = threading.Event()
        self._link_error: BaseException | None = None

        # Cached local view of device state. The device is authoritative --
        # get_status() re-reads the mode from it.
        self._mode = Mode.IDLE
        self._baudrate = DEFAULT_BAUDRATE
        self._registered_ids: set[int] = set()

        self._reader = threading.Thread(target=self._reader_loop, name="kanalin-reader", daemon=True)
        self._reader.start()

    # ------------------------------------------------------------------
    # Discovery / lifecycle
    # ------------------------------------------------------------------
    @staticmethod
    def find_ports(vid: int = USB_VID, pid: int = USB_PID,
                   serial_number: str | None = None) -> list[str]:
        """Return every serial port matching the KanaLin VID/PID, in system order.

        :param serial_number: keep only the board with this USB serial. Every
            board reports one derived from its MCU's unique device ID, which is
            the only thing that tells two KanaLin boards apart -- VID/PID and
            the COM port number do not (the latter can change between plug-ins).
            Use :meth:`list_devices` to see which serials are attached.
        """
        if list_ports is None:  # pragma: no cover
            raise KanaLinError("pyserial is required to enumerate ports: pip install pyserial")
        return [p.device for p in list_ports.comports()
                if p.vid == vid and p.pid == pid
                and (serial_number is None or p.serial_number == serial_number)]

    @staticmethod
    def list_devices(vid: int = USB_VID, pid: int = USB_PID) -> list[tuple[str, str]]:
        """Return ``(port, serial_number)`` for every KanaLin board attached."""
        if list_ports is None:  # pragma: no cover
            raise KanaLinError("pyserial is required to enumerate ports: pip install pyserial")
        return [(p.device, p.serial_number or "")
                for p in list_ports.comports() if p.vid == vid and p.pid == pid]

    @classmethod
    def open(cls, port: str | None = None, *, serial_number: str | None = None,
             **kwargs) -> "KanaLin":
        """
        Open a device, auto-detecting the port when it is not given.

        :param serial_number: pick the board with this USB serial instead of the
            first one found. Required to be deterministic once more than one
            board is plugged in.
        :raises DeviceNotFound: nothing matching the KanaLin VID/PID is present.
        """
        if port is None:
            ports = cls.find_ports(serial_number=serial_number)
            if not ports:
                extra = (f" with serial {serial_number!r}"
                         if serial_number is not None else "")
                raise DeviceNotFound(
                    f"no KanaLin device found{extra} (looking for VID 0x{USB_VID:04X} "
                    f"PID 0x{USB_PID:04X}); is it plugged in and enumerated?"
                )
            if len(ports) > 1:
                log.info("several KanaLin devices found (%s), using %s; "
                         "pass serial_number= to choose", ports, ports[0])
            port = ports[0]
        try:
            return cls(port, **kwargs)
        except Exception as exc:  # pyserial raises SerialException
            if serial is not None and isinstance(exc, serial.SerialException):
                raise DeviceNotFound(f"cannot open {port}: {exc}") from exc
            raise

    def close(self) -> None:
        """
        Stop the reader thread and close the port. Safe to call twice.

        This does not reset the device: mode, schedule and slave responses stay
        as they are, because closing a port does not deconfigure USB. Unplugging
        it does, and that *is* what makes the firmware drop them.

        An instance is not reusable after closing -- there is no reconnect;
        construct a new one.
        """
        if self._stop.is_set():
            return
        self._stop.set()
        self._reader.join(timeout=2.0)
        try:
            self._serial.close()
        except Exception:  # pragma: no cover - closing must never raise
            pass

    def __enter__(self) -> "KanaLin":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def port(self) -> str:
        return self._serial.port

    @property
    def is_open(self) -> bool:
        """
        Whether the device is still usable.

        False after :meth:`close`, and also once the reader thread has hit a
        link failure -- pyserial's own ``is_open`` can stay True after the
        hardware disappears, so that alone is not a reliable signal.
        """
        return not self._stop.is_set() and self._link_error is None and self._serial.is_open

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<KanaLin {self.port} mode={self._mode.name} baud={self._baudrate}>"

    # ------------------------------------------------------------------
    # Reader thread
    # ------------------------------------------------------------------
    def _reader_loop(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self._serial.read(256)
            except Exception as exc:
                if not self._stop.is_set():
                    self._link_error = exc
                    log.error("KanaLin reader stopped: %s", exc)
                return
            if not chunk:
                continue
            self.stats.bytes_rx += len(chunk)
            for raw in self._decoder.feed(chunk):
                self.stats.frames_decoded += 1
                self._dispatch(raw)

    def _dispatch(self, raw: RawFrame) -> None:
        if raw.cmd not in (Event.FRAME_RX, Event.HEADER_ONLY):
            self._replies.put(raw)  # ACK / NACK / STATUS / LOOPBACK / UART_REGS
            return

        try:
            event: BusEvent = (
                LinFrame.from_payload(raw.payload)
                if raw.cmd == Event.FRAME_RX
                else HeaderOnly.from_payload(raw.payload)
            )
        except ValueError as exc:
            self.stats.malformed += 1
            log.warning("undecodable event 0x%02X: %s", raw.cmd, exc)
            return

        self.stats.events_received += 1

        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:  # a broken callback must not kill the reader
                log.exception("on_event callback raised")

        # A sleep request is not a separate event on the wire: it is this very
        # frame, recognised here. The frame itself still goes to the queue
        # untouched, so a bus monitor sees exactly what crossed the bus.
        if (
            isinstance(event, LinFrame)
            and event.checksum_ok
            and is_goto_sleep(event.frame_id, event.data)
        ):
            self.stats.sleep_requests += 1
            if self.on_sleep_requested is not None:
                try:
                    self.on_sleep_requested(SleepRequested(event))
                except Exception:
                    log.exception("on_sleep_requested callback raised")

        item = (time.monotonic(), event)
        try:
            self._events.put_nowait(item)
        except queue.Full:
            try:
                self._events.get_nowait()  # drop the oldest, keep the freshest
                self.stats.events_dropped += 1
            except queue.Empty:  # pragma: no cover - racing consumer
                pass
            try:
                self._events.put_nowait(item)
            except queue.Full:  # pragma: no cover - racing producer
                self.stats.events_dropped += 1

    def _check_link(self) -> None:
        if self._link_error is not None:
            raise LinkError(f"serial link failed: {self._link_error}")
        if self._stop.is_set():
            raise LinkError("device is closed")

    # ------------------------------------------------------------------
    # Command plumbing
    # ------------------------------------------------------------------
    def _write(self, data: bytes) -> None:
        self._check_link()
        with self._write_lock:
            self._serial.write(data)
            self._serial.flush()
        self.stats.bytes_tx += len(data)

    def _wait_reply(self, match: Callable[[RawFrame], bool], timeout: float, what: str) -> RawFrame:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._check_link()
                raise ResponseTimeout(f"no {what} from the device within {timeout:g} s")
            try:
                reply = self._replies.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                self._check_link()
                continue
            if match(reply):
                return reply
            # A reply to an earlier call that already gave up. Dropping it keeps
            # the queue from drifting one answer behind forever.
            self.stats.stale_replies += 1

    def _command(self, cmd: Command, payload: bytes = b"", *, timeout: float | None = None) -> None:
        """Send a command and wait for its ACK. NACK raises :class:`CommandRejected`."""
        expected = bytes((cmd,))
        with self._call_lock:
            self._write(framing.encode(cmd, payload))
            reply = self._wait_reply(
                lambda f: f.cmd in (Event.ACK, Event.NACK) and f.payload == expected,
                self.timeout if timeout is None else timeout,
                f"ACK/NACK for {Command(cmd).name}",
            )
        if reply.cmd == Event.NACK:
            raise CommandRejected(cmd)

    def _query(
        self, cmd: Command, event: Event, payload: bytes = b"", *, timeout: float | None = None
    ) -> RawFrame:
        """Send a command whose reply is an event rather than an ACK."""
        expected = bytes((cmd,))
        with self._call_lock:
            self._write(framing.encode(cmd, payload))
            reply = self._wait_reply(
                lambda f: f.cmd == event or (f.cmd == Event.NACK and f.payload == expected),
                self.timeout if timeout is None else timeout,
                f"{Event(event).name} for {Command(cmd).name}",
            )
        if reply.cmd == Event.NACK:
            raise CommandRejected(cmd)
        return reply

    def _default_bus_wait(self) -> float:
        """
        How long to wait for the bus event a command should produce.

        Scaled with the baud rate: at 1000 baud a header with no response takes
        ~20 character times to time out, which is 200 ms, so a fixed value
        would be wrong at the low end of the range.
        """
        return max(0.15, 800.0 / self._baudrate)

    # ------------------------------------------------------------------
    # Bus events
    # ------------------------------------------------------------------
    def read_event(self, timeout: float | None = 0.0) -> BusEvent | None:
        """
        Pop the next bus event, or ``None`` if none arrives within ``timeout``.

        ``timeout=0`` polls, ``timeout=None`` blocks forever.
        """
        try:
            if timeout == 0:
                return self._events.get_nowait()[1]
            return self._events.get(timeout=timeout)[1]
        except queue.Empty:
            self._check_link()
            return None

    def events(self, timeout: float | None = None, *, idle_timeout: float = 0.5) -> Iterator[BusEvent]:
        """
        Iterate over bus events for ``timeout`` seconds (``None`` = forever).

        Stops early if nothing arrives for ``idle_timeout`` seconds::

            for event in lin.events(timeout=2.0):
                print(event)
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if deadline is None:
                wait = idle_timeout
            else:
                wait = min(idle_timeout, deadline - time.monotonic())
                if wait <= 0:
                    return
            event = self.read_event(timeout=wait)
            if event is None:
                if deadline is None:
                    continue
                return
            yield event

    def flush_events(self) -> int:
        """Discard queued bus events and return how many were dropped."""
        dropped = 0
        while True:
            try:
                self._events.get_nowait()
                dropped += 1
            except queue.Empty:
                return dropped

    def wait_frame(self, frame_id: int | None = None, timeout: float = 1.0) -> BusEvent | None:
        """
        Wait for the next bus event, optionally for one identifier.

        Returns a :class:`~kanaliblin.events.LinFrame`, a
        :class:`~kanaliblin.events.HeaderOnly`, or ``None`` on timeout. Events for
        other identifiers seen while waiting are discarded (counted in
        :attr:`stats`\\ ``.events_discarded``).
        """
        return self._wait_bus_event(frame_id, timeout, since=0.0)

    def _wait_bus_event(
        self, frame_id: int | None, timeout: float, since: float
    ) -> BusEvent | None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                arrived, event = self._events.get(timeout=remaining)
            except queue.Empty:
                self._check_link()
                return None
            if arrived < since:
                self.stats.events_discarded += 1  # older than the command we sent
                continue
            if frame_id is None or event.frame_id == frame_id:
                return event
            self.stats.events_discarded += 1

    # ------------------------------------------------------------------
    # Channel configuration
    # ------------------------------------------------------------------
    @property
    def mode(self) -> Mode:
        """Cached channel role. Assigning to it calls :meth:`set_mode`."""
        return self._mode

    @mode.setter
    def mode(self, mode: Mode) -> None:
        self.set_mode(mode)

    def set_mode(self, mode: Mode) -> None:
        """
        Select the channel role.

        In ``IDLE`` the channel neither transmits nor reports bus events. Any
        mode change also stops the schedule (the loaded table is kept, so a
        :meth:`schedule_start` relaunches it).
        """
        mode = Mode(mode)
        self._command(Command.SET_MODE, bytes((mode,)))
        self._mode = mode

    @property
    def baudrate(self) -> int:
        """Cached LIN bus baud rate. Assigning to it calls :meth:`set_baudrate`."""
        return self._baudrate

    @baudrate.setter
    def baudrate(self, baudrate: int) -> None:
        self.set_baudrate(baudrate)

    def set_baudrate(self, baudrate: int) -> None:
        """
        Reconfigure UART5 at runtime.

        The device verifies the rate by reading it back from the peripheral and
        rejects the command if it cannot be reached, so an accepted call means
        the USART really was reprogrammed. Rejected while the schedule is
        running or a transmission is in flight -- stop them first.

        The change is volatile: a device reset returns to 19200 baud.
        """
        if not BAUDRATE_MIN <= baudrate <= BAUDRATE_MAX:
            raise ValueError(f"baud rate must be {BAUDRATE_MIN}..{BAUDRATE_MAX}, got {baudrate}")
        self._command(Command.SET_BAUDRATE, struct.pack("<I", baudrate))
        self._baudrate = baudrate

    def get_status(self) -> Status:
        """
        Read mode, bus state and the sticky error flags.

        **Reading clears the error flags**, so each call reports what happened
        since the previous one.
        """
        reply = self._query(Command.GET_STATUS, Event.STATUS)
        status = Status.from_payload(reply.payload)
        self._mode = status.mode
        return status

    def reset(self, *, baudrate: int = DEFAULT_BAUDRATE, clear_responses: bool = True) -> Status:
        """
        Put the device in a known state: IDLE, no schedule, no slave responses,
        default baud rate, error flags cleared.

        Call this at the start of every script. The firmware keeps its
        configuration for as long as it stays enumerated, and closing the COM
        port does **not** deconfigure USB -- so without a reset a run silently
        inherits whatever the previous one left behind (a loaded schedule, a
        registered slave response answering headers you expected to go
        unanswered).

        The commands are pipelined into a single write and the final
        ``GET_STATUS`` acts as the sentinel, so this costs one round trip
        rather than seventy.
        """
        with self._call_lock:
            batch = bytearray()
            batch += framing.build_set_mode(Mode.IDLE)
            batch += framing.encode(Command.SCHEDULE_STOP)
            batch += framing.build_schedule_load([])  # empties the table
            batch += framing.build_set_baudrate(baudrate)
            if clear_responses:
                for frame_id in range(MAX_FRAME_ID + 1):
                    batch += framing.build_slave_clear_response(frame_id)
            self._write(bytes(batch))

            self._mode = Mode.IDLE
            self._baudrate = baudrate
            self._registered_ids.clear()
            self.flush_events()

            # The batch produces one ACK per command; they are discarded while
            # waiting for the STATUS sentinel. That is expected here, so the
            # stale-reply counter is restored -- it exists to flag real trouble.
            stale = self.stats.stale_replies
            status = self.get_status()  # also clears err_flags
            self.stats.stale_replies = stale
        self.flush_events()
        return status

    # ------------------------------------------------------------------
    # Master
    # ------------------------------------------------------------------
    def master_send_frame(
        self,
        frame_id: int,
        data: bytes,
        *,
        checksum: Checksum | None = None,
        wait: float | None = None,
    ) -> BusEvent | None:
        """
        Publish a complete frame: break + sync + PID + data + checksum.

        Requires ``MASTER`` mode. ``data`` must be 1..8 bytes -- a zero-length
        frame is rejected because it could not be told apart from a bare header.

        ``checksum`` defaults to enhanced, except on the diagnostic identifiers
        0x3C and 0x3D where the LIN standard requires the classic form (see
        :func:`~kanaliblin.protocol.default_checksum`). Pass it explicitly to
        override.

        Returns the frame as observed on the bus (the transceiver echoes back
        what it transmits, so this is your own frame coming home, and its
        arrival is the proof the break really made it onto the wire), or
        ``None`` if no echo arrived within ``wait`` seconds. Pass ``wait=0`` to
        return immediately and collect events yourself.

        :raises CommandRejected: not in master mode, or a transmission is
            already in flight.
        """
        self._check_frame_id(frame_id)
        self._check_data(data)
        checksum = self._resolve_checksum(frame_id, checksum)
        payload = bytes((frame_id, len(data))) + bytes(data) + bytes((int(checksum),))
        with self._call_lock:
            sent = time.monotonic()
            self._command(Command.MASTER_SEND_FRAME, payload)
            return self._collect(frame_id, wait, sent)

    def master_send_header(self, frame_id: int, *, wait: float | None = None) -> BusEvent | None:
        """
        Send a header (break + sync + PID) and let another node publish the data.

        Requires ``MASTER`` mode. Returns the resulting
        :class:`~kanaliblin.events.LinFrame` if somebody answered, a
        :class:`~kanaliblin.events.HeaderOnly` if nobody did, or ``None`` if the
        device reported nothing within ``wait`` seconds.

        Note that a node that has a slave response registered for this
        identifier answers its own header -- including this device, which is how
        the slave path can be exercised without a second ECU.
        """
        self._check_frame_id(frame_id)
        with self._call_lock:
            sent = time.monotonic()
            self._command(Command.MASTER_SEND_HEADER, bytes((frame_id,)))
            return self._collect(frame_id, wait, sent)

    def _collect(self, frame_id: int, wait: float | None, sent: float) -> BusEvent | None:
        if wait == 0:
            return None
        return self._wait_bus_event(
            frame_id, self._default_bus_wait() if wait is None else wait, since=sent
        )

    # ------------------------------------------------------------------
    # Slave
    # ------------------------------------------------------------------
    def slave_set_response(
        self, frame_id: int, data: bytes, *, checksum: Checksum | None = None
    ) -> None:
        """
        Register the response this device publishes when it sees ``frame_id``.

        The checksum is computed by the firmware when you register the response,
        not when it answers, so the reply goes out inside the LIN timing window.
        As with :meth:`master_send_frame`, the flavour defaults to classic on
        the diagnostic identifiers and enhanced everywhere else.

        Two things surprise people here, both deliberate:

        * The table does **not** require ``SLAVE`` mode. It can be preloaded and
          survives mode changes; the device answers a registered identifier in
          any mode except ``IDLE``. That is what a real master publishing some
          of its own frames does, and it is what lets you validate the slave
          path by sending the matching header yourself.
        * The registration survives closing the port. Use :meth:`reset` or
          :meth:`slave_clear_response` rather than assuming a clean slate.
        """
        self._check_frame_id(frame_id)
        self._check_data(data)
        checksum = self._resolve_checksum(frame_id, checksum)
        payload = bytes((frame_id, len(data))) + bytes(data) + bytes((int(checksum),))
        self._command(Command.SLAVE_SET_RESPONSE, payload)
        self._registered_ids.add(frame_id)

    def slave_clear_response(self, frame_id: int) -> None:
        """Stop answering ``frame_id``."""
        self._check_frame_id(frame_id)
        self._command(Command.SLAVE_CLEAR_RESPONSE, bytes((frame_id,)))
        self._registered_ids.discard(frame_id)

    def slave_clear_all(self) -> None:
        """Clear every registered response, including ones left by other scripts."""
        with self._call_lock:
            batch = b"".join(
                framing.build_slave_clear_response(i) for i in range(MAX_FRAME_ID + 1)
            )
            self._write(batch)
            self._registered_ids.clear()
            stale = self.stats.stale_replies
            self.get_status()  # sentinel: everything before it has been processed
            self.stats.stale_replies = stale  # the 64 ACKs of the batch are expected

    @property
    def registered_responses(self) -> frozenset[int]:
        """Identifiers this instance registered a response for (best effort)."""
        return frozenset(self._registered_ids)

    # ------------------------------------------------------------------
    # Bus sleep / wake-up
    #
    # These act on the *bus*, at your request. KanaLin itself never sleeps:
    # its transceiver stays in Normal mode permanently and no call in this
    # section touches the SLP_N pin.
    # ------------------------------------------------------------------
    def send_goto_sleep(self, *, wait: float | None = None) -> BusEvent | None:
        """
        Tell the bus to go to sleep, with the standard LIN diagnostic frame.

        Sends identifier 0x3C carrying ``00 FF FF FF FF FF FF FF`` with the
        classic checksum, exactly as the LIN standard defines the go-to-sleep
        command. It is an ordinary frame in every respect, so it comes back
        through the transceiver echo like any other -- the returned
        :class:`~kanaliblin.events.LinFrame` is your own transmission, and its
        arrival is the proof the frame reached the wire.

        Requires ``MASTER`` mode: ordering the bus to sleep is the master's job.

        :raises CommandRejected: not in master mode, or a transmission is
            already in flight.
        """
        with self._call_lock:
            sent = time.monotonic()
            self._command(Command.SEND_GOTO_SLEEP)
            return self._collect(MASTER_REQUEST_ID, wait, sent)

    def send_wakeup(self) -> None:
        """
        Put a wake-up pulse on the bus: a dominant level of
        :data:`~kanaliblin.protocol.WAKEUP_PULSE_US` microseconds, then recessive.

        This is not a byte transmission. The firmware drives the TX pin as a
        plain GPIO for the duration, so the width does not move with the baud
        rate -- the LIN window is 250 us to 5 ms, and a byte-based trick would
        drift out of it at the low end of the range.

        Requires the channel not to be ``IDLE``, and the bus to be quiet: no
        schedule running, nothing in flight. A 1 ms dominant dropped on top of
        a frame in progress would destroy it, so the firmware refuses instead.
        Unlike :meth:`send_goto_sleep` this does **not** require master mode --
        a slave node may request a wake-up too.

        :raises CommandRejected: channel idle, schedule running, or the bus was
            busy. Stop the schedule and retry.
        """
        self._command(Command.SEND_WAKEUP)

    def wait_sleep_request(self, timeout: float = 1.0) -> SleepRequested | None:
        """
        Wait for a go-to-sleep command on the bus, or ``None`` on timeout.

        A polling alternative to the ``on_sleep_requested`` callback. It
        consumes bus events from the queue while it waits, like
        :meth:`wait_frame` does.

        Note that this matches your own :meth:`send_goto_sleep` as well, since
        the device observes its own frames -- which is how the detection can be
        exercised without a second ECU on the bus.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            event = self.read_event(timeout=remaining)
            if event is None:
                return None
            if (
                isinstance(event, LinFrame)
                and event.checksum_ok
                and is_goto_sleep(event.frame_id, event.data)
            ):
                return SleepRequested(event)
            self.stats.events_discarded += 1

    # ------------------------------------------------------------------
    # Schedule table
    # ------------------------------------------------------------------
    def schedule_load(self, entries: Sequence[ScheduleEntry | tuple[int, int]]) -> None:
        """
        Load the schedule table. An empty sequence clears it.

        Each entry is ``(frame_id, delay_ms)``: the delay is the pause *after*
        that slot, and the table wraps around. Only **headers** are sent -- for
        a slot to carry data, register a response for that identifier with
        :meth:`slave_set_response` (on this device or another node).

        Rejected while the schedule is running: stop it first, otherwise the
        running index would point into a table that is no longer the same one.
        There is no way to read the table back from the device.
        """
        items = [e if isinstance(e, ScheduleEntry) else ScheduleEntry(*e) for e in entries]
        if len(items) > MAX_SCHEDULE_ENTRIES:
            raise ValueError(
                f"schedule holds at most {MAX_SCHEDULE_ENTRIES} entries, got {len(items)}"
            )
        payload = bytearray((len(items),))
        for entry in items:
            self._check_frame_id(entry.frame_id)
            if not 0 <= entry.delay_ms <= 0xFFFF:
                raise ValueError(f"delay_ms must be 0..65535, got {entry.delay_ms}")
            payload += bytes((entry.frame_id,)) + struct.pack("<H", entry.delay_ms)
        self._command(Command.SCHEDULE_LOAD, bytes(payload))

    def schedule_start(self) -> None:
        """
        Start the embedded scheduler. Requires ``MASTER`` mode and a loaded table.

        The scheduler runs entirely in firmware, so its timing does not depend
        on USB or host scheduling latency. Use it whenever the cadence matters;
        :meth:`master_send_frame` in a Python loop is for interactive work.
        """
        self._command(Command.SCHEDULE_START)

    def schedule_stop(self) -> None:
        """Stop the scheduler. Idempotent, and a mode change stops it too."""
        self._command(Command.SCHEDULE_STOP)

    @contextmanager
    def schedule(
        self, entries: Sequence[ScheduleEntry | tuple[int, int]]
    ) -> Iterator["KanaLin"]:
        """
        Load, start and (on exit) stop a schedule::

            with lin.schedule([(0x10, 30), (0x11, 30)]):
                for event in lin.events(timeout=2.0):
                    print(event)

        The table stays loaded on exit -- only the scheduler is stopped.
        """
        self.schedule_load(entries)
        self.schedule_start()
        try:
            yield self
        finally:
            try:
                self.schedule_stop()
            except KanaLinError:  # pragma: no cover - link already gone
                log.warning("could not stop the schedule on exit", exc_info=True)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def loopback(self, payload: bytes = b"KanaLin") -> bytes:
        """
        Echo ``payload`` through the device without touching the LIN bus.

        A round trip through the firmware's own framing code, so it checks the
        USB link and both encoders end to end. Up to 128 bytes.
        """
        if len(payload) > MAX_PAYLOAD:
            raise ValueError(f"loopback payload must be <= {MAX_PAYLOAD} bytes")
        return self._query(Command.LOOPBACK, Event.LOOPBACK, bytes(payload)).payload

    def ping(self, timeout: float = 0.5) -> bool:
        """Return True if the device answers a loopback within ``timeout``."""
        probe = b"\x7e\x7d\x00\xff"  # bytes that exercise the escaping
        try:
            with self._call_lock:
                saved, self.timeout = self.timeout, timeout
                try:
                    return self.loopback(probe) == probe
                finally:
                    self.timeout = saved
        except (ResponseTimeout, LinkError):
            return False

    def read_uart_registers(self) -> UartRegisters:
        """
        Dump the raw UART5 baud-rate registers (KanaLin diagnostic extension).

        Use it when a baud rate is accepted but the bus looks wrong: it lets you
        recompute the real rate from the silicon instead of trusting the HAL
        arithmetic that programmed it.
        """
        return UartRegisters.from_payload(
            self._query(Command.GET_UART_REGS, Event.UART_REGS).payload
        )

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_checksum(frame_id: int, checksum: Checksum | None) -> Checksum:
        """
        Apply the per-identifier default, and warn about a likely mistake.

        An enhanced checksum on 0x3C/0x3D is almost always an oversight, and it
        fails in the worst possible way: the ECU drops the frame silently, while
        the local echo still reports ``checksum_ok`` because the device verifies
        its own transmissions against the flavour it was given. Hence the
        warning -- the call still goes through, in case it is deliberate.
        """
        if checksum is None:
            return default_checksum(frame_id)
        checksum = Checksum(checksum)
        if frame_id in DIAGNOSTIC_FRAME_IDS and checksum != Checksum.CLASSIC:
            log.warning(
                "enhanced checksum requested on diagnostic identifier 0x%02X; "
                "the LIN standard mandates the classic checksum there, so an ECU "
                "will ignore this frame without reporting anything",
                frame_id,
            )
        return checksum

    @staticmethod
    def _check_frame_id(frame_id: int) -> None:
        if not 0 <= frame_id <= MAX_FRAME_ID:
            raise ValueError(f"LIN identifier must be 0..0x{MAX_FRAME_ID:02X}, got {frame_id}")

    @staticmethod
    def _check_data(data: bytes) -> None:
        if not MIN_DATA_LEN <= len(data) <= MAX_DATA_LEN:
            raise ValueError(
                f"a LIN frame carries {MIN_DATA_LEN}..{MAX_DATA_LEN} data bytes, "
                f"got {len(data)}"
            )
