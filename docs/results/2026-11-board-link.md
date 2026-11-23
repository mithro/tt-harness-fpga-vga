# Board link: first contact with fpga-4

Date: 2026-11-23. All times UTC. Board: fpga-4, USB serial `a2961e5cac65b25f`, through the public
endpoints of `tinytapeout.fpgas.online` (status `GET /board/fpga-4/status.json`, serial
`wss://tinytapeout.fpgas.online/ws/board/fpga-4/serial`), with `User-Agent: tthfv` and nothing else
identifying. Tool: `tthfv board status` and `tthfv board probe` (`tthfv/board/`), which run every
connection inside `session.board_session`: status rule, claim, connect, `clients == 1` check, a status poll
every 5 s with abort on `clients > 1`, a connected-time cap, clean-up on every exit path, claim release,
then a wait for `loaded`.

Two connected sessions were made (the budget allowed three; the spare was not needed). Nothing on the board
was changed by them other than the documented soft reset at the end of each.

## Status of the four boards

At 10:53:00, 11:11:03 and 11:16:51 (read once each time, one request per board):

| Board | USB serial | 10:53 | 11:11 | 11:16 (after both sessions) |
|---|---|---|---|---|
| fpga-1 (another session's; never connected) | 4df39a7a6856f86f | clients 1, `in use` | clients 2, `in use` | clients 2, `in use` |
| fpga-3 | 8c46329b33590ecb | clients 0, `left: tt_um_nanosilicon` | same | same |
| fpga-4 | a2961e5cac65b25f | clients 0, `loaded` | clients 0, `loaded` | clients 0, `loaded` |
| tt-fd1a167bd863a198 | fd1a167bd863a198 | clients 1, `in use` | clients 0, `loaded` | clients 0, `loaded` |

Every board reported `kind: fpga`, `vid_pid 2e8a:0005`, daemon `version 0.1.0`, and a `kind_reason` field
that the design's sample JSON does not show. fpga-4's `uptime_s` grew continuously (271,993 at 11:11,
272,342 at 11:16): the daemon was not restarted.

## Session 1: connect check (prompt only)

| | |
|---|---|
| Status before | 11:11:33.2, clients 0, `loaded` |
| Claim taken | `fpga-4-a2961e5cac65b25f.json`, released 11:11:45.6 |
| WebSocket open + daemon's `{"event": "board", "present": true, ...}` notice | 0.062 s |
| Status reads after connect | 11:11:38.2 clients **0** `loaded`; 11:11:43.3 clients **1** `in use` |
| Sent | `\r` (reply `\r\n>>> `), then clean-up `03 03`, `02`, `04` |
| Connected time | 12.3 s (10 s of it waiting for the status to count us) |
| Disconnected | 11:11:45.6 |
| `loaded` again | 11:12:53.3, **67.6 s** after disconnect |

## Session 2: read-only probe

| | |
|---|---|
| Status before | 11:15:13.0, clients 0, `loaded` |
| Claim taken | `fpga-4-a2961e5cac65b25f.json`, released 11:15:20.7 |
| WebSocket open + board notice | 0.066 s |
| Status read after connect | 11:15:18.1, clients **1**, `in use` |
| Sent | `03 03` (reply `\r\n>>> \r\n>>> `), `01` (reply `\r\nraw REPL; CTRL-B to exit\r\n>` in 4 ms), the probe script + `04`, then clean-up `03 03`, `02`, `04` |
| Probe round trip | 0.112 s from sending the script to the final `>` (`OK` after 15 ms) |
| Connected time | 7.6 s |
| Disconnected | 11:15:20.7 |
| `loaded` again | 11:16:28.1, **67.4 s** after disconnect |

### What the probe read

The script only reads: `sys.implementation`, `sys.version`, `os.uname()`, `machine.freq()`, `gc.mem_free()`
before and after `gc.collect()`, `gc.mem_alloc()`, attributes of the SDK's existing global `tt` (it checks
`'tt' in globals()` and never constructs one), and five `machine.mem32` reads (plus four more only if RESETS
says PWM is out of reset). It configures no pin, touches no PWM, writes no file and loads no design. The full
source is `tthfv/board/probe.py`.

