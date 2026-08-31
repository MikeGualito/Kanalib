"""
KanalibLin -- Python API for the LIN channel of a KanaLin USB interface.

Quick start::

    from kanaliblin import KanaLin, Mode

    with KanaLin.open() as lin:          # finds the port by USB VID/PID
        lin.reset()                      # known state: IDLE, no schedule, 19200
        lin.mode = Mode.MASTER
        print(lin.master_send_frame(0x10, b"\\x01\\x02\\x03\\x04"))

See README.md for the full API.
"""

from .device import KanaLin, ScheduleEntry, Stats
from .errors import (
    CommandRejected,
    DeviceNotFound,
    KanaLinError,
    LinkError,
    ResponseTimeout,
)
from .events import BusEvent, HeaderOnly, LinFrame, SleepRequested, Status, UartRegisters
from .framing import RawFrame, StreamDecoder, crc8_sae_j1850, encode, lin_checksum, lin_pid
from .protocol import (
    BAUDRATE_MAX,
    BAUDRATE_MIN,
    DEFAULT_BAUDRATE,
    DIAGNOSTIC_FRAME_IDS,
    GOTO_SLEEP_DATA,
    MASTER_REQUEST_ID,
    MAX_SCHEDULE_ENTRIES,
    SLAVE_RESPONSE_ID,
    USB_PID,
    USB_VID,
    WAKEUP_PULSE_MAX_US,
    WAKEUP_PULSE_MIN_US,
    WAKEUP_PULSE_US,
    BusState,
    Checksum,
    Command,
    ErrorFlags,
    Event,
    Mode,
    default_checksum,
    is_goto_sleep,
)
from .timestamps import (
    TIMESTAMP_MODULUS,
    TIMESTAMP_WRAP_SECONDS,
    TimestampUnwrapper,
    delta_us,
)

# Version history starts here, at the split into KanalibLin / KanalibCan.
# (The predecessor, LibKanaLin, reached 1.2.0; that numbering is not continued.)
__version__ = "1.1.0"

__all__ = [
    # device
    "KanaLin",
    "ScheduleEntry",
    "Stats",
    # events
    "LinFrame",
    "HeaderOnly",
    "SleepRequested",
    "Status",
    "UartRegisters",
    "BusEvent",
    # enums / constants
    "Mode",
    "Checksum",
    "BusState",
    "ErrorFlags",
    "Command",
    "Event",
    "USB_VID",
    "USB_PID",
    "DEFAULT_BAUDRATE",
    "BAUDRATE_MIN",
    "BAUDRATE_MAX",
    "MAX_SCHEDULE_ENTRIES",
    "MASTER_REQUEST_ID",
    "SLAVE_RESPONSE_ID",
    "DIAGNOSTIC_FRAME_IDS",
    "default_checksum",
    # bus sleep / wake-up
    "GOTO_SLEEP_DATA",
    "WAKEUP_PULSE_US",
    "WAKEUP_PULSE_MIN_US",
    "WAKEUP_PULSE_MAX_US",
    "is_goto_sleep",
    # timestamps
    "TimestampUnwrapper",
    "delta_us",
    "TIMESTAMP_MODULUS",
    "TIMESTAMP_WRAP_SECONDS",
    # errors
    "KanaLinError",
    "DeviceNotFound",
    "CommandRejected",
    "ResponseTimeout",
    "LinkError",
    # framing (low level)
    "encode",
    "StreamDecoder",
    "RawFrame",
    "crc8_sae_j1850",
    "lin_pid",
    "lin_checksum",
    "__version__",
]
