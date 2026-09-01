# Firmware

The firmware image the two libraries in this repo talk to, plus a script to
write it to a board.

```
Firmware/
├── KanaLin_firmware.hex   the image (NUCLEO-C542RC / STM32C542RCT6, Cortex-M33)
├── flash.py               writes it over the board's on-board ST-LINK
└── README.md
```

`KanalibLin` and `KanalibCan` need a board already running this image. Without
it there is nothing to enumerate: no CDC COM port for LIN, no WinUSB interface
for CAN.

## Flashing

```
py Firmware/flash.py            # flash the single attached board
py Firmware/flash.py --list     # just list the attached ST-LINK probes
py Firmware/flash.py --sn <SN>  # pick one when several probes are attached
```

It needs **STM32CubeProgrammer's CLI** (`STM32_Programmer_CLI`), which ships
standalone and also inside STM32CubeIDE. The script looks in the usual install
locations and on `PATH`; if yours is somewhere else, point
`STM32_PROGRAMMER_CLI` at the executable.

With more than one probe attached and no `--sn`, it refuses rather than
guessing — writing the wrong board of a two-board bench is silent, and costs a
debugging session to notice.

## Two cables per board

The NUCLEO has two USB connectors and they do different jobs:

| Connector | What it is for |
|---|---|
| **ST-LINK** | the debug probe. Flashing goes through here. |
| **USER** | what enumerates the KanaLin device. This is the one the libraries open. |

Once a board is flashed it only needs the USER cable.

⚠️ Watch the **power-selection jumper**. If it is set to feed from the ST-LINK,
the board goes dead when left on the USER cable alone, and the symptom is that
nothing enumerates at all.

## After a write

The board resets and re-enumerates on its USER port, so Windows may hand it a
different COM port number. Give it a moment before opening the device.

Each board derives its USB serial from its MCU's unique device ID, so two
boards running this same image are still told apart by
`KanalibCan(serial=...)` and `KanaLin.open(serial_number=...)`.

## Image

| | |
|---|---|
| Target | NUCLEO-C542RC (STM32C542RCT6) |
| Channels | LIN on UART5, CAN / CAN FD on FDCAN1, both over one composite USB device |
| Format | Intel HEX, carrying its own load addresses and entry point |
| Flash range | `0x08000000`-`0x08011517` (70 936 bytes) |
| Size on chip | `text 70504` - `data 432` - `bss 48480` |
| sha256 | `6c13130ae977939ba66000c80c869a81e70aa8164feca96c35d04a3883473288` |

Built with `arm-none-eabi-gcc` 14.3 from the firmware project (a separate
repository); this folder carries the built artefact only.

**Why Intel HEX and not an `.elf`.** The bytes that reach flash are the same
either way, but an `.elf` built for debugging carries the whole DWARF tree with
it: absolute source paths from the build machine, every function and struct
member by name, a map from each instruction back to its source line, and the
literal text of every macro. None of that is needed to program a board, and it
is a third of a megabyte of build-machine detail travelling with a 70 KB image.
Intel HEX carries the addresses and nothing else.

The `.hex` verifies byte for byte against the linked image, and every line
carries its own checksum, so a corrupted download fails at programming time
rather than producing a board that misbehaves.

`flash.py` also accepts a `.bin` (`--image`), supplying the load address for
it, and an `.elf` if you ever need to debug on target.
