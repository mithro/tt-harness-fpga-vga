# Build and reference coverage

All 445 designs were run once (the fan-out of 2026-11-25); 85 of them ended with a failure in some stage. This page
lists each of those 85, what was wrong, and what `tthfv` does about it now. A design is never made to pass by
changing what it does: what cannot be supplied stays failed, and the reason says what is missing.

## The rules

Each rule applies only to a design that fails without it, so a design that built before gets the same yosys
script, nextpnr arguments, build digest, Verilator command line and reference key as before
([tests/test_coverage_keys.py](../tests/test_coverage_keys.py) pins them; recomputed on the real records, all 438
build digests and 410 reference keys of the fan-out are unchanged). What a rule adds is recorded in that design's
`build.json` or `reference.json` and is part of its key.

| rule | trigger | what it does | code |
|---|---|---|---|
| PDK cell models | yosys or Verilator: missing module named like a PDK cell | reads [tthfv/harness/cells/](../tthfv/harness/cells) models of the referenced cells | [tthfv/pdkcells.py](../tthfv/pdkcells.py) |
| latch cells | the models include a latch or clock gate | `synth_ice40 -latches warn`, `nextpnr --ignore-loops`, `pdk_latches: true` | [tthfv/build.py](../tthfv/build.py) |
| loop check | the models are read | a combinational loop through a model is `uses PDK delay as function` | [tthfv/pdkcells.py](../tthfv/pdkcells.py) |
| SystemVerilog retry | yosys: syntax error or an error in `SV_MODE_ERRORS` in a `.v` design | reads every file with `-sv`, `sv_reader_rule` | [tthfv/build.py](../tthfv/build.py) |
| Verilog globals reset | yosys: error located in a file of the toolchain | `design -reset-vlog` before `synth_ice40`, `reset_vlog: true` | [tthfv/build.py](../tthfv/build.py) |
| absent polyfill | `src/sky130_polyfill.v` listed and absent | resolved without it, `polyfill_absent` in `fetch.json` | [tthfv/fetch.py](../tthfv/fetch.py) |
| analog or hard macro | missing module with only layout or schematics in the checkout | reason `needs analog/hard macro <names>` | [tthfv/macros.py](../tthfv/macros.py) |
| empty module at place and route | nextpnr: unsupported cell type that the design declares | reason `needs analog/hard macro <name>` | [tthfv/build.py](../tthfv/build.py) |
| gone repository | git: `Repository not found` | reason `repository not available: ...` | [tthfv/fetch.py](../tthfv/fetch.py) |
| Wokwi project | empty source list, macro `tt_um_wokwi_*` | reason `needs generated source <macro>.v` | [tthfv/fetch.py](../tthfv/fetch.py) |
| own latches | `synth_ice40` latch check fails, no PDK models | reason `design error under yosys: N latches, ...` | [tthfv/build.py](../tthfv/build.py) |
| ladder | a `synth-fail` record without `ladder` 1 | not reused, built again | [tthfv/build.py](../tthfv/build.py) |
| publish | `pdk_latches: true` | skipped: `uses PDK latch cells; not trustworthy on an FPGA` | [tthfv/publish.py](../tthfv/publish.py) |
| results | a failed stage with a reason | listed under `Failure reasons:` in `results.md` | [tthfv/results.py](../tthfv/results.py) |

## Classes

| class | meaning |
|---|---|
| A | PDK cells instantiated directly |
| B | SystemVerilog constructs in `.v` files |
| C | modules missing from the source list |
| D | yosys or Verilator errors of other kinds |
| E | fetch failures |
| F | references without sync |
| G | place and route |
| - | does not fit; no rule applies |

## Designs

New statuses are from runs in a separate work directory (`tthfv run all --only <id> --jobs 4`); "not rerun" means
the design was classified from the fan-out's records only.

