# Test data

- `bars.bin.gz`: the first 1,700,000 bytes of the `uo_out.bin` dump of `calibration/tt_um_vgacal_bars`
  (one byte per clock, clock 0 = first reset clock), gzip level 6, mtime 0. Frame 0 starts at clock 393,561 (the design is quiet for a while), so it holds three whole
  640x480 frames plus a little margin. Made with `tthfv reference calibration/tt_um_vgacal_bars`
  (verilator 5.053, 6,000,000 clocks, `ui_in` = 0, `uio_in` = 0), then:

  ```python
  raw = Path("work/reference/calibration/tt_um_vgacal_bars/uo_out.bin").read_bytes()[:1_700_000]
  ```

  The tests that use it need numpy only, not the toolchain.
