"""
selftest.py - offline checks. No hardware needed.

    python -m kanalibcan.selftest

Checks the framing against the values the firmware's own differential test
pinned down, and checks that the bit-timing solver reproduces the timings that
were confirmed with an oscilloscope rather than some other arithmetically valid
answer.
"""

from __future__ import annotations

import random
import sys

from .bittiming import solve
from .errors import BitTimingError, FrameError
from .framing import (
    Bittiming, BtConst, BtConstExt, DeviceConfig, DeviceMode, DeviceState,
    HostFrame, bittiming_to_hw, dlc_to_len, is_valid_data_len, len_to_dlc,
    parse_frame,
)
from .message import CanMessage
from .protocol import (
    CLASSIC_PACKET_LEN, CLASSIC_PACKET_LEN_TS, EXPECTED_FCLK_HZ,
    FD_PACKET_LEN, FD_PACKET_LEN_TS, GS_CAN_ECHO_ID_RX, GS_CAN_FLAG_BRS,
    GS_CAN_FLAG_FD, GS_CAN_ID_EFF_FLAG, GS_DEVICE_BT_CONST_EXT_LEN,
    GS_DEVICE_BT_CONST_LEN, GS_DEVICE_CONFIG_LEN, GS_DEVICE_MODE_LEN,
    GS_DEVICE_STATE_LEN, GS_ERR_BAD_DLC, GS_ERR_TOO_SHORT, GS_OK,
)

GREEN, RED, RESET = "\033[32m", "\033[31m", "\033[0m"

_checks = 0
_fails = 0


def ok(label: str, cond: bool, detail: str = "") -> None:
    global _checks, _fails
    _checks += 1
    if cond:
        print(f"  {GREEN}OK{RESET}    {label} {detail}")
    else:
        _fails += 1
        print(f"  {RED}FAIL{RESET}  {label} {detail}")


def eq(label: str, got, want) -> None:
    ok(label, got == want, "" if got == want else f"(got {got!r}, want {want!r})")