| id | stage | old status | class | new fetch / build / reference | change or reason |
|---|---|---|---|---|---|
| tt06/tt_um_alexsegura_pong | fetch | fetch-fail | E | fetch-fail / not-run / not-run | repository not available |
| tt06/tt_um_algofoogle_tt06_grab_bag | build | synth-fail | C | ok / synth-fail / verilate-fail | needs analog/hard macro controller, inverter, r2r |
| tt05of/tt_um_no_time_for_squares_tommythorn | build | pnr-fail | G | ok / ok / ok | free-space rule tripped by other writers; rerun only |
| tt07/tt_um_algofoogle_raybox_zero | build | synth-fail | B | ok / timing-fail / ok | read with -sv (static cast) |
| tt08/tt_um_MichaelBell_canon | build | synth-fail | A | ok / timing-fail / ok | sky130 models; latch cells: not published |
| tt08/tt_um_MichaelBell_mandelbrot | build | synth-fail | A | ok / timing-fail / ok | sky130 models; latch cells: not published |
| tt07/tt_um_emern_top | build | no-fit | - | not rerun | does not fit the UP5K |
| tt08/tt_um_algofoogle_tt08_vga_fun | build | synth-fail | C | ok / synth-fail / verilate-fail | needs analog/hard macro controller, csdac_nom, segdac, thermo2bit |
| tt08/tt_um_crispy_vga | reference | no-sync | F | not rerun | no sync under fixed inputs; video pipeline agrees |
| tt08/tt_um_dendraws_donut | build | synth-fail | B | ok / timing-fail / ok | read with -sv (static cast) |
| tt08/tt_um_patater_demokit | build | synth-fail | D | ok / synth-fail / ok | design error under yosys: inferred latch |
| tt08/tt_um_toivoh_demo | build | synth-fail | A | ok / timing-fail / ok | sky130 models; latch cells: not published |
| tt09/tt_um_rebeccargb_colorbars | build | synth-fail | D | ok / ok / ok | design -reset-vlog (file-scope parameter Q) |
| tt09/tt_um_rebeccargb_vga_timing_experiments | reference | no-sync | F | not rerun | needs a write pulse on ui_in[7] |
| tt09/tt_um_rejunity_atari2600 | build | synth-fail | C | ok / synth-fail / verilate-fail | needs analog/hard macro rom_2600_0 to rom_2600_3 |
| tt09/tt_um_toivoh_demo | build | synth-fail | A | ok / timing-fail / ok | sky130 models; latch cells: not published |
| tt09/tt_um_znah_vga_ca | build | synth-fail | A | ok / ok / ok | sky130 models; delay cell |
| ttcad25a/tt_um_MichaelBell_canon | build | synth-fail | A | ok / timing-fail / ok | sky130 models; latch cells: not published |
| ttcad25a/tt_um_MichaelBell_mandelbrot | build | synth-fail | A | ok / timing-fail / ok | sky130 models; latch cells: not published |
| ttcad25a/tt_um_algofoogle_tt08_vga_fun | build | synth-fail | C | ok / synth-fail / verilate-fail | needs analog/hard macro controller, csdac_nom, segdac, thermo2bit |
| ttcad25a/tt_um_algofoogle_raybox_zero | build | synth-fail | B | ok / no-fit / ok | read with -sv (static cast); then does not fit the UP5K |
| ttcad25a/tt_um_crispy_vga | reference | no-sync | F | not rerun | no sync under fixed inputs; video pipeline agrees |
| ttcad25a/tt_um_dendraws_donut | build | synth-fail | B | ok / timing-fail / ok | read with -sv (static cast) |
| ttcad25a/tt_um_patater_demokit | build | synth-fail | D | ok / synth-fail / ok | design error under yosys: inferred latch |
| ttgf0p1/tt_um_algofoogle_raybox_zero | build | synth-fail | B | ok / timing-fail / ok | read with -sv (static cast) |
| ttgf0p2/tt_um_algofoogle_raybox_zero | build | synth-fail | B | ok / no-fit / ok | read with -sv (static cast); then does not fit the UP5K |
| ttgf0p2/tt_um_algofoogle_vgaringosc | build | synth-fail | A | ok / synth-fail / verilate-fail | uses PDK delay as function (ring oscillator) |
| ttgf0p2/tt_um_htfab_vga_tester | reference | no-sync | F | not rerun | needs its QSPI memory; video pipeline agrees |
| ttgf0p3/tt_um_algofoogle_gf_analog | build | synth-fail | C | ok / synth-fail / verilate-fail | needs analog/hard macro bufinv_2, controller, csringosc, fastblock, vco_combo |
| ttgf26a/tt_um_pixel_processor | fetch | fetch-fail | E | fetch-fail / not-run / not-run | VHDL sources are not supported |
| ttgf26a/tt_um_uacj_bouncing_DVD_screensaver | build | synth-fail | D | ok / timing-fail / ok | read with -sv (declaration in unnamed block) |
| ttgf26a/tt_um_htfab_vga_tester | reference | no-sync | F | not rerun | needs its QSPI memory; video pipeline agrees |
| ttgf26b/tt_um_LukeSilva_cartrip | build | synth-fail | C | ok / synth-fail / verilate-fail | needs analog/hard macro tt_gf_cartrip_lg_nameplate |
| ttgf26b/tt_um_ljhahne_pong | build | synth-fail | B | ok / timing-fail / ok | read with -sv (omitted parameter default) |
| ttihp0p1/tt_um_algofoogle_raybox_zero | build | synth-fail | B | ok / timing-fail / ok | read with -sv (static cast) |
| ttihp0p2/tt_um_MichaelBell_canon | build | synth-fail | D | ok / synth-fail / ok | design error under yosys: latches of its own sky130_polyfill.v |
| ttihp0p2/tt_um_algofoogle_raybox_zero | build | synth-fail | B | ok / timing-fail / ok | read with -sv (static cast) |
| ttihp0p2/tt_um_crispy_vga | reference | no-sync | F | not rerun | no sync under fixed inputs; video pipeline agrees |
| ttihp0p2/tt_um_rebeccargb_colorbars | build | synth-fail | D | ok / ok / ok | design -reset-vlog (file-scope parameter Q) |
| ttihp0p3/tt_um_algofoogle_antonalog | build | synth-fail | C | ok / synth-fail / verilate-fail | needs analog/hard macro controller, r2r_dac, rgb_buffers |
| ttihp0p4/tt_um_MichaelBell_photo_frame | reference | no-sync | F | not rerun | needs its QSPI memory; video pipeline agrees |
| ttihp0p4/tt_um_algofoogle_raybox_zero | build | synth-fail | B | ok / no-fit / ok | read with -sv (static cast); then does not fit the UP5K |
| ttihp0p4/tt_um_algofoogle_vgaringosc | build | synth-fail | A | ok / synth-fail / verilate-fail | uses PDK delay as function (ring oscillator) |
| ttihp25a/tt_um_MichaelBell_canon | fetch | fetch-fail | E | ok / timing-fail / ok | absent polyfill; sky130 models; latch cells: not published |
| ttihp25a/tt_um_MichaelBell_mandelbrot | fetch | fetch-fail | E | ok / timing-fail / ok | absent polyfill; sky130 models; latch cells: not published |
| ttihp25a/tt_um_crispy_vga | reference | no-sync | F | not rerun | no sync under fixed inputs; video pipeline agrees |
| ttihp25a/tt_um_dendraws_donut | build | synth-fail | B | ok / timing-fail / ok | read with -sv (static cast) |
| ttihp25a/tt_um_patater_demokit | build | synth-fail | D | ok / synth-fail / ok | design error under yosys: inferred latch |
| ttihp25a/tt_um_rebeccargb_colorbars | build | synth-fail | D | ok / ok / ok | design -reset-vlog (file-scope parameter Q) |
| ttihp25a/tt_um_rebeccargb_vga_timing_experiments | reference | no-sync | F | not rerun | needs a write pulse on ui_in[7] |
| ttihp25a/tt_um_toivoh_demo_deluxe | fetch | fetch-fail | E | ok / timing-fail / ok | absent polyfill; sky130 models; latch cells: not published |
| ttihp25a/tt_um_toivoh_demo_tt08 | build | synth-fail | C | ok / synth-fail / verilate-fail | memories.sv missing from the shuttle's source list |
| ttihp25a/tt_um_znah_vga_ca | fetch | fetch-fail | E | ok / ok / ok | absent polyfill; sky130 models; delay cell |
| ttihp26a/tt_um_MichaelBell_photo_frame | reference | no-sync | F | not rerun | needs its QSPI memory; video pipeline agrees |
| ttihp26a/tt_um_lkhanh_vga_trng | build | synth-fail | A | ok / synth-fail / verilate-fail | uses PDK delay as function (ring oscillator) |
| ttihp26a/tt_um_tomolt_rasterizer | build | synth-fail | B | ok / ok / ok | read with -sv (parameter keyword) |
| ttihp26a/tt_um_vga_ca | build | synth-fail | D | ok / synth-fail / ok | design error under yosys: inferred latch |
| ttsky25a/tt_um_MichaelBell_mandelbrot | build | synth-fail | A | ok / timing-fail / ok | sky130 models; latch cells: not published |
| ttsky25a/tt_um_RoyTr16 | build | synth-fail | B | ok / timing-fail / ok | read with -sv (parameter keyword) |
| ttsky25a/tt_um_algofoogle_vga_matrix_dac | build | synth-fail | C | ok / synth-fail / verilate-fail | needs analog/hard macro controller_wrapper, csdac255 |
| ttsky25a/tt_um_nitelich_conway | reference | no-sync | F | not rerun | hsync and vsync swapped; video pipeline agrees |
| ttsky25a/tt_um_rebeccargb_colorbars | build | synth-fail | D | ok / ok / ok | design -reset-vlog (file-scope parameter Q) |
| ttsky25a/tt_um_rebeccargb_vga_timing_experiments | reference | no-sync | F | not rerun | needs a write pulse on ui_in[7] |
| ttsky25a/tt_um_rejunity_atari2600 | build | synth-fail | C | ok / synth-fail / verilate-fail | needs analog/hard macro rom_2600_0 to rom_2600_3 |
| ttsky25a/tt_um_spacewar | reference | no-sync | F | not rerun | needs gamepad input; video pipeline agrees |
| ttsky25b/tt_um_algofoogle_vga_matrix_dac | build | synth-fail | C | ok / synth-fail / verilate-fail | needs analog/hard macro controller_wrapper, csdac255 |
| ttsky25b/tt_um_tiny_shader_v2_mole99 | build | synth-fail | A | ok / timing-fail / ok | sky130 models; delay cells |
| ttsky25b/tt_um_verilog_meetup_template_project_example | build | synth-fail | B | ok / ok / ok | read with -sv (wildcard port connection) |
| ttsky25b/tt_um_pongsagon_tinygpu_v2 | build | no-fit | - | not rerun | does not fit the UP5K |
| ttsky26a/tt_um_algofoogle_vga_matrix_dac | build | synth-fail | C | ok / synth-fail / verilate-fail | needs analog/hard macro controller_wrapper, csdac255 |
| ttsky26a/tt_um_ds_missile_command | build | synth-fail | D | ok / timing-fail / ok | read with -sv (declaration in function body) |
| ttsky26a/tt_um_TinyGPU_v3 | build | no-fit | - | not rerun | does not fit the UP5K |
| ttsky26a/tt_um_kolontsov_journey | build | synth-fail | A | ok / timing-fail / ok | sky130 models; delay cell |
| ttsky26a/tt_um_maze | build | synth-fail | D | ok / ok / ok | read with -sv (input port declared as variable) |
| ttsky26a/tt_um_pakesson_vga_rocket | build | synth-fail | B | ok / timing-fail / ok | read with -sv (static cast) |
| ttsky26a/tt_um_rebelmike_asic_odyssey | build | synth-fail | A | ok / pnr-fail / no-sync | sky130 models; needs analog/hard macro incrementer |
| ttsky26a/tt_um_toivoh_demo | build | synth-fail | A | ok / timing-fail / ok | sky130 models; latch cells: not published |
| ttsky26a/tt_um_toivoh_demo_4tile | build | synth-fail | A | ok / timing-fail / ok | sky130 models; latch cells: not published |
| ttsky26a/tt_um_prime_quine | reference | verilate-fail | D | not rerun | design error under Verilator: '~&' as a binary operator |
| ttsky26b/tt_um_enjimneering_spi_mem | reference | no-sync | F | not rerun | needs its SPI memory; video pipeline agrees |
| ttsky26b/tt_um_abhinavputhran_raycast | build | pnr-fail | G | not rerun | nextpnr timed out (900 s per attempt) |
| ttsky26b/tt_um_sandsim_Alden_G878 | reference | no-sync | F | not rerun | 802-clock lines; video pipeline renders blank |
| ttsky26c/tt_um_htfab_vga_tester | reference | no-sync | F | not rerun | needs its QSPI memory; video pipeline agrees |
| ttsky26c/tt_um_dragonochi | build | no-fit | - | not rerun | does not fit the UP5K |
| ttsky26c/tt_um_wokwi_469806914724000769 | fetch | fetch-fail | E | fetch-fail / not-run / not-run | needs generated source (Wokwi project) |

