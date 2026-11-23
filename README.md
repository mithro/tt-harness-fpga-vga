# tt-harness-fpga-vga

A test harness for building and validating [Tiny Tapeout](https://tinytapeout.com/) VGA
designs (the kind that run in the
[VGA Playground](https://github.com/TinyTapeout/vga-playground)) on the Tiny Tapeout FPGA
development board (iCE40UP5K).

**Status: design phase. Nothing is built yet.**

The name follows `tt-harness-<board type>-<design type>`: a Tiny Tapeout harness, for FPGA
boards, for VGA designs.

## Goal

A large library of prebuilt, known-good FPGA bitstreams of good-looking Tiny Tapeout VGA
designs, for use as demos on <https://tinytapeout.fpgas.online/> and for people who own
the board to try at home.

A bitstream enters the library once the picture it produces on a real board matches the
simulated reference render. Two ways of checking are planned:

1. With the right Pmods plugged into the board's connectors.
2. With the board's own RP2 microcontroller clocking the design and reading its pins.

The candidate designs are the ones rendered at
<https://data.wafer.space/big-storage/tinytapeout-vga-videos/>.

## Developing

```
uv sync
uv run pytest -q
uv run ruff check .
```

## Licence

Apache-2.0. See `LICENSE`.
