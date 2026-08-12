"""
Decoded events coming from the device.

Bus events (:class:`LinFrame`, :class:`HeaderOnly`) are asynchronous: they show
up whenever something happens on the LIN wire, including as a side effect of
your own commands. Command replies (:class:`Status`, :class:`UartRegisters`)
are returned directly by the call that asked for them.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Union

from .protocol import BusState, ErrorFlags, Mode

__all__ = [
    "LinFrame",
    "HeaderOnly",
    "SleepRequested",
    "Status",
    "UartRegisters",
    "BusEvent",
]


@dataclass(frozen=True)
class LinFrame:
    """
    A complete LIN frame observed on the bus.

    The device reports frames it observes, which -- because the transceiver
    echoes back everything it transmits -- includes the frames it publishes
    itself. So sending a frame as master normally produces a :class:`LinFrame`
    of your own transmission; that echo is the proof it really reached the wire.

    ``timestamp_us`` is captured inside the USART interrupt handler at the
    start of the frame (the break), not when the USB packet is assembled, so
    it is free of USB/Windows jitter. It wraps around every ~71 minutes.
    """

    frame_id: int
    data: bytes
    checksum_ok: bool
    timestamp_us: int

    @property
    def dlc(self) -> int:
        """Number of data bytes (1..8)."""
        return len(self.data)

    def __str__(self) -> str:
        mark = "" if self.checksum_ok else " CHECKSUM-BAD"
        return (
            f"ID 0x{self.frame_id:02X} [{self.dlc}] "
            f"{self.data.hex(' ')} @{self.timestamp_us} us{mark}"
        )

    @classmethod
    def from_payload(cls, payload: bytes) -> "LinFrame":
        """Parse a FRAME_RX payload: ``id dlc data[dlc] csum_ok timestamp_us``."""
        if len(payload) < 7:
            raise ValueError(f"FRAME_RX payload too short: {len(payload)} bytes")
        frame_id, dlc = payload[0], payload[1]
        if len(payload) != dlc + 7:
            raise ValueError(
                f"FRAME_RX payload says dlc={dlc} but carries {len(payload) - 7} data bytes"
            )
        data = payload[2 : 2 + dlc]
        checksum_ok = bool(payload[2 + dlc])
        (timestamp_us,) = struct.unpack_from("<I", payload, 3 + dlc)
        return cls(frame_id, data, checksum_ok, timestamp_us)


@dataclass(frozen=True)
class HeaderOnly:
    """
    A header went out (or was seen) and no node published a response.

    Without a second ECU on the bus this is the expected outcome of
    :meth:`kanaliblin.KanaLin.master_send_header` for an identifier that has no
    local slave response registered. It also sets the sticky ``TIMEOUT`` error
    flag, which is normal here and does not indicate a fault.
    """

    frame_id: int

    def __str__(self) -> str:
        return f"ID 0x{self.frame_id:02X} header only (no response)"

    @classmethod
    def from_payload(cls, payload: bytes) -> "HeaderOnly":
        if len(payload) != 1:
            raise ValueError(f"HEADER_ONLY payload should be 1 byte, got {len(payload)}")
        return cls(payload[0])


@dataclass(frozen=True)
class SleepRequested:
    """
    Somebody asked the bus to go to sleep.

    This is **derived on the host**, not a distinct event on the wire. A LIN
    sleep request *is* the diagnostic frame 0x3C with data byte 0 set to 0x00,
    and the firmware already reports it in full as an ordinary
    :class:`LinFrame` -- identifier, all eight data bytes, checksum and MCU
    timestamp. Recognising it here rather than in the firmware costs nothing in
    information and keeps flash, RAM and the interrupt path free.

    The originating frame is kept in :attr:`frame`, so nothing is hidden: the
    same frame is *also* delivered through the normal event queue. This type
    only reaches you through the ``on_sleep_requested`` callback or
    :meth:`kanaliblin.KanaLin.wait_sleep_request`.

    KanaLin never acts on this. It does not sleep, it does not stop answering
    headers, and it does not touch the transceiver -- it only tells you.
    """

    frame: LinFrame

    @property
    def frame_id(self) -> int:
        return self.frame.frame_id

    @property
    def data(self) -> bytes:
        return self.frame.data

    @property
    def timestamp_us(self) -> int:
        return self.frame.timestamp_us

    def __str__(self) -> str:
        return f"go-to-sleep requested on 0x{self.frame_id:02X} @{self.timestamp_us} us"


@dataclass(frozen=True)
class Status:
    """Reply to ``GET_STATUS``. Reading it clears the sticky error flags."""

    mode: Mode
    bus_state: BusState
    errors: ErrorFlags

    def __str__(self) -> str:
        return (
            f"mode={self.mode.name} bus={self.bus_state.name} "
            f"errors={self.errors.name or 'NONE'}"
        )

    @classmethod
    def from_payload(cls, payload: bytes) -> "Status":
        if len(payload) != 3:
            raise ValueError(f"STATUS payload should be 3 bytes, got {len(payload)}")
        return cls(Mode(payload[0]), BusState(payload[1]), ErrorFlags(payload[2]))


#: USART prescaler divisors, indexed by the PRESC register field.
_PRESCALER_DIV = (1, 2, 4, 6, 8, 10, 12, 16, 32, 64, 128, 256, 256, 256, 256, 256)


@dataclass(frozen=True)
class UartRegisters:
    """
    Raw UART5 baud-rate registers (KanaLin diagnostic extension).

    Why this exists: the firmware validates a new baud rate by reading it back
    with ``HAL_UART_GetBaudRate()``, which shares its arithmetic with
    ``HAL_UART_SetBaudRate()``. If both start from a wrong kernel clock or
    prescaler they agree with each other while the bus runs at a different
    speed. These five raw values let the host recompute the real rate from the
    silicon, so a read-back check cannot lie to itself.
    """

    kernel_clock_hz: int
    prescaler: int
    brr: int
    cr1: int
    requested_baudrate: int

    @property
    def over8(self) -> bool:
        """True if the USART is oversampling by 8 instead of 16."""
        return bool((self.cr1 >> 15) & 1)

    @property
    def prescaler_div(self) -> int:
        return _PRESCALER_DIV[self.prescaler & 0x0F]

    @property
    def usartdiv(self) -> int:
        if self.over8:
            return (self.brr & 0xFFF0) | ((self.brr & 0x0007) << 1)
        return self.brr

    @property
    def actual_baudrate(self) -> float:
        """Baud rate recomputed from the registers, independent of the HAL."""
        div = self.usartdiv
        if div == 0:
            return 0.0
        clock = self.kernel_clock_hz // self.prescaler_div
        return (2 * clock / div) if self.over8 else (clock / div)

    @property
    def error_percent(self) -> float:
        """Relative deviation of the real rate from the requested one, in %."""
        if not self.requested_baudrate:
            return 0.0
        return 100.0 * (self.actual_baudrate - self.requested_baudrate) / self.requested_baudrate

    @property
    def bit_time_us(self) -> float:
        """Width of one bit on the wire, in microseconds (what a scope shows)."""
        rate = self.actual_baudrate
        return 1e6 / rate if rate else 0.0

    def __str__(self) -> str:
        return (
            f"fck={self.kernel_clock_hz} Hz presc={self.prescaler} (/{self.prescaler_div}) "
            f"BRR={self.brr} OVER8={int(self.over8)} -> {self.actual_baudrate:.1f} baud "
            f"(requested {self.requested_baudrate}, {self.error_percent:+.2f}%)"
        )

    @classmethod
    def from_payload(cls, payload: bytes) -> "UartRegisters":
        if len(payload) != 20:
            raise ValueError(f"UART_REGS payload should be 20 bytes, got {len(payload)}")
        return cls(*struct.unpack("<5I", payload))


#: Anything the bus can hand you asynchronously.
BusEvent = Union[LinFrame, HeaderOnly]