| Item | Value |
|---|---|
| Firmware | MicroPython `1.29.0-preview`, build `b006887db6 on 2026-08-11 (GNU 13.2.1 MinSizeRel)`; `sys.version` `3.4.0; MicroPython b006887db6 on 2026-08-11` |
| Machine | `TinyTapeout RP2350B Core with RP2350` |
| SDK | `tt` present; `tt.version` `3.1.0`, `tt.revision` `baaf0a2758b475d6e099b8ef65dbd1b5667ea0aa`, `tt.shuttle.run` `FPGA` |
| Enabled design | `idle_display` (the daemon's idle design) |
| SDK mode | `ASIC_MANUAL_INPUTS` |
| Project clock | `tt.is_auto_clocking` True, `tt.auto_clocking_freq` 100 (Hz) |
| System clock | `machine.freq()` 133,000,000 |
| Heap | `mem_free` 317,264 before `gc.collect()`, 318,464 after; `mem_alloc` 169,216 |
| `SYSINFO_CHIP_ID` (0x40000000) | 0x30004927: revision field 3, part 0x0004, manufacturer 0x493 |
| `IO_BANK0_GPIO16_CTRL` (0x40028084) | 0x00000004: FUNCSEL 4 = PWM_A_0 |
| `IO_BANK0_GPIO16_STATUS` (0x40028080) | 0x00002000: output enable to pad 1; output level 0 at that instant |
| `RESETS_RESET_DONE` (0x40020008) | 0x13ffffff (PWM bit 16 set: out of reset) |
| `PWM_CH0_CSR` (0x400a8000) | 0x1: slice 0 enabled |
| `PWM_EN` (0x400a80f0) | 0x1: slice 0 enabled |
| `PWM_CH0_DIV` (0x400a8004) | 325 = 20 + 5/16 = 20.3125 |
| `PWM_CH0_TOP` (0x400a8010) | 65,476 |

Register addresses and field layouts are from pico-sdk 2.1.1, `src/rp2350/hardware_regs/include/hardware/regs/`
(`addressmap.h`: SYSINFO_BASE 0x40000000, RESETS_BASE 0x40020000, IO_BANK0_BASE 0x40028000, PWM_BASE
0x400a8000; `sysinfo.h`, `resets.h`, `io_bank0.h`, `pwm.h` for the offsets and bits). GPIO16 is PWM slice 0
channel A by `PWM_GPIO_SLICE_NUM` in `rp2_common/hardware_pwm/include/hardware/pwm.h`. The register readings
agree with the SDK's own answer: 133 MHz / 20.3125 / (65,476 + 1) = 100.0 Hz.

The chip-id revision field reads 3. Which silicon stepping that number corresponds to was not established
from a source here, so no stepping letter is claimed.

## Clean-up and return to `loaded`

Both sessions ended with the same sequence and the same result:

1. `03 03` (Ctrl-C twice): `\r\n>>> \r\n>>> ` (session 1, from the friendly REPL) or nothing visible (session 2,
   from the raw REPL).
2. 300 ms later `02` (Ctrl-B): the friendly banner `MicroPython b006887db6 on 2026-08-11; TinyTapeout RP2350B
   Core with RP2350 ... >>> ` within 5 ms.
3. `04` (Ctrl-D) from the friendly REPL: `MPY: soft reboot`, then the SDK's `main.py`: GPIOs to inputs,
   `Detected TTDBv3 [3.2] demoboard`, mode `ASIC_MANUAL_INPUTS`, `Enable design tt_um_factory_test` (104,090
   bytes streamed in about 1.4 s), system clock reset to 125 MHz and then set to 133 MHz, `Clocking at 10Hz`,
   project reset toggled, its heap report (`GC: total: 487680, used: 58736, free: 428944`), `boot done: 1940`,
   `tt.sdk_version=3.1.0`, and the friendly prompt. From Ctrl-D to the prompt: about 2.0 s.
4. Disconnect, claim released at the same millisecond.

The status then read `waiting` (clients 0) for about 60 s, then for one read clients **1** with state still
`waiting` (11:12:48.3 and 11:16:23.1: the daemon's own REPL task asking the board and streaming the idle
design; no visitor), then clients 0 `loaded`: 67.6 s and 67.4 s after disconnect.

Final check, 11:16:51: fpga-4 `{"board": {"chip": "fpga", "device": "/dev/ttboard", "present": true,
"usb_serial": "a2961e5cac65b25f", "vid_pid": "2e8a:0005"}, "clients": 0, "idle_display": {"design":
"/usr/share/fpgas-online/tt-fpga/bitstreams/tt-display-tt-fpga/tt_fpga_platform.bin", "state": "loaded"},
"kind": "fpga", "kind_reason": "/run/fpgas-online/verify.json says board a2961e5cac65b25f carries fpga",
"reachable": true, "safe_start": {"state": "not a chip board"}, "uptime_s": 272342, "version": "0.1.0"}`.
The claims directory was empty.

## Differences from the hardware validation design

- **System clock is 133 MHz, not 150 MHz.** The SDK's `main.py` sets 133 MHz at every start (its log line
  says "RP2040 system clock" on this RP2350). The design's "150 MHz / 6 = 25.0 MHz" full-speed clock does not
  exist at 133 MHz (133 / 5 = 26.6, 133 / 6 = 22.2 MHz). Full-speed windows will need the harness to set the
  system clock itself, or to use other divisors.
- **Free heap is about 318 KB with the idle display loaded**, not the 426 KB the design quotes. Right after a
  soft reset the SDK reports 428,944 free (that matches the design); the 318 KB was measured with the daemon's
  idle design loaded and after `gc.collect()`. A RAM capture window or a bitstream buffer must be sized for
  the state the harness actually finds, or the harness must start from a fresh soft reset.
- **While idle, the project clock is running**: PWM slice 0 drives GPIO16 at 100 Hz and `tt.is_auto_clocking` is
  True; after a soft reset (factory test) it is 10 Hz. The design's rule (stop the SDK's PWM with the SDK's own
  call before touching the pad) applies from the first command of every session.
- **`clients` lags the connection by up to about 10 s.** The first status read 5 s after connecting still showed
  clients 0 and `loaded` in session 1. The session therefore waits (up to 20 s) for clients to reach 1 before
  sending anything, rather than reading once.
- **`clients` is 1 with nobody connected while the daemon restores the idle display** (one read, about 65-70 s
  after disconnect). The status rule refuses a board in that moment, which is the safe direction.
- **Time to `loaded`** was 67.4-67.6 s after disconnect, consistent with the design's "about 70 s" and the
  earlier observation of about 75 s.
- **The clean-up sequence behaves exactly as the design describes**: Ctrl-B then Ctrl-D from the friendly REPL
  runs `main.py` and the daemon restores the idle display by itself. A soft reset from the raw REPL was not
  tried.
- The status JSON carries a `kind_reason` field not in the design's sample.

## Not done

- No third (spare) session.
- No pin census, no PIO, no design load: those are later tasks.
- fpga-1, fpga-3 and tt-fd1a167bd863a198 were not connected to (status reads only).
