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

## Table (`boards/fpga-4.json`, run of 11:31; runs of 09:25, 09:27 and 10:18 agree except for `uo_out` levels)

| Net | GPIO | pull-down read | pull-up read | class | basis |
|---|---|---|---|---|---|
| ui_in[0..7] | 17..24 | 0 | 1 | floating | measured |
| uio[0..5] | 25..30 | 0 | 1 | floating | measured |
| uio[6], uio[7] | 31, 32 | 1 | 1 | driven-high | indistinguishable |
| uo_out[0..3], [5], [7] | 33..36, 38, 40 | 0 | 0 | driven-low | indistinguishable |
| uo_out[4], [6] | 37, 39 | 1 | 1 | driven-high | indistinguishable |
| clk | 16 | null | null | mcu-configured (PWM output) | not-tested |
| rst_n | 14 | 1 | 1 | driven-high | indistinguishable |

(`uo_out` levels belong to the animated idle display and change from run to run; only "driven" is compared.)

Tie tests, all eight pairs, readings [a, b] with a = `ui_in[k]`, b = `uio[k]`:

| k | a up, b down | a down, b up | verdict |
|---|---|---|---|
| 0, 4, 5 | [1, 0] | [0, 1] | independent (the negative control: the method does tell unconnected nets apart) |
| 1, 2, 3 | [1, 1] | [1, 1] | tied: consistent with a connection; a resistive link would read the same |
| 6, 7 | [1, 1] | [0, 1] | undetermined: `uio[k]` is held high, so both of its readings are 1 |

`ties` = pairs 1, 2, 3. `drive_conflicts` = `{ui_in 1, uio 1}`, `{2, 2}`, `{3, 3}`. `mcu_may_drive` = 0..7.
`stimulus` = `{"ui_in": 0, "uio_in": 192, "ui_in_mask": 255, "uio_in_uncertain": 192,
"floating_assumed_low": {"ui_in": 255, "uio": 63}}`. `effective_ui_in_mask(profile, 0xFF)` = 0xF1.
Agreement with the earlier runs: every net has the same class and reads, and the same ties, in all four
runs, except the levels of `uo_out[4..7]` (driven-low/high swap between runs, as `uo_out[6]` did before) and
the transient `uo_out[3]` of the 10:18 run, which is why a profile could not be built then.

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
  The tie of `ui_in[1:3]` to `uio[1:3]` is consistent with the design document (see the control below).
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

## Fix round 1 (review follow-up)

Profile format. `boards/fpga-4.json` as committed in b2cfd46 is the FIRST format; the format below is what
`census.py` now writes and validates; `boards/fpga-4.json` was regenerated in it on 2026-11-24 11:31 (fix round 2).

- Measured versus interpreted: every net has `basis`: `measured` (`floating`), `indistinguishable` (`driven-*`:
  equal readings cannot separate a driver from a stiff external resistor) or `not-tested` (`mcu-configured`).
  `validate_profile` checks every class against its reads.
- `stimulus.uio_in_uncertain` is the mask of `uio` bits that are `driven-*` (0xC0 on fpga-4): their level may
  be the loaded design's. `stimulus.floating_assumed_low` is the `ui_in` and `uio` bits taken as 0 only because
  the SDK left a pull-down on them (0xFF and 0x3F here). The Pi side's own pulls (about 50 kOhm, the same
  strength as the MCU's) may also sit on these nets; that is an unresolved confounder of "floating".
- Two outputs fighting. `drive_conflicts` lists every tie between a `ui_in` and a `uio` bit. The rule, in
  `census.effective_ui_in_mask(profile, uio_oe_possible)`: the profile's `ui_in_mask` minus every `ui_in` bit
  whose tied `uio` bit is in `uio_oe_possible` (the uio bits the design can ever enable as outputs; 0xFF when
  unknown). On fpga-4, unknown means bits 1..3 are dropped: 0xFF becomes 0xF1.
