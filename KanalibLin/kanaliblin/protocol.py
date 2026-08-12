"""
Protocol constants for the KanaLin LIN channel.

Every value here mirrors the device firmware. They are transcriptions, not
definitions: if one ever disagrees with the hardware, the hardware is right.
"""

from __future__ import annotations

from enum import IntEnum, IntFlag

# --------------------------------------------------------------------------
# USB identity of the CDC ACM (LIN) interface of the composite device
# --------------------------------------------------------------------------
USB_VID = 0x0483
USB_PID = 0x5710

#: Baud rate used on the host side of the virtual COM port. It is meaningless
#: for a CDC ACM link (there is no real UART between host and MCU), but
#: pyserial requires a value.
HOST_SERIAL_BAUDRATE = 921600

# --------------------------------------------------------------------------
# Byte framing (PPP/HDLC-like): 0x7E LEN CMD PAYLOAD... CRC8 0x7E
# --------------------------------------------------------------------------
FLAG = 0x7E
ESC = 0x7D
ESC_XOR = 0x20


class Command(IntEnum):
    """Commands sent host -> device."""

    SET_MODE = 0x01
    SET_BAUDRATE = 0x02
    MASTER_SEND_HEADER = 0x10
    MASTER_SEND_FRAME = 0x11
    SLAVE_SET_RESPONSE = 0x20
    SLAVE_CLEAR_RESPONSE = 0x21
    GET_STATUS = 0x30
    SCHEDULE_LOAD = 0x31
    SCHEDULE_START = 0x32
    SCHEDULE_STOP = 0x33

    # Bus sleep/wake-up. These act on the *bus*; the interface itself never
    # sleeps and its transceiver stays in Normal mode.
    SEND_GOTO_SLEEP = 0x40
    SEND_WAKEUP = 0x41

    # Diagnostic extensions, outside the standard LIN command set.
    GET_UART_REGS = 0x7C  # dump the raw baud-rate registers of UART5
    LOOPBACK = 0x7F  # echo the payload back, without touching the LIN bus


class Event(IntEnum):
    """Events sent device -> host."""

    FRAME_RX = 0x80
    STATUS = 0x81
    HEADER_ONLY = 0x82
    NACK = 0x8E
    ACK = 0x8F

    # KanaLin extensions (replies to the two extension commands above).
    UART_REGS = 0xFC
    LOOPBACK = 0xFF


class Mode(IntEnum):
    """Role of the LIN channel, selected with ``SET_MODE``."""

    IDLE = 0  #: channel neither transmits nor reports bus events
    MASTER = 1  #: may send headers/frames and run the schedule table
    SLAVE = 2  #: listens for headers and answers from the response table


class Checksum(IntEnum):
    """LIN checksum flavour."""

    CLASSIC = 0  #: sum of the data bytes only (LIN 1.x, and IDs 0x3C/0x3D)
    ENHANCED = 1  #: sum includes the PID (LIN 2.x)


class BusState(IntEnum):
    """``bus_state`` field of the STATUS event."""

    IDLE = 0  #: no frame seen in the last second
    ACTIVE = 1  #: at least one frame seen recently


class ErrorFlags(IntFlag):
    """
    Sticky ``err_flags`` bits of the STATUS event.

    They accumulate in the firmware ISR and are cleared when read, so every
    :meth:`kanaliblin.KanaLin.get_status` call returns the errors seen *since the
    previous call*.

    ``TIMEOUT`` after a bare header is normal, not a fault: it is what the
    firmware sets when it emits ``HEADER_ONLY`` because nobody answered.
    """

    NONE = 0
    TIMEOUT = 1 << 0  #: no response to a header, or frame closed by silence
    CHECKSUM = 1 << 1  #: received frame with a bad LIN checksum
    FRAMING = 1 << 2  #: framing/bit error on the wire
    SHORT_BREAK = 1 << 3  #: break shorter than the configured 11 bits


# --------------------------------------------------------------------------
# Limits enforced by the firmware. Exceeding any of them gets the command
# rejected with NACK, so the library checks them up front to give a clearer
# error than "the device said no".
# --------------------------------------------------------------------------

