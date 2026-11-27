# Published library bitstreams on fpga-4: hardware against simulation

Date: 2026-11-27, 06:46-09:32 UTC, in three batches with gaps, each under a broker claim on
[fpga-4](https://tinytapeout.fpgas.online/board/fpga-4/) (RP2350 demo board with the FabricFox iCE40UP5K
breakout; Pi `pi-sw2-p35`). Each design's published `.bin` from the
[FPGA library](https://data.wafer.space/big-storage/tinytapeout-vga-videos/fpga-library.json) was loaded
through the board's daemon, captured with [vgacap](https://github.com/mithro/vgacap) `ttcap capture` at
commit 2a1caa7 (`--mode stream --clock-hz 400000 --frames 8`: one `uo_out` sample per project clock,
4,210,688 clocks), and compared with `tthfv board compare-capture` against the design's
[`fpga-4`](../../boards/fpga-4.json) reference (Verilator, ui_in 0, uio_in 0xc0).

Driver: [`scripts/hardware_library/hw_batch.py`](../../scripts/hardware_library/hw_batch.py) with its Pi
side [`pi_helper.py`](../../scripts/hardware_library/pi_helper.py). Causes of the non-exact results:
[`classify.py`](../../scripts/hardware_library/classify.py).

Designs: every library entry with `build_status` ok, no inputs needed (`ui_in` 0 or none), no PDK
latch cells, and an fpga-4 reference: 196.

## Totals

| verdict | designs |
|---|---|
| exact | 169 |
| mismatch | 14 |
| no-match | 13 |

`exact` means every compared clock of the capture equals the reference, bit for bit, after placing the
capture's first 4,096 samples in the reference's first two frames.

## Causes of the non-exact results

`classify.py` simulates each non-exact design four ways and reports which one reproduces the board's
capture exactly:

| model | what differs from the reference |
|---|---|
| rtl | nothing (the reference itself) |
| net | the FPGA build's own synthesised netlist instead of the RTL |
| rtl_late | the clock runs 1,000,000 clocks before reset is applied |
| net_late | both |

| cause | meaning | designs |
|---|---|---|
| synthesis differs | only `net` matches | 13 |
| clock before reset | `rtl_late` matches | 8 |
| both | only `net_late` matches | 2 |
| board history | no model matches | 4 |

- **Synthesis differs.** The design reads bits outside a vector (for example `layer_a_x[86]` of a
  10-bit wire in `tt_um_harveywong85_harveywilly`) or otherwise depends on `x`. Yosys and Verilator resolve
  it differently. The board matches the netlist exactly, so the bitstream is right; the RTL reference
  cannot model it. The ASIC flow also uses Yosys.
- **Clock before reset.** The board clocks the design before ttcap's reset. A design whose reset logic is
  clocked by one of its own outputs (for example `tt_um_KK_VGA01`, `motor_handler.v:37-38`, clocked by
  vsync) never sees the edge it needs during reset. No reset sequence can fix this
  ([vgacap #73](https://github.com/mithro/vgacap/issues/73)); loading with the clock held until reset is
  released can.
- **Board history.** Registers that the reset never clears hold a value that depends on how long the
  design ran before the capture (for example the older `tt_um_rejunity_vga_logo`, whose frame counter is
  clocked by vsync). The differing-clock count changes with the pre-reset clocking in the models; this cause
  is likely, not proven.

No design was found where the FPGA differs from its own synthesised netlist.

| id | verdict | clocks compared | cause | attempts | bin sha256 |
|---|---|---|---|---|---|
| tt05/tt_um_flappy_vga_cutout1 | mismatch | 4,210,688 | clock before reset | 1 | 24a139a491ca |
| tt05/tt_um_wokwi_380409393770716161 | mismatch | 4,210,688 | clock before reset | 1 | 3faace1dfde9 |
| tt05of/tt_um_flappy_vga_cutout1 | mismatch | 4,210,688 | clock before reset | 1 | 24a139a491ca |
| tt05of/tt_um_wokwi_380409393770716161 | mismatch | 4,210,688 | clock before reset | 1 | 3faace1dfde9 |
| tt08/tt_um_nvious_graphics | no-match | - | synthesis differs | 1 | b812847aafaa |
| tt08/tt_um_quarren42_demoscene_top | no-match | - | synthesis differs | 1 | f3be9ca21be9 |
| tt08/tt_um_rejunity_vga_logo | no-match | - | board history | 1 | 45c16fe5beb3 |
| ttcad25a/tt_um_nvious_graphics | no-match | - | synthesis differs | 1 | b812847aafaa |
| ttcad25a/tt_um_quarren42_demoscene_top | no-match | - | synthesis differs | 1 | f3be9ca21be9 |
| ttcad25a/tt_um_rejunity_vga_logo | no-match | - | board history | 1 | 45c16fe5beb3 |
| ttgf0p1/tt_um_flappy_vga_cutout1 | mismatch | 4,210,688 | clock before reset | 1 | 24a139a491ca |
| ttihp0p1/tt_um_flappy_vga_cutout1 | mismatch | 4,210,688 | clock before reset | 1 | 24a139a491ca |
| ttihp0p2/tt_um_algofoogle_tinyvga_fun_wrapper | no-match | - | synthesis differs | 1 | aaccf388d2e5 |
| ttihp0p4/tt_um_snake_game | mismatch | 4,210,688 | both | 1 | 8f5cf157a719 |
| ttihp25a/tt_um_nvious_graphics | no-match | - | synthesis differs | 1 | b812847aafaa |
| ttihp25a/tt_um_quarren42_demoscene_top | no-match | - | synthesis differs | 1 | f3be9ca21be9 |
| ttihp26a/tt_um_snake_game | mismatch | 4,210,688 | both | 1 | 8f5cf157a719 |
| ttsky25a/tt_um_quarren42_demoscene_top | no-match | - | synthesis differs | 1 | f3be9ca21be9 |
| ttsky25b/tt_um_bouncing_squares | mismatch | 4,210,688 | synthesis differs | 1 | fcaa83cbf82d |
| ttsky25b/tt_um_enjimneering_tts_top | mismatch | 4,210,688 | board history | 1 | 758dac96cdde |
| ttsky25b/tt_um_flappy_vga_cutout1 | mismatch | 4,210,688 | clock before reset | 1 | 24a139a491ca |
| ttsky25b/tt_um_rejunity_vga_playground | no-match | - | synthesis differs | 1 | b225643876cc |
| ttsky26a/tt_um_goose | mismatch | 4,210,688 | synthesis differs | 1 | 2e1a6429a3dc |
| ttsky26a/tt_um_spacewar_top | mismatch | 4,210,688 | board history | 1 | 5659af5e5607 |
| ttsky26b/tt_um_28add11_latchup | no-match | - | synthesis differs | 2 | d6fbcb5f55f1 |
| ttsky26b/tt_um_KK_VGA01 | mismatch | 4,210,688 | clock before reset | 3 | 7354ab9e6292 |
| ttsky26b/tt_um_harveywong85_harveywilly | no-match | - | synthesis differs | 3 | 00d1447240e2 |

Attempts: `tt_um_harveywong85_harveywilly` and `tt_um_KK_VGA01` were also captured once with `ui_in`
driven to 0 (`ttcap --ui-in 0`); the result was the same.

## Exact

| id | clocks compared | bin sha256 |
|---|---|---|
| tt05/tt_um_no_time_for_squares_tommythorn | 4,210,688 | 19cfe8bf589e |
| tt05of/tt_um_no_time_for_squares_tommythorn | 4,210,688 | 19cfe8bf589e |
| tt07/tt_um_rejunity_vga | 4,210,688 | 0390bc7519bd |
| tt08/tt_um_favoritohjs_scroller | 4,210,688 | 86109ce12112 |
| tt08/tt_um_gfg_development_tinymandelbrot | 4,210,688 | 758a2eebf003 |
| tt08/tt_um_jmack2201 | 4,210,688 | c3ac7aed6bc4 |
| tt08/tt_um_rejunity_vga_test01 | 4,210,688 | db8a11424e7e |
| tt08/tt_um_vga_cbtest | 4,210,688 | e3357a970a5e |
| tt08/tt_um_vga_glyph_mode | 4,210,688 | 6a48e2340ab7 |
| tt08/tt_um_yuri_panchul_sea_battle_vga_game | 4,210,688 | 327bcac97141 |
| tt08/tt_um_zec_square1 | 4,210,688 | c7b2208b633b |
| tt09/tt_um_rebeccargb_vga_pride | 4,210,688 | fc261d2a79cf |
| tt09/tt_um_rejunity_vga_logo | 4,210,688 | 861374c228cf |
| tt09/tt_um_znah_vga_ca | 4,210,688 | e09ecddad7fc |
| ttcad25a/tt_um_favoritohjs_scroller | 4,210,688 | 86109ce12112 |
| ttcad25a/tt_um_gfg_development_tinymandelbrot | 4,210,688 | 758a2eebf003 |
| ttcad25a/tt_um_htfab_checkers | 4,210,688 | 366dd421b907 |
| ttcad25a/tt_um_jmack2201 | 4,210,688 | c3ac7aed6bc4 |
| ttcad25a/tt_um_luke_clock | 4,210,688 | 5c6d06416f99 |
| ttcad25a/tt_um_rejunity_vga_test01 | 4,210,688 | db8a11424e7e |
| ttcad25a/tt_um_vga_cbtest | 4,210,688 | e3357a970a5e |
| ttcad25a/tt_um_vga_glyph_mode | 4,210,688 | 6a48e2340ab7 |
| ttcad25a/tt_um_yuri_panchul_sea_battle_vga_game | 4,210,688 | 327bcac97141 |
| ttcad25a/tt_um_zec_square1 | 4,210,688 | c7b2208b633b |
| ttgf0p1/tt_um_no_time_for_squares_tommythorn | 4,210,688 | 19cfe8bf589e |
| ttgf0p2/tt_um_kbeckmann_flame | 4,210,688 | 9cd0c1d42e11 |
| ttgf0p2/tt_um_rejunity_vga_logo | 4,210,688 | 452e43f2bf68 |
| ttgf0p3/tt_um_AbAdA_2048 | 4,210,688 | c8c7c885ca3d |
| ttgf26a/tt_um_danielpenas42 | 4,210,688 | 0c03ec3961cc |
| ttgf26a/tt_um_fer_logo_music_vga | 4,210,688 | b2c84067296b |
| ttgf26a/tt_um_happyhop_deadcast2 | 4,210,688 | d5981705951b |
| ttgf26a/tt_um_uacj | 4,210,688 | 3405c7c406d8 |
| ttihp0p1/tt_um_no_time_for_squares_tommythorn | 4,210,688 | 19cfe8bf589e |
| ttihp0p2/tt_um_favoritohjs_scroller | 4,210,688 | 86109ce12112 |
| ttihp0p2/tt_um_no_time_for_squares_tommythorn | 4,210,688 | 19cfe8bf589e |
| ttihp0p2/tt_um_rebeccargb_vga_pride | 4,210,688 | fc261d2a79cf |
| ttihp0p2/tt_um_rejunity_vga_logo | 4,210,688 | 9a9a72e7ee45 |
| ttihp0p2/tt_um_vga_glyph_mode | 4,210,688 | 1f0a960db40b |
| ttihp0p3/tt_um_bleeptrack_prism | 4,210,688 | 556127a51df7 |
| ttihp0p4/tt_um_glyph_mode_hd | 4,210,688 | 58350fe767ec |
| ttihp0p4/tt_um_silicon_strummer | 4,210,688 | af3cd3c05941 |
| ttihp0p4/tt_um_urish_rings | 4,210,688 | b165c8942b5e |
| ttihp25a/tt_um_favoritohjs_scroller | 4,210,688 | 86109ce12112 |
| ttihp25a/tt_um_gfg_development_tinymandelbrot | 4,210,688 | 758a2eebf003 |
| ttihp25a/tt_um_htfab_checkers | 4,210,688 | 366dd421b907 |
| ttihp25a/tt_um_jmack2201 | 4,210,688 | c3ac7aed6bc4 |
| ttihp25a/tt_um_luke_clock | 4,210,688 | 5c6d06416f99 |
| ttihp25a/tt_um_rebeccargb_vga_pride | 4,210,688 | fc261d2a79cf |
| ttihp25a/tt_um_rejunity_vga_logo | 4,210,688 | 861374c228cf |
| ttihp25a/tt_um_vga_cbtest | 4,210,688 | e3357a970a5e |
| ttihp25a/tt_um_vga_glyph_mode | 4,210,688 | 6a48e2340ab7 |
| ttihp25a/tt_um_yuri_panchul_sea_battle_vga_game | 4,210,688 | 327bcac97141 |
| ttihp25a/tt_um_zec_square1 | 4,210,688 | c7b2208b633b |
| ttihp25a/tt_um_znah_vga_ca | 4,210,688 | e09ecddad7fc |
| ttihp25b/tt_um_bleeptrack_prism | 4,210,688 | 556127a51df7 |
| ttihp26a/tt_um_RongGi_tiny_dino | 4,210,688 | c6594157c68a |
| ttihp26a/tt_um_adriantrummer_checker | 4,210,688 | 9c4aaee9172b |
| ttihp26a/tt_um_embeddedinn_vga | 4,210,688 | 1723627fb829 |
| ttihp26a/tt_um_ezelioli_blockvga | 4,210,688 | b689d708b1a8 |
| ttihp26a/tt_um_gschultz_bouncingcheckers | 4,210,688 | 3fa67d746bf4 |
| ttihp26a/tt_um_matth_fischer_vgaTT | 4,210,688 | c244552c938f |
| ttihp26a/tt_um_mattvenn_vgatest | 4,210,688 | 4640b212edfd |
| ttihp26a/tt_um_mo_module | 4,210,688 | 44fade06b813 |
| ttihp26a/tt_um_moss_display | 4,210,688 | 435636c8eb5b |
| ttihp26a/tt_um_not_a_dinosaur | 4,210,688 | 5e35cab04688 |
| ttihp26a/tt_um_phsauter_vga_maze | 4,210,688 | cdbed40983fb |
| ttihp26a/tt_um_pmiotti_squares_hypnosis | 4,210,688 | 228956050124 |
| ttihp26a/tt_um_pong | 4,210,688 | 23d0d935d419 |
| ttihp26a/tt_um_rebeccargb_vga_pride | 4,210,688 | d25d68d3c1de |
| ttihp26a/tt_um_rejunity_vga_logo | 4,210,688 | 768008001672 |
| ttihp26a/tt_um_thomasherzog_plasma | 4,210,688 | 876e4ec4a31a |
| ttihp26a/tt_um_tobiasgreiser_move_vga_square | 4,210,688 | 9d8c2356c4d0 |
| ttihp26a/tt_um_tomolt_rasterizer | 4,210,688 | 4c038ca5a592 |
| ttihp26a/tt_um_urish_rings | 4,210,688 | b165c8942b5e |
| ttihp26a/tt_um_vga_example | 4,210,688 | 2119b296ab8b |
| ttihp26a/tt_um_vga_leonllrmc | 4,210,688 | 40407817243a |
| ttihp26a/tt_um_zouzias | 4,210,688 | d9d1a97e01d7 |
| ttsky25a/tt_um_7FM_ShadyPong | 4,210,688 | a341c519344a |
| ttsky25a/tt_um_SummerTT_HDL | 4,210,688 | 6bc5b41d8837 |
| ttsky25a/tt_um_nvious_graphics | 4,210,688 | 3cc9cddd52df |
| ttsky25a/tt_um_rebeccargb_vga_pride | 4,210,688 | 38ca0999009b |
| ttsky25a/tt_um_rom_vga_screensaver | 4,210,688 | 5dd5a972de03 |
| ttsky25a/tt_um_sjsu | 4,210,688 | 7f8f401aeabb |
| ttsky25a/tt_um_sleepwell | 4,210,688 | 8cccf908c258 |
| ttsky25a/tt_um_vga_example | 4,210,688 | f2d9400f48bc |
| ttsky25a/tt_um_vga_glyph_mode | 4,210,688 | c182536bea70 |
| ttsky25a/tt_um_vga_hello_world | 4,210,688 | 64e7911962b9 |
| ttsky25b/tt_um_lukasdragoste_vga_patterns | 4,210,688 | bc48924f2f75 |
| ttsky25b/tt_um_rebeccargb_vga_pride | 4,210,688 | 38ca0999009b |
| ttsky25b/tt_um_rom_vga_screensaver | 4,210,688 | 5dd5a972de03 |
| ttsky25b/tt_um_verilog_meetup_template_project_example | 4,210,688 | dedffa2cf775 |
| ttsky26a/tt_um_5482582_cat_vga | 4,210,688 | 49a0af4259d1 |
| ttsky26a/tt_um_AlephNaNsea_space_time_waves_and_filaments | 4,210,688 | 6afc92bde36c |
| ttsky26a/tt_um_BellaB05_Hearts | 4,210,688 | 63221eb4ed3e |
| ttsky26a/tt_um_ChristmasTree_MaligayangPasko | 4,210,688 | 35e342562a74 |
| ttsky26a/tt_um_JAIMEPRYOR0_VGA_YAY | 4,210,688 | 3b36d474c606 |
| ttsky26a/tt_um_NguyenHuuHenry_vga_project | 4,210,688 | aa9e2302d97b |
| ttsky26a/tt_um_bad_ode_plotter_vga | 4,210,688 | 730cf94406a2 |
| ttsky26a/tt_um_cmos_inverter | 4,210,688 | 89b52c5f8dc0 |
| ttsky26a/tt_um_demoscenettsky | 4,210,688 | 084646d3bfbc |
| ttsky26a/tt_um_dinukuk_MYVGA_GLIDER | 4,210,688 | b128d0558482 |
| ttsky26a/tt_um_electron65_vga | 4,210,688 | bb12458bb191 |
| ttsky26a/tt_um_gerardvt_clash_poc | 4,210,688 | 1cc44666ca45 |
| ttsky26a/tt_um_gerardvt_spade_poc | 4,210,688 | 44912e02651c |
| ttsky26a/tt_um_glyph_mode_hd | 4,210,688 | f79d18d0aa0f |
| ttsky26a/tt_um_immrudul_w7khan | 4,210,688 | d5db61df1c02 |
| ttsky26a/tt_um_jackthoene_frogger | 4,210,688 | 762d45e4ed96 |
| ttsky26a/tt_um_katomata | 4,210,688 | de1bf257fafe |
| ttsky26a/tt_um_mastensg_ttsky26a_demo | 4,210,688 | 21fa3b366a34 |
| ttsky26a/tt_um_maze | 4,210,688 | d51d86a69a63 |
| ttsky26a/tt_um_nebula | 4,210,688 | 066f70d7fca4 |
| ttsky26a/tt_um_odgrip_demoscene_ttsky26a | 4,210,688 | 6ee34359236d |
| ttsky26a/tt_um_parakeet | 4,210,688 | d910de5a0592 |
| ttsky26a/tt_um_pong | 4,210,688 | 763430f1df90 |
| ttsky26a/tt_um_rebeccargb_vga_pride | 4,210,688 | 38ca0999009b |
| ttsky26a/tt_um_rom_vga_screensaver | 4,210,688 | 5dd5a972de03 |
| ttsky26a/tt_um_scottshuynh_ad_astra | 4,210,688 | 837fcae01e38 |
| ttsky26a/tt_um_shalindra_vga_rings | 4,210,688 | 05dc7062fea1 |
| ttsky26a/tt_um_shiho_space_invaders | 4,210,688 | e364538764bf |
| ttsky26a/tt_um_shimmydee_checkers | 4,210,688 | 27aeae809b4f |
| ttsky26a/tt_um_tomvdsch_cyclonerunner | 4,210,688 | 3c35cb849dd1 |
| ttsky26a/tt_um_topadi | 4,210,688 | b68498dff2dc |
| ttsky26a/tt_um_vga_example_directional_toggle | 4,210,688 | 3fd8ecda595f |
| ttsky26a/tt_um_vga_ocarina | 4,210,688 | bdc5e36916c4 |
| ttsky26a/tt_um_vighnesh_sawant_plane | 4,210,688 | 5d89cd0fee81 |
| ttsky26a/tt_um_xxsahanaxx_hwsec_glitch | 4,210,688 | f827e5b4e32b |
| ttsky26b/tt_um_27jorge05_crc_fifo | 4,210,688 | 9f44a9a0572d |
| ttsky26b/tt_um_AlephNaNsea_decentvgachipIEEEIESIPSPH | 4,210,688 | 5af848f28710 |
| ttsky26b/tt_um_AlephNaNsea_space_time_waves_and_filaments | 4,210,688 | 6afc92bde36c |
| ttsky26b/tt_um_Edward2005lol_Slot_Machine_Top | 4,210,688 | 16606227114a |
| ttsky26b/tt_um_Halcy0nnnn | 4,210,688 | 8a17d611ea2c |
| ttsky26b/tt_um_Halcy0nnnn_1 | 4,210,688 | 570996354898 |
| ttsky26b/tt_um_IEEE_OpenSilicon_SubmissionCredits | 4,210,688 | 31115b419f10 |
| ttsky26b/tt_um_LAT | 4,210,688 | a69d423255c1 |
| ttsky26b/tt_um_RaphRaphyRofl_VerilogIEEEBounce | 4,210,688 | b1291a79c854 |
| ttsky26b/tt_um_bytex64_wave_hi | 4,210,688 | da779c47bc70 |
| ttsky26b/tt_um_calebulboaca_calebcheckers | 4,210,688 | ad978d1aaed2 |
| ttsky26b/tt_um_digitalclock | 4,210,688 | 2aac79f77864 |
| ttsky26b/tt_um_erika24 | 4,210,688 | 663dc48ec27d |
| ttsky26b/tt_um_flappy_vga_Akul18 | 4,210,688 | ab660892f4fa |
| ttsky26b/tt_um_iporre_rm121 | 4,210,688 | 51a583a76984 |
| ttsky26b/tt_um_jasonbrave_terre | 4,210,688 | 21683b6a16ea |
| ttsky26b/tt_um_leongamboa_OpenSilicon_SubmissionChapterLogo | 4,210,688 | c49a34dcdf7e |
| ttsky26b/tt_um_leozqi_onetile | 4,210,688 | 3173fc654725 |
| ttsky26b/tt_um_lfearn_latchup | 4,210,688 | f7c79ce76038 |
| ttsky26b/tt_um_llhtimlam_movingscreen | 4,210,688 | fbf4d9622d0d |
| ttsky26b/tt_um_noah_azz_demo | 4,210,688 | b7193dde0a39 |
| ttsky26b/tt_um_peterhan_ReactionGame | 4,210,688 | f5e877349ade |
| ttsky26b/tt_um_rebeccargb_vga_pride | 4,210,688 | 38ca0999009b |
| ttsky26b/tt_um_rom_vga_screensaver | 4,210,688 | 5dd5a972de03 |
| ttsky26b/tt_um_rule30_vga | 4,210,688 | 97596917c6e7 |
| ttsky26b/tt_um_rv32_vga | 4,210,688 | ba984ee5df0d |
| ttsky26b/tt_um_rwnt_vgatest | 4,210,688 | a3727fca7a05 |
| ttsky26b/tt_um_silicon_art_vga_screensaver | 4,210,688 | af2a575610a1 |
| ttsky26b/tt_um_theandelope_checkers | 4,210,688 | b165c8942b5e |
| ttsky26b/tt_um_vga | 4,210,688 | 9ec5dbb83dfd |
| ttsky26b/tt_um_vga_ascii | 4,210,688 | f00cecd2fd45 |
| ttsky26b/tt_um_vga_glyph_mode_clone | 4,210,688 | c400b3fa97c7 |
| ttsky26b/tt_um_vga_yusefkarim | 4,210,688 | 17139584b7a1 |
| ttsky26b/tt_um_zlj8800_tiny_tapeout_v2 | 4,210,688 | 20dde908ad0e |
| ttsky26c/tt_um_1DC_vga_dyoa | 4,210,688 | e560d9423066 |
| ttsky26c/tt_um_ehofmannbr_pmodvga_06 | 4,210,688 | b8b2d8369ced |
| ttsky26c/tt_um_jonahsaunders_slsvga | 4,210,688 | 75741748367f |
| ttsky26c/tt_um_np523_pong | 4,210,688 | 52ed3a255cdf |
| ttsky26c/tt_um_rebeccargb_vga_pride | 4,210,688 | 38ca0999009b |
| ttsky26c/tt_um_rom_vga_screensaver | 4,210,688 | 5dd5a972de03 |
| ttsky26c/tt_um_silicon_art_vga_screensaver | 4,210,688 | af2a575610a1 |
| ttsky26c/tt_um_urish_rings | 4,210,688 | b165c8942b5e |
| ttsky26c/tt_um_vga_ca | 4,210,688 | c388675321d9 |
