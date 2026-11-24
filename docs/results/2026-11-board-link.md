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
  clients 0 and `loaded` in session 1. At the time of these sessions the code waited for the first lone `1`;
  since the review it waits for a read taken at least 15 s after connecting (see below).
- **`clients` is 1 with nobody connected while the daemon restores the idle display** (one read, about 65-70 s
  after disconnect). The status rule refuses a board in that moment, which is the safe direction.
- **Time to `loaded`** was 67.4-67.6 s after disconnect, consistent with the design's "about 70 s" and the
  earlier observation of about 75 s.
- **The clean-up sequence behaves exactly as the design describes**: Ctrl-B then Ctrl-D from the friendly REPL
  runs `main.py` and the daemon restores the idle display by itself. A soft reset from the raw REPL was not
  tried.
- The status JSON carries a `kind_reason` field not in the design's sample.

## Review findings and fixes

An independent review of this work confirmed that the probe was read-only byte for byte, the register
addresses and arithmetic, and that fpga-4 was left clean. It also reproduced, against fakes, ways the session
code could harm a visitor or leave a board dirty once later tasks drive pins. All were fixed on 2026-11-23,
each proven against the in-process fake board; there was no further board contact.

- **A blind watcher** (an `http.client` error from the status endpoint killed the watcher thread silently,
  after which neither a visitor nor the time cap was noticed). Every status failure is now a failed read; ten
  seconds without a good read aborts the session; the link refuses to send or read when its watcher thread is
  dead, and enforces the connected-time cap from its own deadline.
- **SIGTERM or SIGHUP skipped the clean-up.** During a session they raise an exception handled like Ctrl-C
  (clean-up, close, release), and `tthfv board probe` exits 128 + the signal. SIGKILL cannot be handled: it
  leaves the claim until it expires and the board as the session left it.
- **The status lag could hide a visitor**: a lone `1` seen soon after connecting can be someone who connected
  just before us, with us not yet counted. Before sending anything the session now needs a read taken at least
  15 s after its WebSocket opened showing exactly one client, and no read since showing more; otherwise it
  leaves by 40 s without sending. Status reads of a board are 5 s apart, on a grid set by the read before
  connecting; the session now connects just before the grid's next point (waiting up to 5 s before it
  connects), so the confirming read is taken about 15.3 s after connecting and answered at about 15.3 s
  (measured against the fake at the real constants: 15.31 s; without the alignment it would be about 20 s).
  It was 5-10 s in the two sessions above.
- **A second Ctrl-C during clean-up** abandoned it before the Ctrl-D. Interrupts and termination signals are now
  deferred until the clean-up and the claim release are done, then re-raised; the outcome says so.
- **A visitor after we left was reported as our failure.** After a clean exit, someone using the board past the
  status lag is now `restored-unverified: board in use by someone else`. `BoardNotRestored` is kept for
  `left: ...`/`failed: ...`, an incomplete clean-up, or a board that stays `waiting`, and carries the clean-up
  report. When nothing was sent the session no longer waits.
- **The claims directory** must be named explicitly (`TTHFV_BOARD_CLAIMS`); the default under the work
  directory would have hidden other projects' claims. Claim check-and-create and release now hold an `flock`
  on a lock file in that directory (`.claims.lock`): two processes replacing the same expired claim had both
  ended up holding it.
- **Soft reset only when the session changed the board.** Every script now says whether it changes the board
  (`changes_state`, a required argument); binary writes count as changes. A session that changed something
  leaves with Ctrl-C twice, Ctrl-B and Ctrl-D (our pins or programs must not fight whatever comes next). A
  read-only session sends no Ctrl-D, whether it ends normally or because a visitor arrived, so a visitor is not
  reset under their feet (since the second review it sends even less: see below).

  Whether a read-only exit without a reset still lets the board return to `loaded`, from the daemon's
  `idle.py` and `designs.py`: once the last client has been gone 60 s, the idle display asks the board, through
  its own raw-REPL task, whether `tt` exists and what `tt.shuttle.enabled` is. If the enabled design is the
  idle design (`designs.IDLE_NAME = "idle_display"`) it sets the state to `loaded` without loading anything;
  if it is the SDK's start design or none, it streams the idle design and then says `loaded`. The probe found
  `tt` present and `enabled` = `idle_display` before it touched anything, and a read-only session changes
  neither; the daemon's task enters the raw REPL itself (Ctrl-C, Ctrl-A) and leaves it with Ctrl-B, so being at
  the friendly prompt is enough. Conclusion: a read-only session needs no soft reset for the board to return
  to `loaded`, so none is sent, alone or not. This is inferred from the daemon's code and the probe's
  readings; a read-only exit without a reset has not yet been observed on a board.