## Summary

| outcome | designs |
|---|---|
| recovered: a bitstream and a reference with frames | 36 |
| of which with PDK latch cells, so not published | 12 |
| needs analog/hard macro | 12 |
| uses PDK delay as function | 3 |
| design error under yosys (own latches) | 5 |
| design error under Verilator | 1 |
| needs generated source (Wokwi) | 1 |
| repository not available | 1 |
| VHDL | 1 |
| source list omits a file (`memories.sv`) | 1 |
| does not fit the UP5K | 7 |
| nextpnr timed out | 1 |
| reference without sync | 16 |

## Findings by class

### A: PDK cells instantiated directly

The designs instantiate 25 distinct cells: 16 sky130_fd_sc_hd, 4 gf180mcu_fd_sc_mcu7t5v0, 1 sg13g2, 4 sg13cmos5l
(found by scanning every fetched design's compilation units). Four other designs that build today also name PDK
cells, inside `ifdef` or generate branches they do not elaborate; the rule is triggered by the tool's missing-module
error, not by the text, so their keys are untouched.

| cell kind | cells | on the FPGA |
|---|---|---|
| logic | inv, buf, clkbuf, and2, nand4, mux4, fa | exact |
| delay | dlygate4sd3, clkdlybuf4s25 | a wire; `pdk_note` says so |
| latch | dlxtp, dlxtn, dlclkp | a LUT loop; `pdk_latches: true`; not published |

### C: modules missing from the source list

| design | missing | what it is |
|---|---|---|
| [algofoogle](https://github.com/algofoogle) grab bag, vga_fun, gf_analog, antonalog, vga_matrix_dac | inverter, segdac, csringosc, r2r_dac, csdac255 | analog (magic layout, xschem) |
| same | controller, controller_wrapper | hard macro (GDS) with RTL in `verilog/`; the analog part is missing anyway |
| [rejunity atari2600](https://github.com/rejunity) | rom_2600_0 to rom_2600_3 | hard macro ROMs; an emulation model in `test/` reads `../roms/*.mem` |
| LukeSilva cartrip | tt_gf_cartrip_lg_nameplate | artwork macro: empty portless stub in `ip/` |
| ttihp25a toivoh_demo_tt08 | np_latch_registers | in `src/memories.sv`, which the project's `info.yaml` lists and the shuttle's source list omits |
| ttsky26a rebelmike_asic_odyssey | incrementer | hard macro; its function exists only under `ifdef SIM` |

### D: other yosys and Verilator errors

| error | designs | cause | outcome |
|---|---|---|---|
| `Specify DAT width does not match DST width` in `ice40/cells_sim.v` | rebeccargb_colorbars x4 | file-scope `parameter Q` leaks into the toolchain's cell library | fixed: `design -reset-vlog` |
| `Found N problems in 'check -assert'` | patater_demokit x3, vga_ca | latches inferred by the design's RTL | stays: `design error under yosys` |
| same | ttihp0p2 MichaelBell_canon | latches of the design's own `sky130_polyfill.v` | stays: `design error under yosys` |
| `implicitly declared and default_nettype none` | uacj_bouncing_DVD_screensaver | declaration in an unnamed block (SystemVerilog) | fixed: `-sv` |
| `Input port is declared as register` | maze | input port declared as a variable (SystemVerilog) | fixed: `-sv` |
| `Failed to detect width for identifier` | ds_missile_command | declaration in a function body (SystemVerilog) | fixed: `-sv` |
| `'~&' is not a 'nand' binary operator` (Verilator) | prime_quine | design uses `~&` as a binary operator | stays: design error under Verilator |

### E: fetch failures

| design | cause | outcome |
|---|---|---|
| tt06 alexsegura_pong | GitHub: `Repository not found` | stays: `repository not available` |
| ttgf26a pixel_processor | VHDL sources | stays |
| ttsky26c wokwi_469806914724000769 | empty source list: a Wokwi project whose Verilog the Tiny Tapeout flow generates | stays: needs generated source |
| ttihp25a canon, mandelbrot, toivoh_demo_deluxe, znah_vga_ca | `sky130_polyfill.v` listed, added by the IHP re-harden, absent from the project repository | fixed: absent polyfill rule |

No failure was caused by our fetch (depth-1 fetch, submodules, redirects).

### F: references without sync

| design | ours | [video pipeline](https://github.com/mithro/tinytapeout-vga-videos) | why |
|---|---|---|---|
| crispy_vga x4 | no sync | no-sync | no sync under fixed inputs |
| htfab_vga_tester x3 | no sync | no-sync | needs its QSPI memory |
| MichaelBell_photo_frame x2 | no sync | no-sync | needs its QSPI memory |
| spacewar | no sync | no-sync | needs gamepad input |
| enjimneering_spi_mem | no sync | no-sync | needs its SPI memory |
| rebeccargb_vga_timing_experiments x3 | no sync | ok with an input script | needs a write pulse on `ui_in[7]` |
| nitelich_conway | 420000-clock lines | bad-timing | hsync and vsync pins swapped |
| sandsim_Alden_G878 | 802-clock lines | blank | non-standard line length |

None is caused by our cutter or stimulus; the frame definition is unchanged.

### G: place and route

| design | outcome |
|---|---|
| tt05of no_time_for_squares_tommythorn | free-space rule tripped by other processes' writes (`not cached`); needs a rerun only |
| ttsky26b abhinavputhran_raycast | `nextpnr timed out` at the 900 s per-attempt limit (`NEXTPNR_TIMEOUT`); limit not raised |
