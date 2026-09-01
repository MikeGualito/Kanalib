#!/usr/bin/env python3
"""
flash.py - writes the KanaLin firmware to a NUCLEO-C542RC over its ST-LINK.

The .hex next to this script is the image the two host libraries in this repo
talk to. Flashing it is the one step that cannot be done from Python alone: it
goes through STM32CubeProgrammer's CLI, over the board's on-board ST-LINK-V3EC.

Two USB cables per board, and they do different jobs:

  ST-LINK port   the debug probe. This is what flashing uses.
  USER port      what enumerates the KanaLin device (the CDC COM port for LIN
                 and the WinUSB interface for CAN). The libraries use this one.

A board whose power-selection jumper is set to feed from the ST-LINK goes dead
when left on the user cable alone, and the symptom is that nothing enumerates
at all.

Usage:
  py flash.py                  flash the single attached board
  py flash.py --list           list attached probes and do nothing else
  py flash.py --sn <ST-LINK>   pick one board when several probes are attached
  py flash.py --image <path>   write a different image (.hex, .bin or .elf)

With more than one probe attached and no --sn, the script refuses rather than
guessing: writing the wrong board of a two-board bench is silent, and costs a
debugging session to notice.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_IMAGE = os.path.join(HERE, "KanaLin_firmware.hex")

# Where the firmware lives in the STM32C542RC's flash. Only needed for a raw
# .bin, which carries no addresses of its own; .hex and .elf carry theirs.
FLASH_BASE = "0x08000000"

# Where STM32CubeProgrammer's CLI tends to live. Globbed rather than pinned:
# the CubeIDE plugin directory carries the build date in its name and changes
# with every update, and the standalone bundle carries its version.
PROGRAMMER_GLOBS = [
    # Bundled with STM32CubeIDE.
    os.path.join("C:\\", "ST", "STM32CubeIDE_*", "STM32CubeIDE", "plugins",
                 "com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.*",
                 "tools", "bin", "STM32_Programmer_CLI.exe"),
    # Bundle dropped by the STM32Cube tools under the user profile.
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "stm32cube", "bundles",
                 "programmer", "*", "bin", "STM32_Programmer_CLI.exe"),
    # Standalone STM32CubeProgrammer install.
    os.path.join("C:\\", "Program Files", "STMicroelectronics",
                 "STM32Cube", "STM32CubeProgrammer", "bin",
                 "STM32_Programmer_CLI.exe"),
    # Linux / macOS standalone install.
    os.path.expanduser("~/STMicroelectronics/STM32Cube/STM32CubeProgrammer/"
                       "bin/STM32_Programmer_CLI"),
    "/opt/stm32cubeprog/bin/STM32_Programmer_CLI",
]


def find_programmer() -> str | None:
    """The STM32_Programmer_CLI to use, or None if there is no copy around."""
    override = os.environ.get("STM32_PROGRAMMER_CLI")
    if override and os.path.exists(override):
        return override

    for pattern in PROGRAMMER_GLOBS:
        if not pattern:
            continue
        hits = sorted(glob.glob(pattern))
        if hits:
            # Newest by name: the globs sort by version or build date.
            return hits[-1]

    # Last resort: on PATH.
    from shutil import which
    return which("STM32_Programmer_CLI")


def run_programmer(prog: str, args: list[str]) -> str:
    try:
        out = subprocess.run([prog] + args, capture_output=True, text=True,
                             timeout=180)
    except subprocess.TimeoutExpired:
        return "<timed out>"
    return (out.stdout or "") + (out.stderr or "")


def list_probes(prog: str) -> list[tuple[str, str]]:
    """The ST-LINK probes attached, as (serial, board name)."""
    text = run_programmer(prog, ["-l", "st-link"])
    serials = re.findall(r"ST-LINK SN\s*:\s*(\S+)", text)
    boards = re.findall(r"Board Name\s*:\s*(.+)", text)
    return [(sn, boards[i].strip() if i < len(boards) else "?")
            for i, sn in enumerate(serials)]


def pick_probe(probes: list[tuple[str, str]],
               wanted: str | None) -> tuple[str, str] | None:
    if not probes:
        print("!! no ST-LINK probe found. Is the DEBUG USB cable plugged in?")
        return None

    if wanted is not None:
        for sn, board in probes:
            if sn.lower() == wanted.lower():
                return sn, board
        print(f"!! no probe with ST-LINK SN {wanted}. Attached:")
        for sn, board in probes:
            print(f"     {sn}  {board}")
        return None

    if len(probes) > 1:
        print("!! more than one probe attached. Pass --sn, or unplug one, so "
              "there is no doubt about which board is being written:")
        for sn, board in probes:
            print(f"     {sn}  {board}")
        return None

    return probes[0]


def flash(prog: str, image: str, wanted_sn: str | None) -> bool:
    if not os.path.exists(image):
        print(f"!! {image} does not exist.")
        return False

    chosen = pick_probe(list_probes(prog), wanted_sn)
    if chosen is None:
        return False
    sn, board = chosen

    print(f"-- writing {os.path.basename(image)} to {board}, ST-LINK SN {sn}")
    # A raw .bin has no addresses in it, so the base has to be supplied; .hex
    # and .elf carry theirs and must NOT be given one, or it is written twice.
    write = ["-w", image]
    if image.lower().endswith(".bin"):
        write.append(FLASH_BASE)
    # mode=UR holds the target under reset while connecting, so a running
    # firmware busy on USB cannot refuse the connection.
    text = run_programmer(prog, ["-c", "port=SWD", f"sn={sn}", "mode=UR"]
                                + write + ["-v", "-rst"])
    for line in text.splitlines():
        if re.search(r"[Ee]rror|Warning|verified|Download|complete", line):
            print("   " + line.strip())
    return ("Download verified successfully" in text
            or "Verification...OK" in text)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", default=DEFAULT_IMAGE,
                    help="image to write: .hex, .bin or .elf "
                         "(default: the .hex beside this script)")
    ap.add_argument("--sn", default=None,
                    help="ST-LINK serial of the board to write")
    ap.add_argument("--list", action="store_true",
                    help="list the attached probes and exit")
    args = ap.parse_args()

    prog = find_programmer()
    if prog is None:
        print("!! STM32_Programmer_CLI not found. Install STM32CubeProgrammer "
              "(or STM32CubeIDE, which bundles it), or point the environment "
              "variable STM32_PROGRAMMER_CLI at the executable.")
        return 2

    if args.list:
        probes = list_probes(prog)
        if not probes:
            print("no ST-LINK probe found. Is the DEBUG USB cable plugged in?")
        for sn, board in probes:
            print(f"{sn}  {board}")
        return 0

    if not flash(prog, args.image, args.sn):
        print("\n!! flashing did not report success.")
        return 1

    print("\n-- done. The board resets and re-enumerates on its USER port, so "
          "give it a moment before opening the COM port or the WinUSB "
          "interface.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