#: Longest payload the device's frame decoder accepts (``LIN_MAX_PAYLOAD``).
#: The wire format would allow 253; 128 is a RAM budget choice in the firmware.
MAX_PAYLOAD = 128

#: A LIN frame carries 1..8 data bytes. ``dlc = 0`` is rejected on purpose:
#: it could not be told apart from a header with no response.
MIN_DATA_LEN = 1
MAX_DATA_LEN = 8

#: Schedule table capacity (``LIN_SCHEDULE_MAX``).
MAX_SCHEDULE_ENTRIES = 32

#: Range accepted by ``SET_BAUDRATE`` (the LIN standard's range).
BAUDRATE_MIN = 1000
BAUDRATE_MAX = 20000

#: Baud rate the firmware boots with, and restores when the host disconnects.
DEFAULT_BAUDRATE = 19200

#: Below roughly this rate the USART BRR overflows on this board (UART5 runs
#: off PCLK1 with prescaler DIV1, so USARTDIV = PCLK1 / baud and PCLK1 is
#: 144 MHz). The firmware detects it by reading the rate back and answers
#: NACK, so nothing breaks -- but do not expect 1200 baud to work.
PRACTICAL_BAUDRATE_MIN = 2200

#: Valid LIN frame identifiers (6 bits).
MAX_FRAME_ID = 0x3F

#: The two identifiers reserved for diagnostics: 0x3C carries the master
#: request, 0x3D the slave response.
MASTER_REQUEST_ID = 0x3C
SLAVE_RESPONSE_ID = 0x3D
DIAGNOSTIC_FRAME_IDS = (MASTER_REQUEST_ID, SLAVE_RESPONSE_ID)


# --------------------------------------------------------------------------
# Bus sleep / wake-up
# --------------------------------------------------------------------------

#: Data bytes of the LIN go-to-sleep command, as the firmware transmits them
#: for ``SEND_GOTO_SLEEP``: first byte 0x00, the remaining seven 0xFF.
GOTO_SLEEP_DATA = bytes([0x00]) + bytes([0xFF] * 7)

#: Value of data byte 0 that marks a frame on 0x3C as a go-to-sleep command.
GOTO_SLEEP_MARKER = 0x00

#: Bounds of the dominant wake-up pulse in the LIN standard, in microseconds.
WAKEUP_PULSE_MIN_US = 250
WAKEUP_PULSE_MAX_US = 5000

#: Width the firmware actually emits for ``SEND_WAKEUP`` (``LIN_WAKEUP_PULSE_US``).
#: Centred in the window above, and independent of the baud rate because it is
#: bit-banged on the TX pin rather than transmitted as a byte.
WAKEUP_PULSE_US = 1000


def is_goto_sleep(frame_id: int, data: bytes) -> bool:
    """
    Whether a frame seen on the bus is the LIN go-to-sleep command.

    The test is deliberately loose about data bytes 1..7. The LIN standard
    fills them with 0xFF but declares them reserved, and requires slave nodes
    to evaluate **only the first data byte** -- so matching all eight would be
    stricter than the standard and would miss real ECUs that put something else
    in the padding.

    The caller is expected to have checked the checksum first; this looks at
    identifier and data only.
    """
    return frame_id == MASTER_REQUEST_ID and len(data) == 8 and data[0] == GOTO_SLEEP_MARKER


def default_checksum(frame_id: int) -> Checksum:
    """
    The checksum flavour a frame should use when the caller does not say.

    The LIN standard mandates the **classic** checksum on the diagnostic
    identifiers, whatever the rest of the bus uses. Getting this wrong fails
    silently: the ECU drops the request without a word, and the device's own
    echo still reports ``checksum_ok`` because it verifies frames it published
    against the flavour it was told to use. So the rule is applied here rather
    than left to every caller to remember.
    """
    return Checksum.CLASSIC if frame_id in DIAGNOSTIC_FRAME_IDS else Checksum.ENHANCED
