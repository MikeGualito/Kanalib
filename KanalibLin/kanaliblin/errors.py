"""Exceptions raised by the KanaLin library."""

from __future__ import annotations

from .protocol import Command

__all__ = [
    "KanaLinError",
    "DeviceNotFound",
    "CommandRejected",
    "ResponseTimeout",
    "LinkError",
]


class KanaLinError(Exception):
    """Base class for every error raised by this library."""


class DeviceNotFound(KanaLinError):
    """No KanaLin device is enumerated (or the given port does not exist)."""


class ResponseTimeout(KanaLinError):
    """The device did not answer a command in time."""


class LinkError(KanaLinError):
    """
    The serial link failed or was closed while a call was in flight.

    Typically the device was unplugged. **There is no automatic reconnection**:
    once the link is gone the reader thread stops and every later call raises
    this, rather than hanging. An application that must survive a replug should
    catch it, ``close()`` the instance and open a new one -- see the
    "Reconnecting" section of the README, including what the device forgets
    when USB is deconfigured.
    """


#: Why the firmware normally rejects each command. Appended to the exception
#: message because a bare "NACK" sends people looking in the wrong place --
#: most rejections are a state precondition, not a malformed payload.
_REJECT_HINTS: dict[int, str] = {
    Command.SET_MODE: "mode must be IDLE, MASTER or SLAVE",
    Command.SET_BAUDRATE: (
        "baud rate outside 1000..20000, the schedule is running, a transmission is "
        "in progress, or the USART could not be reprogrammed to that rate "
        "(below ~2200 baud the BRR overflows on this board)"
    ),
    Command.MASTER_SEND_HEADER: "the channel is not in MASTER mode",
    Command.MASTER_SEND_FRAME: (
        "the channel is not in MASTER mode, or the data length is not 1..8 bytes"
    ),
    Command.SLAVE_SET_RESPONSE: "the data length is not 1..8 bytes",
    Command.SCHEDULE_LOAD: (
        "the schedule is still running (stop it first), or more than 32 entries"
    ),
    Command.SCHEDULE_START: "the channel is not in MASTER mode, or the table is empty",
    Command.SEND_GOTO_SLEEP: (
        "the channel is not in MASTER mode, or a transmission is already in flight"
    ),
    Command.SEND_WAKEUP: (
        "the channel is IDLE, the schedule is running, or the bus was busy -- the "
        "pulse is only emitted with nothing in flight, since a 1 ms dominant would "
        "destroy a frame in progress"
    ),
}


class CommandRejected(KanaLinError):
    """
    The device answered NACK.

    The command is echoed back in the NACK payload, so :attr:`command` always
    identifies which call was refused -- even when several are in flight.
    """

    def __init__(self, command: int, detail: str = "") -> None:
        try:
            name = Command(command).name
        except ValueError:
            name = f"0x{command:02X}"
        hint = detail or _REJECT_HINTS.get(command, "")
        message = f"device rejected {name}"
        if hint:
            message += f" ({hint})"
        super().__init__(message)
        self.command = command