def run() -> int:
    print("== DLC <-> length ==")
    eq("classic codes 9..15 all mean 8 bytes",
       [dlc_to_len(d, False) for d in range(9, 16)], [8] * 7)
    eq("FD code 15 is 64 bytes", dlc_to_len(15, True), 64)
    eq("40 bytes rounds up to the 48-byte code", len_to_dlc(40, True), 14)
    eq("9 bytes is impossible in classic CAN", len_to_dlc(9, False), 0xFF)
    ok("48 is an exact FD length", is_valid_data_len(48, True))
    ok("40 is NOT an exact FD length", not is_valid_data_len(40, True))

    print("== packet size follows the FORMAT, never the DLC ==")
    for dlc in range(16):
        f = HostFrame(can_dlc=dlc, data=b"\xff" * dlc_to_len(dlc, False))
        if f.wire_len(False) != CLASSIC_PACKET_LEN or \
                f.wire_len(True) != CLASSIC_PACKET_LEN_TS:
            ok(f"classic dlc={dlc} sizes to 20/24", False)
            break
    else:
        ok("every classic DLC sizes to 20/24 bytes", True)

    f = HostFrame(can_dlc=15, flags=GS_CAN_FLAG_FD, data=bytes(64))
    eq("a full FD frame is 76 without a timestamp", f.wire_len(False),
       FD_PACKET_LEN)
    eq("a full FD frame is 80 with one", f.wire_len(True), FD_PACKET_LEN_TS)

    f = HostFrame(can_dlc=3, data=b"\x11\x22\x33", timestamp_us=0xAABBCCDD)
    wire = f.serialize(True)
    eq("a 3-byte classic frame still weighs 24 bytes", len(wire), 24)
    eq("its data slot is zero-padded", wire[15:20], b"\x00" * 5)
    eq("the timestamp sits at the fixed offset 20",
       int.from_bytes(wire[20:24], "little"), 0xAABBCCDD)

    print("== parse ==")
    code, got = parse_frame(wire, hw_timestamp=True)
    eq("round trip: code", code, GS_OK)
    eq("round trip: dlc", got.can_dlc, 3)
    eq("round trip: data", got.data, b"\x11\x22\x33")
    eq("round trip: timestamp", got.timestamp_us, 0xAABBCCDD)
    eq("a short header is refused", parse_frame(b"\x00" * 11)[0],
       GS_ERR_TOO_SHORT)
    eq("an out-of-range DLC is refused",
       parse_frame(bytes(8) + b"\x10" + bytes(15))[0], GS_ERR_BAD_DLC)

    print("== CanMessage ==")
    m = CanMessage(0x123, b"\x01\x02\x03")
    ok("standard id by default (python-can defaults to extended!)",
       not m.is_extended_id)
    hf = m.to_host_frame(7)
    eq("standard id is not EFF-flagged", hf.can_id & GS_CAN_ID_EFF_FLAG, 0)
    back = CanMessage.from_host_frame(hf)
    eq("id survives", back.arbitration_id, 0x123)
    eq("data survives", back.data, b"\x01\x02\x03")

    m = CanMessage(0x1ABCDEF, b"", is_extended_id=True)
    eq("extended id is EFF-flagged",
       bool(m.to_host_frame(1).can_id & GS_CAN_ID_EFF_FLAG), True)

    m = CanMessage(0x321, bytes(64), is_fd=True, bitrate_switch=True)
    hf = m.to_host_frame(2)
    eq("FD flag set", bool(hf.flags & GS_CAN_FLAG_FD), True)
    eq("BRS flag set", bool(hf.flags & GS_CAN_FLAG_BRS), True)
    eq("64 bytes is DLC code 15", hf.can_dlc, 15)
    eq("an FD frame weighs 80 bytes with a timestamp", hf.wire_len(True), 80)

    m = CanMessage(0x1, bytes(40), is_fd=True)
    ok("a 40-byte payload reports itself as padded", m.is_padded)

    try:
        CanMessage(0x800).to_host_frame(1)
        ok("an 11-bit id above 0x7FF is refused, not masked", False)
    except FrameError:
        ok("an 11-bit id above 0x7FF is refused, not masked", True)
    try:
        CanMessage(0x20000000, is_extended_id=True).to_host_frame(1)
        ok("a 29-bit id above 0x1FFFFFFF is refused", False)
    except FrameError:
        ok("a 29-bit id above 0x1FFFFFFF is refused", True)

    rx = CanMessage.from_host_frame(HostFrame(echo_id=GS_CAN_ECHO_ID_RX))
    ok("echo_id 0xFFFFFFFF means received from the bus", rx.is_rx)
    echo = CanMessage.from_host_frame(HostFrame(echo_id=5))
    ok("any other echo_id means it is our own echo", not echo.is_rx)

    print("== bit timing: the solver must reproduce the SCOPE-MEASURED values ==")
    # Confirmed on an oscilloscope: 1998 ns arbitration bit, 497 ns data bit,
    # and 395 us vs 197 us frame duration at 250k vs 500k.
    s = solve(500_000, EXPECTED_FCLK_HZ)
    eq("500k: 72 quanta per bit", s.tq_per_bit, 72)
    eq("500k: prescaler 4", s.hw.prescaler, 4)
    eq("500k: seg1 62 / seg2 9", (s.hw.time_seg1, s.hw.time_seg2), (62, 9))
    eq("500k: exact", s.actual_bitrate, 500_000)
    ok("500k: sample point 87.5%", abs(s.sample_point - 0.875) < 0.005,
       f"({s.sample_point * 100:.1f}%)")

    s = solve(250_000, EXPECTED_FCLK_HZ)
    eq("250k: same 72 quanta, prescaler doubled to 8",
       (s.tq_per_bit, s.hw.prescaler), (72, 8))
    eq("250k: exact", s.actual_bitrate, 250_000)

    s = solve(2_000_000, EXPECTED_FCLK_HZ, data_phase=True)
    eq("2M data: 18 quanta per bit", s.tq_per_bit, 18)
    eq("2M data: prescaler 4", s.hw.prescaler, 4)
    eq("2M data: seg1 14 / seg2 3", (s.hw.time_seg1, s.hw.time_seg2), (14, 3))
    eq("2M data: exact", s.actual_bitrate, 2_000_000)

    for rate in (125_000, 250_000, 500_000, 1_000_000):
        s = solve(rate, EXPECTED_FCLK_HZ)
        if s.actual_bitrate != rate:
            eq(f"{rate} is exact", s.actual_bitrate, rate)
            break
    else:
        ok("125k / 250k / 500k / 1M are all exact on a 144 MHz clock", True)

    try:
        solve(5_000_000, EXPECTED_FCLK_HZ, data_phase=True)
        ok("5 Mbit/s is refused (144/5 is not an integer)", False)
    except BitTimingError:
        ok("5 Mbit/s is refused rather than rounded", True)

    print("== bit timing: range checks mirror the firmware ==")
    bad = [
        (Bittiming(13, 15, 7, 4, 0), False),          # brp below minimum
        (Bittiming(13, 15, 7, 4, 513), False),        # brp above maximum
        (Bittiming(200, 100, 7, 4, 8), False),        # seg1 above maximum
        (Bittiming(0xFFFFFFFF, 2, 7, 4, 8), False),   # the sum would wrap
        (Bittiming(13, 15, 4, 5, 8), False),          # sjw wider than seg2
        (Bittiming(32, 30, 9, 4, 4), True),           # fine nominal, not data
    ]
    if all(bittiming_to_hw(bt, dp)[0] != GS_OK for bt, dp in bad):
        ok("six out-of-range timings are all refused", True)
    else:
        ok("six out-of-range timings are all refused", False)

    eq("the .ioc2 nominal timing really is 500 kbit/s",
       bittiming_to_hw(Bittiming(0, 251, 36, 1, 1), False)[1]
       .bitrate(EXPECTED_FCLK_HZ), 500_000)

    print("== control payload sizes ==")
    eq("bt_const", len(BtConst().serialize()), GS_DEVICE_BT_CONST_LEN)
    eq("bt_const_ext", len(BtConstExt().serialize()),
       GS_DEVICE_BT_CONST_EXT_LEN)
    eq("device_config", len(DeviceConfig().serialize()), GS_DEVICE_CONFIG_LEN)
    eq("bittiming", len(Bittiming().serialize()), 20)
    eq("device_mode", len(DeviceMode().serialize()), GS_DEVICE_MODE_LEN)
    eq("device_state", len(DeviceState().serialize()), GS_DEVICE_STATE_LEN)
    ok("the first 40 bytes of bt_const_ext ARE a bt_const",
       BtConstExt(nominal=BtConst(feature=0x251B)).serialize()[:40]
       == BtConst(feature=0x251B).serialize())

    print("== random round trips ==")
    rnd = random.Random(0xCA)
    bad_trip = 0
    for _ in range(400):
        fd = rnd.random() < 0.5
        ext = rnd.random() < 0.5
        n = dlc_to_len(rnd.randrange(16), fd)
        src = CanMessage(
            # The id must match the format it is declared as; the library
            # refuses a mismatch rather than masking it.
            arbitration_id=rnd.randrange(1 << (29 if ext else 11)),
            data=bytes(rnd.randrange(256) for _ in range(n)),
            is_extended_id=ext,
            is_fd=fd,
            bitrate_switch=fd and rnd.random() < 0.5,
        )
        wire = src.to_host_frame(rnd.randrange(1 << 20)).serialize(True)
        code, hf = parse_frame(wire, hw_timestamp=True)
        got = CanMessage.from_host_frame(hf) if code == GS_OK else None
        if got is None or got.data != src.data or \
                got.arbitration_id != src.arbitration_id or \
                got.is_fd != src.is_fd or got.is_extended_id != src.is_extended_id:
            bad_trip += 1
    eq("400 random frames survive serialize -> parse", bad_trip, 0)

    print()
    if _fails:
        print(f"{RED}{_fails} of {_checks} checks FAILED{RESET}")
        return 1
    print(f"{GREEN}all {_checks} checks passed{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
