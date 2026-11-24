# Pin census of fpga-4

Date: 2026-11-24, all times UTC. Board: fpga-4, USB serial `a2961e5cac65b25f`, TTDBv3 [3.2] demoboard
(from the SDK's boot text), RP2350B, MicroPython 1.29.0-preview (`b006887db6`, 2026-08-11), SDK 3.1.0
(revision `baaf0a27...`), CPU 133 MHz. RP2350 SYSINFO CHIP_ID = `0x30004927`: revision field 3, part 4,
manufacturer Raspberry Pi. MicroPython does not expose the stepping by name; only that field is known.
Tool: `tthfv board census fpga-4` (profile `boards/fpga-4.json`), after a read-only `--info` session.
The board was in the idle display's state (`tt_fpga_platform.bin` loaded, SDK mode `ASIC_MANUAL_INPUTS`,
project clock 100 Hz by PWM on GPIO16).

## Method

Every project pin was, as the SDK left it, `Pin.IN` with `PULL_DOWN`, except GPIO16 (clk), which is the
clock PWM with its output enabled: it was read passively and not touched (class `mcu-configured`).
For every other net the script set a pull-down, waited 2 ms, read three times, set a pull-up, waited,
read three times, and put the pin back (Pin.IN, pull as found). Nothing was driven; the PWM registers
were identical before and after; every pad read back as found. For the pairs `ui_in[k]`/`uio[k]`, k = 1..3,
one got a pull-up and the other a pull-down, then the reverse. The SDK's own GPIO constants
(`GPIOMapTTDBv3`) matched our list for all 26 nets.

## Table (run 1; run 2 identical but for the level of `uo_out[6]`)

| Net | GPIO | pull-down read | pull-up read | class |
|---|---|---|---|---|
| ui_in[0..7] | 17..24 | 0 | 1 | floating |
| uio[0..5] | 25..30 | 0 | 1 | floating |
| uio[6], uio[7] | 31, 32 | 1 | 1 | driven-high |
| uo_out[0..4], [6] | 33..37, 39 | 0 | 0 | driven-low (run 2: [6] high) |
| uo_out[5], [7] | 38, 40 | 1 | 1 | driven-high |
| clk | 16 | n/a | n/a | mcu-configured (PWM output) |
| rst_n | 14 | 1 | 1 | driven-high |

Tie evidence (a = `ui_in[k]`, b = `uio[k]`, readings [a, b]):

| k | a pull-up, b pull-down | a pull-down, b pull-up |
|---|---|---|
| 1, 2, 3 | [1, 1] | [1, 1] |

Independent nets would read [1, 0] then [0, 1]. Equal readings both ways, with the pull-up winning the fight,
is what one net gives. This is evidence, not proof. Ties: `ui_in[1]`-`uio[1]`, `ui_in[2]`-`uio[2]`,
`ui_in[3]`-`uio[3]`; none undetermined.

## What it means

- Class names. With only weak pulls and no driving, "held by a driver" and "held by a stiff resistor" read
  the same, so `driven-*` means "something stronger than the MCU's pull holds it". `rst_n` is the known
  external pull-up on GPIO14, not a driver. `mcu-configured` is an addition to the brief's classes, for the
  clock pin only.
- All eight `ui_in` bits have nothing but the pulls on them: `mcu_may_drive` = 0..7. The SDK leaves them with a
  pull-down, so undriven they read 0.
- `ui_in[1:3]` and `uio[1:3]` are one net each. If the MCU drives `ui_in[k]` (k = 1..3), `uio_in[k]` follows it,
  and a design that enables `uio[k]` as an output fights the MCU. So for such designs the mask must drop
  bits 1..3 (the design document's rule); the profile's mask is the board's capability.
- `uio[0..5]` float (read 0 with the SDK's pull-down). `uio[6]` and `uio[7]` read high under both pulls.
  Whether the board or the idle display design holds them could not be separated with one design loaded:
  the idle display drives `uo_out` (one bit changed between runs), and these two may be its outputs too.
- Stimulus: `{"ui_in": 0, "uio_in": 192, "ui_in_mask": 255}`: the MCU may set every `ui_in` bit, nothing is
  held, and `uio_in` 0xC0 is the two high `uio` nets. Treat `uio_in` bits 6..7 as uncertain until a census
  with another design loaded.
- Against the design document: `ui_in[0]` does NOT read 1; it floats (0 under pull-down, 1 under pull-up).
  The tie of `ui_in[1:3]` to `uio[1:3]` is confirmed.
- Two runs (09:25 and 09:27) agree on every net except the level of `uo_out[6]`, an output of the animated
  idle display. The comparison rule that ignores `uo_out` levels was written after seeing this; it was not
  repeated a third time.

## Pi side (read-only, `pi-sw2-p35`)

The alias `pi-sw2-p35` does not resolve from this host; the documented route worked:
`ssh -J tweed.welland.mithis.com pi@10.21.2.35` (hostname `pi-sw2-p35`, Linux 6.12.109+rpt-rpi-v8).
`pinctrl get`: GPIO0..27 all inputs (no output, no alternate function except GPIO15 = RXD0), pulls
mixed: up on 0-21 and 26, down on 22-25 and 27. Which GPIOs the Pmod HAT uses was not determinable from this. `lsmod`: no SPI module loaded (i2c_dev, i2c_bcm2835,
i2c_brcmstb present). `/boot/firmware/config.txt` does not exist. `fpgas-tt.service` active since 2026-11-20;
the journal shows the idle display streaming `tt_fpga_platform.bin` "was: tt_um_factory_test" after the
SDK's reset.

## Sessions

| | info (read-only) | census 1 | census 2 |
|---|---|---|---|
| started | 09:21:49 | 09:25:01 | 09:26:56 |
| pre-connect wait | 4.62 s | 4.58 s | 4.61 s |
| connect | 0.073 s | 0.067 s | 0.087 s |
| first read after open (0.3 s) | clients 0 | clients 1 (state still loaded) | clients 0 |
| confirmed (clients 1) | 15.48 s after open | 15.52 s | 15.43 s |
| connected | 15.9 s | 18.6 s | 18.6 s |
| exit sequence | `02` (Ctrl-B) only | `03 03`, `02`, `04` | same |
| outcome | ok | ok | ok |
| `loaded` after disconnect | 69.6 s (no reset) | 71.9 s | 71.9 s |

The read-only session returned to `loaded` without a reset, going through `waiting` for about 65 s. After
the full clean-up the SDK booted into `tt_um_factory_test` at 10 Hz (the board had 100 Hz as found), and the
daemon then streamed the idle display back. No claim or status problem occurred; no visitor appeared.