- Ties: all eight pairs `ui_in[k]`/`uio[k]` are tested, with pulls only, and recorded in `tie_tests` with a
  verdict (`tied`, `independent`, `undetermined`) and a reason; `ties` is derived from them.
  "tied" means equal readings both ways round: consistent with a connection; a resistive link would read the same.
- The census script restores each pin in its own try/except (a failure is reported and refused by the host),
  reads an animated net again until its reads are consistent (at most 6 times; `retries` is recorded), and
  reports `gc.mem_free()` before and after.

Negative control, from a real run on 2026-11-24 10:18 (record `work/board/fpga-4-census-20261124T101849Z.json`;
the host refused to build a profile from it, see below). Readings [a, b], a = `ui_in[k]`:

| k | a up, b down | a down, b up | verdict by the rules |
|---|---|---|---|
| 0, 4, 5 | [1, 0] | [0, 1] | independent |
| 1, 2, 3 | [1, 1] | [1, 1] | tied (consistent with a connection) |
| 6, 7 | [1, 1] | [0, 1] | undetermined (`uio[k]` is held high: both reads of b are 1) |

The control holds: the method does read [1, 0] then [0, 1] for nets that are not connected, and [1, 1] twice
only for pairs 1..3. For pairs 6 and 7 the floating `ui_in[k]` reads 0 under its pull-down while `uio[k]` reads
1, so they are not connected by anything stiffer than the pull, but the rule leaves them undetermined.

The 10:18 run's census was refused by the host: `uo_out[3]` read 1 under the pull-down and 0 under the pull-up
(the idle display changed its output between the two phases). The script now reads such a net again (at most
6 times; `retries` is recorded, 0 in the 11:31 run). The session itself was fine: all 26 pads restored, PWM
registers unchanged, `restore_failed` empty, heap 415168 bytes free before and 392384 after the script
(11:31 run: 414416 and 389232).

Restore-poll oddities (M3). Census 1: the first restore poll (09:25:26) read `in use` with clients 0, two
seconds after we left: the status was still catching up. Census 2: polls at 09:28:22 and 09:28:27 read
clients 1 with state `waiting` before `loaded` at 09:28:32. The Pi journal (`journalctl -u fpgas-tt`,
09:25-09:30, read-only) shows no serial client connecting at that time, and an `idle display: streamed
tt_fpga_platform.bin into the unused board (was: tt_um_factory_test)` at 09:28:23.7, between the two polls.
So the extra client is the daemon's own idle-display task while it streams the bitstream, as the session code's
documentation says. The 10:18 session showed the same single `clients 1` poll at 10:20:19.

## Fix round 2: the census session of 11:31 and the certificate exception

At 10:38:47Z the certificate of tinytapeout.fpgas.online expired. The owner allowed checking to be disabled
temporarily; it was done as a pinned exception (`tthfv/board/tls.py`, README, `tests/test_tls_pin.py`): the
environment variable `TTHFV_BOARD_TLS_EXPIRED_PIN` holds the SHA-256 fingerprint (DER) of the certificate, and
only an expiry failure is retried, with the peer pinned before anything is sent. The pin used,
`89b589772aca204fb4fa2eaae322f889928e86fd759aaccdaa16477334232deb`, was verified with
`openssl s_client | openssl x509 -fingerprint -sha256` (serial 064AEF7EE095EED280615299520788D8A41E, notAfter
24 Nov 2026 10:38:47 GMT) and was passed in the environment for the run only, never stored in the repository.

| | census 5 (11:31) |
|---|---|
| started | 11:31:06; fpga-4 clients 0, `loaded`, no claim on it |
| pre-connect wait / connect | 4.61 s / 0.076 s |
| first read after open (0.3 s) | clients 0 |
| confirmed (clients 1) | 15.5 s after open |
| connected | 18.7 s |
| exit sequence | `03 03`, `02`, `04` (full clean-up, SDK soft reset) |
| outcome | `ok; expired certificate accepted by pin` (also in `SessionReport.tls_exception`, and one warning line on stderr) |
| `loaded` after disconnect | 71.7 s |
