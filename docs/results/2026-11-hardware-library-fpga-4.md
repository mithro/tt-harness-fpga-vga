# Published library bitstreams on fpga-4: hardware against simulation

Date: 2026-11-27, 06:46-09:31 UTC, under
[fpgas.online broker](https://tinytapeout.fpgas.online/) claims on
[fpga-4](https://tinytapeout.fpgas.online/board/fpga-4/) (RP2350 demo board, FabricFox iCE40UP5K breakout, Pi
`pi-sw2-p35`).

| run | start (UTC) | end (UTC) | entries captured |
|---|---|---|---|
| 1 | 06:46 | 07:26 | 69 |
| 2 | 07:26 | 07:28 | 2 |
| 3 | 07:55 | 08:34 | 72 |
| 4 | 08:58 | 09:31 | 58 |

Runs 1 and 3 stopped at their time budget; run 2 recaptured 2 designs. 1 further start (08:52) ran no design.

## Method

- **Designs.** Every entry of the
  [FPGA library](https://data.wafer.space/big-storage/tinytapeout-vga-videos/fpga-library.json) with
  `build_status` ok, `ui_in` 0 or none, no PDK latch cells and an fpga-4 reference:
  196 entries, 144 distinct bitstreams (designs submitted to
  several shuttles).
- **Load.** The published `.bin`, through the board's fpgas-tt daemon.
- **Capture.** [ttcap](https://github.com/mithro/vgacap/tree/2a1caa703dc0196feac0f9fd5d7f6dfcc443f892/python/ttcap) `capture` at vgacap
  [2a1caa7](https://github.com/mithro/vgacap/commit/2a1caa703dc0196feac0f9fd5d7f6dfcc443f892), `--mode stream --clock-hz 400000 --frames 8`
  (capture header `mode=extclk`): the RP2350 drives the project clock and records `uo_out` once per clock.
- **Compare.** [tthfv](../../README.md) `board compare-capture` against the design's
  [`fpga-4`](../../boards/fpga-4.json) reference ([Verilator](https://verilator.org/), `ui_in` 0, `uio_in`
  0xc0).
- **Scripts.** Driver [`hw_batch.py`](../../scripts/hardware_library/hw_batch.py), Pi side
  [`pi_helper.py`](../../scripts/hardware_library/pi_helper.py), causes
  [`classify.py`](../../scripts/hardware_library/classify.py), this page
  [`make_report.py`](../../scripts/hardware_library/make_report.py).

## Totals

| verdict | entries |
|---|---|
| exact | 169 |
| mismatch | 14 |
| no-match | 13 |

`exact`: every compared clock of the capture equals the reference, bit for bit, after placing the capture's
first 4,096 samples in the reference's first two frames.

## Causes of the non-exact results

[`classify.py`](../../scripts/hardware_library/classify.py) simulates each non-exact design four ways
([Verilator](https://verilator.org/), same stimulus as the reference) and checks which reproduces every
capture of it exactly.

| model | design source | reset released after |
|---|---|---|
| rtl | RTL | 10 clocks |
| net | FPGA build netlist | 10 clocks |
| rtl_late | RTL | 1,000,010 clocks |
| net_late | FPGA build netlist | 1,000,010 clocks |

`rtl` is the reference itself. The netlist is the build's [Yosys](https://yosyshq.net/yosys/) output
(`out-default.json`).

| cause | model that matches | entries |
|---|---|---|
| synthesis differs | net | 13 |
| clock before reset | rtl_late | 8 |
| both | net_late only | 2 |
| unexplained | none | 4 |

- **Synthesis differs.** The RTL and the synthesised netlist behave differently, and the board follows the
  netlist. The mechanism was read in the source for two designs; for the rest only the model match is known.
  - `tt_um_harveywong85_harveywilly` reads bits far outside 10-bit wires, for example `layer_a_x[86]`
    ([`project.v:46`](https://github.com/harveywong85/harvey_and_willy/blob/5b3d55dcf3e1e9dcab816408c1e55842969ce7fa/src/project.v#L46)). Yosys makes them x; Verilator gives them values.
  - `tt_um_28add11_latchup` reads bit -1 in `xShiftReg[pix_x[9:4] -: 2]`
    ([`project.v:74`](https://github.com/28add11/tt_workshop_latchup2026/blob/40d4453982cf2ef76f53985f83110d4817e11d29/src/project.v#L74)).
- **Clock before reset.** The RTL matches once it runs freely before reset: the board clocks the design
  before ttcap's reset. The mechanism was read in the source for one design; for the rest only the model
  match is known.
  - `tt_um_KK_VGA01` samples reset only on vsync rising edges
    ([`motor_handler.v:37-38`](https://github.com/magnetoField/ttsky-zuzel-ieee/blob/3cb736cc17304add62f5cf4f2de477c5041dc56c/src/motor_handler.v#L37-L38)). On the board vsync is already high
    when reset starts, so its game state is never reset.
  - No reset sequence can fix this ([vgacap #73](https://github.com/mithro/vgacap/issues/73)). A load that holds the clock until
    reset is released (planned vgacap PR 4d) should; that is untested.
- **Unexplained.** No model reproduces the capture; the cause is not established.
  - `tt_um_rejunity_vga_logo` (tt08, ttcad25a) clears a frame counter on reset inside
    `always @(posedge vsync)` ([`project.v:335`](https://github.com/rejunity/tt08-huge-480x480-tiny-tapeout-logo-in-1-tile/blob/db5615f0a8d3fd5ee738526bbc2e877fc608d1d2/src/project.v#L335)), the same pattern as
    `tt_um_KK_VGA01`, yet `rtl_late` does not match it.

The netlist models (`net` or `net_late`) reproduce the board exactly for 20 of the
27 non-exact entries. They do not for these 7:

- `tt05/tt_um_wokwi_380409393770716161` (clock before reset)
- `tt05of/tt_um_wokwi_380409393770716161` (clock before reset)
- `tt08/tt_um_rejunity_vga_logo` (unexplained)
- `ttcad25a/tt_um_rejunity_vga_logo` (unexplained)
- `ttsky25b/tt_um_enjimneering_tts_top` (unexplained)
- `ttsky26a/tt_um_spacewar_top` (unexplained)
- `ttsky26b/tt_um_KK_VGA01` (clock before reset)

For the clock-before-reset entries in this list, `rtl_late` matches but `net_late` does not; why is not
established.

| id | verdict | clocks compared | cause | captures | bin sha256 |
|---|---|---|---|---|---|
| tt05/tt_um_flappy_vga_cutout1 | mismatch | 4,210,688 | clock before reset | 1 | 24a139a491ca |
| tt05/tt_um_wokwi_380409393770716161 | mismatch | 4,210,688 | clock before reset | 1 | 3faace1dfde9 |
| tt05of/tt_um_flappy_vga_cutout1 | mismatch | 4,210,688 | clock before reset | 1 | 24a139a491ca |
| tt05of/tt_um_wokwi_380409393770716161 | mismatch | 4,210,688 | clock before reset | 1 | 3faace1dfde9 |
| tt08/tt_um_nvious_graphics | no-match | - | synthesis differs | 1 | b812847aafaa |
| tt08/tt_um_quarren42_demoscene_top | no-match | - | synthesis differs | 1 | f3be9ca21be9 |
| tt08/tt_um_rejunity_vga_logo | no-match | - | unexplained | 1 | 45c16fe5beb3 |
| ttcad25a/tt_um_nvious_graphics | no-match | - | synthesis differs | 1 | b812847aafaa |
| ttcad25a/tt_um_quarren42_demoscene_top | no-match | - | synthesis differs | 1 | f3be9ca21be9 |
| ttcad25a/tt_um_rejunity_vga_logo | no-match | - | unexplained | 1 | 45c16fe5beb3 |
| ttgf0p1/tt_um_flappy_vga_cutout1 | mismatch | 4,210,688 | clock before reset | 1 | 24a139a491ca |
| ttihp0p1/tt_um_flappy_vga_cutout1 | mismatch | 4,210,688 | clock before reset | 1 | 24a139a491ca |
| ttihp0p2/tt_um_algofoogle_tinyvga_fun_wrapper | no-match | - | synthesis differs | 1 | aaccf388d2e5 |
| ttihp0p4/tt_um_snake_game | mismatch | 4,210,688 | both | 1 | 8f5cf157a719 |
| ttihp25a/tt_um_nvious_graphics | no-match | - | synthesis differs | 1 | b812847aafaa |
| ttihp25a/tt_um_quarren42_demoscene_top | no-match | - | synthesis differs | 1 | f3be9ca21be9 |
| ttihp26a/tt_um_snake_game | mismatch | 4,210,688 | both | 1 | 8f5cf157a719 |
| ttsky25a/tt_um_quarren42_demoscene_top | no-match | - | synthesis differs | 1 | f3be9ca21be9 |
| ttsky25b/tt_um_bouncing_squares | mismatch | 4,210,688 | synthesis differs | 1 | fcaa83cbf82d |
| ttsky25b/tt_um_enjimneering_tts_top | mismatch | 4,210,688 | unexplained | 1 | 758dac96cdde |
| ttsky25b/tt_um_flappy_vga_cutout1 | mismatch | 4,210,688 | clock before reset | 1 | 24a139a491ca |
| ttsky25b/tt_um_rejunity_vga_playground | no-match | - | synthesis differs | 1 | b225643876cc |
| ttsky26a/tt_um_goose | mismatch | 4,210,688 | synthesis differs | 1 | 2e1a6429a3dc |
| ttsky26a/tt_um_spacewar_top | mismatch | 4,210,688 | unexplained | 1 | 5659af5e5607 |
| ttsky26b/tt_um_28add11_latchup | no-match | - | synthesis differs | 2 | d6fbcb5f55f1 |
| ttsky26b/tt_um_KK_VGA01 | mismatch | 4,210,688 | clock before reset | 3 | 7354ab9e6292 |
| ttsky26b/tt_um_harveywong85_harveywilly | no-match | - | synthesis differs | 3 | 00d1447240e2 |

Extra captures:

- Run 3 recaptured the 3 entries that were not exact in run 1.
- Run 2 captured `tt_um_harveywong85_harveywilly` and `tt_um_KK_VGA01` with `ttcap --ui-in 0`. Whether that
  reaches the FPGA is not established: the inputs these designs read are 0 when undriven too.
- Every capture of an entry gave the same verdict, and `classify.py` checked all of them.

## Exact

All 169 compared 4,210,688 clocks in one capture.

| id | bin sha256 |
|---|---|
| tt05/tt_um_no_time_for_squares_tommythorn | 19cfe8bf589e |
| tt05of/tt_um_no_time_for_squares_tommythorn | 19cfe8bf589e |
| tt07/tt_um_rejunity_vga | 0390bc7519bd |
| tt08/tt_um_favoritohjs_scroller | 86109ce12112 |
| tt08/tt_um_gfg_development_tinymandelbrot | 758a2eebf003 |
| tt08/tt_um_jmack2201 | c3ac7aed6bc4 |
| tt08/tt_um_rejunity_vga_test01 | db8a11424e7e |
| tt08/tt_um_vga_cbtest | e3357a970a5e |
| tt08/tt_um_vga_glyph_mode | 6a48e2340ab7 |
| tt08/tt_um_yuri_panchul_sea_battle_vga_game | 327bcac97141 |
| tt08/tt_um_zec_square1 | c7b2208b633b |
| tt09/tt_um_rebeccargb_vga_pride | fc261d2a79cf |
| tt09/tt_um_rejunity_vga_logo | 861374c228cf |
| tt09/tt_um_znah_vga_ca | e09ecddad7fc |
| ttcad25a/tt_um_favoritohjs_scroller | 86109ce12112 |
| ttcad25a/tt_um_gfg_development_tinymandelbrot | 758a2eebf003 |
| ttcad25a/tt_um_htfab_checkers | 366dd421b907 |
| ttcad25a/tt_um_jmack2201 | c3ac7aed6bc4 |
| ttcad25a/tt_um_luke_clock | 5c6d06416f99 |
| ttcad25a/tt_um_rejunity_vga_test01 | db8a11424e7e |
| ttcad25a/tt_um_vga_cbtest | e3357a970a5e |
| ttcad25a/tt_um_vga_glyph_mode | 6a48e2340ab7 |
| ttcad25a/tt_um_yuri_panchul_sea_battle_vga_game | 327bcac97141 |
| ttcad25a/tt_um_zec_square1 | c7b2208b633b |
| ttgf0p1/tt_um_no_time_for_squares_tommythorn | 19cfe8bf589e |
| ttgf0p2/tt_um_kbeckmann_flame | 9cd0c1d42e11 |
| ttgf0p2/tt_um_rejunity_vga_logo | 452e43f2bf68 |
| ttgf0p3/tt_um_AbAdA_2048 | c8c7c885ca3d |
| ttgf26a/tt_um_danielpenas42 | 0c03ec3961cc |
| ttgf26a/tt_um_fer_logo_music_vga | b2c84067296b |
| ttgf26a/tt_um_happyhop_deadcast2 | d5981705951b |
| ttgf26a/tt_um_uacj | 3405c7c406d8 |
| ttihp0p1/tt_um_no_time_for_squares_tommythorn | 19cfe8bf589e |
| ttihp0p2/tt_um_favoritohjs_scroller | 86109ce12112 |
| ttihp0p2/tt_um_no_time_for_squares_tommythorn | 19cfe8bf589e |
| ttihp0p2/tt_um_rebeccargb_vga_pride | fc261d2a79cf |
| ttihp0p2/tt_um_rejunity_vga_logo | 9a9a72e7ee45 |
| ttihp0p2/tt_um_vga_glyph_mode | 1f0a960db40b |
| ttihp0p3/tt_um_bleeptrack_prism | 556127a51df7 |
| ttihp0p4/tt_um_glyph_mode_hd | 58350fe767ec |
| ttihp0p4/tt_um_silicon_strummer | af3cd3c05941 |
| ttihp0p4/tt_um_urish_rings | b165c8942b5e |
| ttihp25a/tt_um_favoritohjs_scroller | 86109ce12112 |
| ttihp25a/tt_um_gfg_development_tinymandelbrot | 758a2eebf003 |
| ttihp25a/tt_um_htfab_checkers | 366dd421b907 |
| ttihp25a/tt_um_jmack2201 | c3ac7aed6bc4 |
| ttihp25a/tt_um_luke_clock | 5c6d06416f99 |
| ttihp25a/tt_um_rebeccargb_vga_pride | fc261d2a79cf |
| ttihp25a/tt_um_rejunity_vga_logo | 861374c228cf |
| ttihp25a/tt_um_vga_cbtest | e3357a970a5e |
| ttihp25a/tt_um_vga_glyph_mode | 6a48e2340ab7 |
| ttihp25a/tt_um_yuri_panchul_sea_battle_vga_game | 327bcac97141 |
| ttihp25a/tt_um_zec_square1 | c7b2208b633b |
| ttihp25a/tt_um_znah_vga_ca | e09ecddad7fc |
| ttihp25b/tt_um_bleeptrack_prism | 556127a51df7 |
| ttihp26a/tt_um_RongGi_tiny_dino | c6594157c68a |
| ttihp26a/tt_um_adriantrummer_checker | 9c4aaee9172b |
| ttihp26a/tt_um_embeddedinn_vga | 1723627fb829 |
| ttihp26a/tt_um_ezelioli_blockvga | b689d708b1a8 |
| ttihp26a/tt_um_gschultz_bouncingcheckers | 3fa67d746bf4 |
| ttihp26a/tt_um_matth_fischer_vgaTT | c244552c938f |
| ttihp26a/tt_um_mattvenn_vgatest | 4640b212edfd |
| ttihp26a/tt_um_mo_module | 44fade06b813 |
| ttihp26a/tt_um_moss_display | 435636c8eb5b |
| ttihp26a/tt_um_not_a_dinosaur | 5e35cab04688 |
| ttihp26a/tt_um_phsauter_vga_maze | cdbed40983fb |
| ttihp26a/tt_um_pmiotti_squares_hypnosis | 228956050124 |
| ttihp26a/tt_um_pong | 23d0d935d419 |
| ttihp26a/tt_um_rebeccargb_vga_pride | d25d68d3c1de |
| ttihp26a/tt_um_rejunity_vga_logo | 768008001672 |
| ttihp26a/tt_um_thomasherzog_plasma | 876e4ec4a31a |
| ttihp26a/tt_um_tobiasgreiser_move_vga_square | 9d8c2356c4d0 |
| ttihp26a/tt_um_tomolt_rasterizer | 4c038ca5a592 |
| ttihp26a/tt_um_urish_rings | b165c8942b5e |
| ttihp26a/tt_um_vga_example | 2119b296ab8b |
| ttihp26a/tt_um_vga_leonllrmc | 40407817243a |
| ttihp26a/tt_um_zouzias | d9d1a97e01d7 |
| ttsky25a/tt_um_7FM_ShadyPong | a341c519344a |
| ttsky25a/tt_um_SummerTT_HDL | 6bc5b41d8837 |
| ttsky25a/tt_um_nvious_graphics | 3cc9cddd52df |
| ttsky25a/tt_um_rebeccargb_vga_pride | 38ca0999009b |
| ttsky25a/tt_um_rom_vga_screensaver | 5dd5a972de03 |
| ttsky25a/tt_um_sjsu | 7f8f401aeabb |
| ttsky25a/tt_um_sleepwell | 8cccf908c258 |
| ttsky25a/tt_um_vga_example | f2d9400f48bc |
| ttsky25a/tt_um_vga_glyph_mode | c182536bea70 |
| ttsky25a/tt_um_vga_hello_world | 64e7911962b9 |
| ttsky25b/tt_um_lukasdragoste_vga_patterns | bc48924f2f75 |
| ttsky25b/tt_um_rebeccargb_vga_pride | 38ca0999009b |
| ttsky25b/tt_um_rom_vga_screensaver | 5dd5a972de03 |
| ttsky25b/tt_um_verilog_meetup_template_project_example | dedffa2cf775 |
| ttsky26a/tt_um_5482582_cat_vga | 49a0af4259d1 |
| ttsky26a/tt_um_AlephNaNsea_space_time_waves_and_filaments | 6afc92bde36c |
| ttsky26a/tt_um_BellaB05_Hearts | 63221eb4ed3e |
| ttsky26a/tt_um_ChristmasTree_MaligayangPasko | 35e342562a74 |
| ttsky26a/tt_um_JAIMEPRYOR0_VGA_YAY | 3b36d474c606 |
| ttsky26a/tt_um_NguyenHuuHenry_vga_project | aa9e2302d97b |
| ttsky26a/tt_um_bad_ode_plotter_vga | 730cf94406a2 |
| ttsky26a/tt_um_cmos_inverter | 89b52c5f8dc0 |
| ttsky26a/tt_um_demoscenettsky | 084646d3bfbc |
| ttsky26a/tt_um_dinukuk_MYVGA_GLIDER | b128d0558482 |
| ttsky26a/tt_um_electron65_vga | bb12458bb191 |
| ttsky26a/tt_um_gerardvt_clash_poc | 1cc44666ca45 |
| ttsky26a/tt_um_gerardvt_spade_poc | 44912e02651c |
| ttsky26a/tt_um_glyph_mode_hd | f79d18d0aa0f |
| ttsky26a/tt_um_immrudul_w7khan | d5db61df1c02 |
| ttsky26a/tt_um_jackthoene_frogger | 762d45e4ed96 |
| ttsky26a/tt_um_katomata | de1bf257fafe |
| ttsky26a/tt_um_mastensg_ttsky26a_demo | 21fa3b366a34 |
| ttsky26a/tt_um_maze | d51d86a69a63 |
| ttsky26a/tt_um_nebula | 066f70d7fca4 |
| ttsky26a/tt_um_odgrip_demoscene_ttsky26a | 6ee34359236d |
| ttsky26a/tt_um_parakeet | d910de5a0592 |
| ttsky26a/tt_um_pong | 763430f1df90 |
| ttsky26a/tt_um_rebeccargb_vga_pride | 38ca0999009b |
| ttsky26a/tt_um_rom_vga_screensaver | 5dd5a972de03 |
| ttsky26a/tt_um_scottshuynh_ad_astra | 837fcae01e38 |
| ttsky26a/tt_um_shalindra_vga_rings | 05dc7062fea1 |
| ttsky26a/tt_um_shiho_space_invaders | e364538764bf |
| ttsky26a/tt_um_shimmydee_checkers | 27aeae809b4f |
| ttsky26a/tt_um_tomvdsch_cyclonerunner | 3c35cb849dd1 |
| ttsky26a/tt_um_topadi | b68498dff2dc |
| ttsky26a/tt_um_vga_example_directional_toggle | 3fd8ecda595f |
| ttsky26a/tt_um_vga_ocarina | bdc5e36916c4 |
| ttsky26a/tt_um_vighnesh_sawant_plane | 5d89cd0fee81 |
| ttsky26a/tt_um_xxsahanaxx_hwsec_glitch | f827e5b4e32b |
| ttsky26b/tt_um_27jorge05_crc_fifo | 9f44a9a0572d |
| ttsky26b/tt_um_AlephNaNsea_decentvgachipIEEEIESIPSPH | 5af848f28710 |
| ttsky26b/tt_um_AlephNaNsea_space_time_waves_and_filaments | 6afc92bde36c |
| ttsky26b/tt_um_Edward2005lol_Slot_Machine_Top | 16606227114a |
| ttsky26b/tt_um_Halcy0nnnn | 8a17d611ea2c |
| ttsky26b/tt_um_Halcy0nnnn_1 | 570996354898 |
| ttsky26b/tt_um_IEEE_OpenSilicon_SubmissionCredits | 31115b419f10 |
| ttsky26b/tt_um_LAT | a69d423255c1 |
| ttsky26b/tt_um_RaphRaphyRofl_VerilogIEEEBounce | b1291a79c854 |
| ttsky26b/tt_um_bytex64_wave_hi | da779c47bc70 |
| ttsky26b/tt_um_calebulboaca_calebcheckers | ad978d1aaed2 |
| ttsky26b/tt_um_digitalclock | 2aac79f77864 |
| ttsky26b/tt_um_erika24 | 663dc48ec27d |
| ttsky26b/tt_um_flappy_vga_Akul18 | ab660892f4fa |
| ttsky26b/tt_um_iporre_rm121 | 51a583a76984 |
| ttsky26b/tt_um_jasonbrave_terre | 21683b6a16ea |
| ttsky26b/tt_um_leongamboa_OpenSilicon_SubmissionChapterLogo | c49a34dcdf7e |
| ttsky26b/tt_um_leozqi_onetile | 3173fc654725 |
| ttsky26b/tt_um_lfearn_latchup | f7c79ce76038 |
| ttsky26b/tt_um_llhtimlam_movingscreen | fbf4d9622d0d |
| ttsky26b/tt_um_noah_azz_demo | b7193dde0a39 |
| ttsky26b/tt_um_peterhan_ReactionGame | f5e877349ade |
| ttsky26b/tt_um_rebeccargb_vga_pride | 38ca0999009b |
| ttsky26b/tt_um_rom_vga_screensaver | 5dd5a972de03 |
| ttsky26b/tt_um_rule30_vga | 97596917c6e7 |
| ttsky26b/tt_um_rv32_vga | ba984ee5df0d |
| ttsky26b/tt_um_rwnt_vgatest | a3727fca7a05 |
| ttsky26b/tt_um_silicon_art_vga_screensaver | af2a575610a1 |
| ttsky26b/tt_um_theandelope_checkers | b165c8942b5e |
| ttsky26b/tt_um_vga | 9ec5dbb83dfd |
| ttsky26b/tt_um_vga_ascii | f00cecd2fd45 |
| ttsky26b/tt_um_vga_glyph_mode_clone | c400b3fa97c7 |
| ttsky26b/tt_um_vga_yusefkarim | 17139584b7a1 |
| ttsky26b/tt_um_zlj8800_tiny_tapeout_v2 | 20dde908ad0e |
| ttsky26c/tt_um_1DC_vga_dyoa | e560d9423066 |
| ttsky26c/tt_um_ehofmannbr_pmodvga_06 | b8b2d8369ced |
| ttsky26c/tt_um_jonahsaunders_slsvga | 75741748367f |
| ttsky26c/tt_um_np523_pong | 52ed3a255cdf |
| ttsky26c/tt_um_rebeccargb_vga_pride | 38ca0999009b |
| ttsky26c/tt_um_rom_vga_screensaver | 5dd5a972de03 |
| ttsky26c/tt_um_silicon_art_vga_screensaver | af2a575610a1 |
| ttsky26c/tt_um_urish_rings | b165c8942b5e |
| ttsky26c/tt_um_vga_ca | c388675321d9 |
