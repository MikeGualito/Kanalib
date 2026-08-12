"""
UDS diagnostics over LIN: TesterPresent and Extended Session.

Run:  python examples/06_uds_diagnostics.py [COM7] [--nad 0x64] [--baud 19230]
      python examples/06_uds_diagnostics.py --keep-alive 30   # hold the session

Requires a real LIN slave ECU on the bus. This device acts as the master: it
publishes the request frame and then polls the response frame until the ECU
answers.

--------------------------------------------------------------------------
How UDS travels on LIN
--------------------------------------------------------------------------
UDS services are not sent raw on LIN. They ride on the LIN transport layer,
which adds two things in front of the service bytes:

    byte 0   NAD   node address of the ECU being addressed
    byte 1   PCI   protocol control information: frame type + length
    byte 2+  the UDS payload (SID, then service data), padded with 0xFF

Both directions use fixed identifiers and a fixed shape:

    0x3C  master request frame (MRF)   master -> slave, always 8 bytes
    0x3D  slave response frame (SRF)   slave -> master, always 8 bytes

Two rules that bite if you get them wrong:

* **Classic checksum, always.** 0x3C and 0x3D are diagnostic identifiers and
  the LIN standard mandates the classic checksum for them, whatever the rest of
  the bus uses. The library already defaults to it on those two identifiers; it
  is passed explicitly below to keep the rule visible.
* **The slave never speaks first.** It only puts data on the bus when the
  master sends a 0x3D header. So a request is "publish on 0x3C, then poll 0x3D
  until an answer shows up or the timeout expires".

PCI encoding, low nibble first:

    0x0N   single frame     N = payload length, 1..6 bytes
    0x1N   first frame      length = (N << 8) | next byte, up to 4095
    0x2N   consecutive fr.  N = sequence counter, 1..15 then wraps to 0

Responses come back with the response SID = request SID + 0x40 (so 0x3E -> 0x7E,
0x10 -> 0x50). A negative response is always ``7F <SID> <NRC>``; the special
NRC 0x78 means "still working, keep polling", and is handled here rather than
treated as a failure.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kanaliblin import Checksum, KanaLin, LinFrame, Mode  # noqa: E402

# --------------------------------------------------------------------------
# ECU configuration
# --------------------------------------------------------------------------
DEFAULT_NAD = 0x64
DEFAULT_BAUDRATE = 19230

MASTER_REQUEST_ID = 0x3C
SLAVE_RESPONSE_ID = 0x3D

#: How long to keep polling 0x3D for an answer (the ECU's P2 timeout).
RESPONSE_TIMEOUT = 1.5

#: Gap between two response polls. Matches the schedule slot the ECU expects.
POLL_INTERVAL = 0.017

PADDING = 0xFF

# --------------------------------------------------------------------------
# UDS service identifiers used here
# --------------------------------------------------------------------------
SID_DIAGNOSTIC_SESSION_CONTROL = 0x10
SID_TESTER_PRESENT = 0x3E

SESSION_DEFAULT = 0x01
SESSION_PROGRAMMING = 0x02
SESSION_EXTENDED = 0x03

NRC_RESPONSE_PENDING = 0x78

NRC_NAMES = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x72: "generalProgrammingFailure",
    0x78: "requestCorrectlyReceived-ResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}


class UdsError(Exception):
    """Base class for diagnostic failures."""


class NoResponse(UdsError):
    """The ECU did not answer within the timeout."""


class NegativeResponse(UdsError):
    """The ECU answered ``7F <SID> <NRC>``."""

    def __init__(self, sid: int, nrc: int) -> None:
        name = NRC_NAMES.get(nrc, "unknown")
        super().__init__(f"negative response to SID 0x{sid:02X}: NRC 0x{nrc:02X} ({name})")
        self.sid = sid
        self.nrc = nrc


class UdsOverLin:
    """
    Minimal UDS client on top of the LIN transport layer.

    ``lin`` must already be open and in :class:`~kanaliblin.Mode.MASTER`; this
    class only publishes requests and polls for responses, it does not touch
    the channel configuration.
    """

    def __init__(
        self,
        lin: KanaLin,
        nad: int = DEFAULT_NAD,
        *,
        timeout: float = RESPONSE_TIMEOUT,
        poll_interval: float = POLL_INTERVAL,
        verbose: bool = False,
    ) -> None:
        self.lin = lin
        self.nad = nad
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.verbose = verbose

    # ------------------------------------------------------------------
    def request(self, payload: bytes) -> bytes:
        """
        Send one UDS request and return the positive response payload.

        ``payload`` is the service data starting at the SID; the returned bytes
        start at the response SID. Raises :class:`NegativeResponse` or
        :class:`NoResponse` on failure.
        """
        self._send(payload)
        deadline = time.monotonic() + self.timeout
        while True:
            response = self._read(deadline)
            sid = payload[0]
            if len(response) >= 3 and response[0] == 0x7F and response[1] == sid:
                nrc = response[2]
                if nrc == NRC_RESPONSE_PENDING:
                    # "Still working." The ECU may send this several times; the
                    # clock is restarted, which is what P2* means in practice.
                    if self.verbose:
                        print("      ECU busy (NRC 0x78), still waiting")
                    deadline = time.monotonic() + self.timeout
                    continue
                raise NegativeResponse(sid, nrc)
            if response and response[0] != sid + 0x40:
                raise UdsError(
                    f"unexpected response SID 0x{response[0]:02X}, "
                    f"expected 0x{sid + 0x40:02X}"
                )
            return response

    # ------------------------------------------------------------------
    def _send(self, payload: bytes) -> None:
        """Publish the request on 0x3C. Only single frames are built here."""
        if not 1 <= len(payload) <= 6:
            raise UdsError(
                f"this example only sends single frames (1..6 bytes), got {len(payload)}"
            )
        pdu = bytes((self.nad, len(payload))) + payload
        pdu += bytes((PADDING,)) * (8 - len(pdu))
        if self.verbose:
            print(f"      -> 0x3C {pdu.hex(' ')}")
        # Classic checksum: mandatory here, and what the library would pick
        # anyway for 0x3C. Stated explicitly because getting it wrong fails
        # silently -- the ECU just never answers.
        self.lin.master_send_frame(MASTER_REQUEST_ID, pdu, checksum=Checksum.CLASSIC)

    def _read(self, deadline: float) -> bytes:
        """Poll 0x3D until the ECU answers, reassembling a multi-frame reply."""
        frame = self._poll(deadline)
        pci = frame[1]
        kind, low = pci & 0xF0, pci & 0x0F

        if kind == 0x00:  # single frame
            if not 1 <= low <= 6:
                raise UdsError(f"invalid single-frame length in PCI 0x{pci:02X}")
            return frame[2 : 2 + low]

        if kind != 0x10:  # neither single nor first frame
            raise UdsError(f"unexpected PCI 0x{pci:02X} at the start of a response")

        total = (low << 8) | frame[2]  # first frame: 12-bit length
        data = bytearray(frame[3:8])
        expected_counter = 1
        while len(data) < total:
            frame = self._poll(deadline)
            pci = frame[1]
            if pci & 0xF0 != 0x20:
                raise UdsError(f"expected a consecutive frame, got PCI 0x{pci:02X}")
            if (pci & 0x0F) != expected_counter:
                raise UdsError(
                    f"consecutive frame out of order: got {pci & 0x0F}, "
                    f"expected {expected_counter}"
                )
            expected_counter = (expected_counter + 1) % 16
            data += frame[2:8]
        return bytes(data[:total])

    def _poll(self, deadline: float) -> bytes:
        """Send 0x3D headers until this NAD answers, or the deadline passes."""
        while time.monotonic() < deadline:
            event = self.lin.master_send_header(SLAVE_RESPONSE_ID)
            if isinstance(event, LinFrame) and event.dlc == 8:
                if self.verbose:
                    print(f"      <- 0x3D {event.data.hex(' ')}")
                if not event.checksum_ok:
                    raise UdsError(f"response frame with a bad checksum: {event.data.hex(' ')}")
                if event.data[0] == self.nad:
                    return event.data
                # Another ECU answering on the shared response identifier.
                if self.verbose:
                    print(f"      (ignoring answer from NAD 0x{event.data[0]:02X})")
            time.sleep(self.poll_interval)
        raise NoResponse(
            f"no answer from NAD 0x{self.nad:02X} within {self.timeout:g} s "
            f"(is the ECU powered, and is the NAD right?)"
        )

    # ------------------------------------------------------------------
    # Services
    # ------------------------------------------------------------------
    def tester_present(self) -> bytes:
        """``3E 00`` -- keep the current session alive."""
        return self.request(bytes((SID_TESTER_PRESENT, 0x00)))

    def diagnostic_session_control(self, session: int) -> bytes:
        """``10 xx`` -- switch session. Returns the response payload."""
        return self.request(bytes((SID_DIAGNOSTIC_SESSION_CONTROL, session)))


def describe_session_response(response: bytes) -> str:
    """
    Decode the payload of a positive 0x50 response.

    Beyond the echoed session byte, UDS defines four optional bytes: P2 in
    milliseconds and P2* in units of 10 ms. Plenty of ECUs omit them.
    """
    if len(response) < 2:
        return "(empty)"
    text = f"session 0x{response[1]:02X}"
    if len(response) >= 6:
        p2 = int.from_bytes(response[2:4], "big")
        p2_star = int.from_bytes(response[4:6], "big") * 10
        text += f", P2 = {p2} ms, P2* = {p2_star} ms"
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("---")[0])
    parser.add_argument("port", nargs="?", help="COM port (auto-detected if omitted)")
    parser.add_argument("--nad", type=lambda s: int(s, 0), default=DEFAULT_NAD,
                        help=f"ECU node address (default 0x{DEFAULT_NAD:02X})")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUDRATE,
                        help=f"LIN bus baud rate (default {DEFAULT_BAUDRATE})")
    parser.add_argument("--keep-alive", type=float, metavar="SECONDS", default=0.0,
                        help="after entering the extended session, hold it with "
                             "periodic TesterPresent for this many seconds")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print every diagnostic frame in hex")
    args = parser.parse_args()

    with KanaLin.open(args.port) as lin:
        print(f"master on {lin.port}, {args.baud} baud, ECU NAD 0x{args.nad:02X}")
        lin.reset()
        lin.set_baudrate(args.baud)
        lin.mode = Mode.MASTER

        uds = UdsOverLin(lin, args.nad, verbose=args.verbose)
        failures = 0

        # --- 1) TesterPresent in the default session --------------------
        print("\n1. TesterPresent (3E 00)")
        try:
            response = uds.tester_present()
            print(f"   positive response: {response.hex(' ')}  -> the ECU is alive")
        except UdsError as exc:
            print(f"   FAILED: {exc}")
            failures += 1

        # --- 2) Extended session ----------------------------------------
        print("\n2. DiagnosticSessionControl -> extended (10 03)")
        try:
            response = uds.diagnostic_session_control(SESSION_EXTENDED)
            print(f"   positive response: {response.hex(' ')}")
            print(f"   {describe_session_response(response)}")
        except UdsError as exc:
            print(f"   FAILED: {exc}")
            failures += 1

        # --- 3) TesterPresent again, now inside the extended session ----
        print("\n3. TesterPresent again (the session should still be open)")
        try:
            response = uds.tester_present()
            print(f"   positive response: {response.hex(' ')}")
        except UdsError as exc:
            print(f"   FAILED: {exc}")
            failures += 1

        # --- 4) optionally hold the session -----------------------------
        # The embedded scheduler cannot do this: it sends headers only, and a
        # TesterPresent needs its request payload published on 0x3C each time.
        # Sending it from here is fine -- the deadline is seconds, not
        # microseconds, so host jitter does not matter.
        if args.keep_alive > 0:
            period = 2.0
            print(f"\n4. Holding the session for {args.keep_alive:g} s "
                  f"(TesterPresent every {period:g} s)")
            end = time.monotonic() + args.keep_alive
            while time.monotonic() < end:
                time.sleep(min(period, max(0.0, end - time.monotonic())))
                try:
                    uds.tester_present()
                    print(f"   {end - time.monotonic():5.1f} s left: ECU still responding")
                except UdsError as exc:
                    print(f"   FAILED: {exc}")
                    failures += 1
                    break

        # --- back to a quiet bus ----------------------------------------
        # Returning to the default session drops any elevated state; without it
        # the ECU falls back on its own S3 timeout (typically 5 s) anyway.
        try:
            uds.diagnostic_session_control(SESSION_DEFAULT)
            print("\nback to the default session")
        except UdsError as exc:
            print(f"\ncould not return to the default session: {exc}")

        lin.mode = Mode.IDLE
        status = lin.get_status()
        # TIMEOUT is expected here: every unanswered 0x3D poll sets it.
        print(f"status: {status}")
        print(f"link: {lin.stats}")

        if failures:
            print(f"\n{failures} request(s) failed")
            return 1
        print("\nall requests answered")
        return 0


if __name__ == "__main__":
    sys.exit(main())