- **A dropped connection** now keeps the claim. If the session had changed the board, it waits past the
  status lag for the board to be empty, reconnects once, confirms as above and sends the full clean-up; if
  anyone else is there it touches nothing, releases the claim, and raises an error saying the board may be
  dirty and what had been done. After read-only work it leaves the board as it is.
- **The time cap** is now 540 s (so the clean-up fits inside ten minutes), counted from the moment the
  WebSocket opened and checked on every link operation; a script's timeout covers the whole call and is cut
  short by the session deadline.
- Smaller fixes: the watcher thread is joined on exit; text output containing 0x04 raises a framing error
  and the link resynchronises at the next prompt; `link.check()` for bodies doing host-side work; the rules
  for session bodies are in `tthfv/board/session.py`.

### Second review (2026-11-24)

A second review, with the real timing constants against fakes, confirmed the fixes above and found further
gaps, fixed the same way (fakes only, no board contact):

- A status server that trickled bytes, or never answered, kept the watcher blocked inside one read, so blind
  detection never fired. A status read now has a total deadline (resolve, connect, every byte), and the link
  judges blindness on the clock: no successful read for 10 s stops the session.
- After an abort the watcher used to stop polling, and the cap did not end a body that never touched the link.
  The watcher now polls until the session ends, cuts the connection itself at the cap, and the session takes a
  fresh status read before sending anything after an abort: with someone else there it sends nothing unless it
  changed the board, in which case it sends the full clean-up (the lesser harm) and says so.
- Leaving sends the least that undoes what was done: nothing if the session never entered the raw REPL, Ctrl-B
  alone at an idle raw prompt, Ctrl-C twice and Ctrl-B only if a script of ours may still be running (Ctrl-C
  would interrupt whatever a visitor just started), and the full sequence with Ctrl-D only if the board was
  changed.
- One signal handler serves the whole session; a deferred signal is never lost when the clean-up itself fails;
  a second signal ends a long reconnect; a signal during the restore check shows in the outcome.
- Writes are bounded by the session deadline; the claims lock is opened read-only and waited for at most 10 s;
  a connection is closed on any exception while opening; a read-only drop says it left the board in the raw
  REPL with nothing changed; the "board may be dirty" error names the drop time, whether a script was running
  and the changes by label.
- A minimal read-only session (confirm, one short script, leave) is now connected about 15.5 s (measured
  against the fake at the real constants), after up to 5 s of waiting before connecting.

### Third review (2026-11-24)

A third review drove 2,400 signalled sessions, 64 at a time, against the fake, and found three defects, fixed
the same way (fakes only):

- **The signal handler raised wherever the main thread happened to be**, including inside `threading` waits and
  locks; that left a lock in a bad state. In the reviewer's 2,400 runs: 3 hung (claim held, board left in the
  raw REPL) and 2 ended with "release unlocked lock"; the same stress here before the fix: 7 hung and 18 ended
  with the wrong exit status. The handler now only records the signal; the session raises it at its own safe
  points, and the session's waits poll instead of blocking. The same stress after: 2,400 of 2,400 ended with the
  signal's exit status and a complete clean-up.
- **A signal during the wait before connecting left the claim file behind** (that wait now sits inside the
  part of the session that always releases the claim; a signal there ends the session at once, with nothing
  sent and the claim released).
- **A signal stashed during the body's last receive could fire inside the clean-up**, cutting it after its
  first Ctrl-C with the outcome "ok". The clean-up no longer takes a stashed signal; the signal is re-raised
  after it, and the outcome names it.
- Smaller: a failing close no longer skips the claim release, and a claims lock busy for more than 10 s no
  longer leaves our claim behind; a signal is always the session's final exception, with any board error
  chained; "a script of ours was running" is counted only once its bytes may have left; the outcome names a
  read-only script of ours left running, says whether a clean-up was confirmed when a signal was deferred, and
  no longer reads "board taken: ;".

### What remains possible

What the protocol cannot prevent (there is no lock): `clients` lags a connection by 5-10 s and is polled every
5 s, so a visitor who arrives during our session can receive 10-15 s of our traffic before we notice,
including, for a session that changed the board's state, our clean-up's soft reset of the board. A visitor who
arrives in the last ~10 s before our confirming read is not seen by it either. After a dropped connection a
state-changing script of ours keeps running on the board for about 35 s while we wait past the lag before
reconnecting to clean up (or until someone acts, if a visitor is there and we do not reconnect). SIGKILL,
power loss or a crash of the host running the harness leave the board as it was and the claim file in place
until it expires (at most 30 minutes).

## Not done

- No third (spare) session.
- No pin census, no PIO, no design load: those are later tasks.
- fpga-1, fpga-3 and tt-fd1a167bd863a198 were not connected to (status reads only).
