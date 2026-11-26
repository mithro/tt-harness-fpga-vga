# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: F811 - pytest fixtures imported from other test modules
"""A result that was cut short (transient or timed_out) is rebuilt on every run; a rebuild that is cut short again
and ends worse must not replace it: the earlier record and its files are kept, with a rebuild_note. A complete
rebuild, or one of other inputs, replaces it as always."""

import hashlib
import json

from test_build import NEXTPNR, fake_tool, rec, run, toolchain  # noqa: F401
from test_timed_out import FMAX, NEXTPNR_HANG_ON_3

from tthfv import build, paths

ARGS_LINE = 'echo "$@" >> "$FAKE_REC/nextpnr.args"'
NEXTPNR_HANG_ON_2 = NEXTPNR.replace(ARGS_LINE, ARGS_LINE + '\ncase "$*" in *"--seed 2"*) sleep 60;; esac')
NEXTPNR_HANG_ALWAYS = NEXTPNR.replace(ARGS_LINE, ARGS_LINE + "\nsleep 60")
CLOCK = 20_000_000  # FMAX's figures (10 and 12 MHz) are below it: a timing-fail that keeps the best bitstream


def out_dir(d):
    return paths.design_dir("build", d.id)


def files(d) -> dict[str, str]:
    """name -> sha256 of every file in the build directory."""
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in out_dir(d).iterdir() if p.is_file()}


def cut_short_with_a_bitstream(toolchain, tmp_path, monkeypatch):
    """The first run: seeds 1 and 2 complete (12 MHz the best), seed 3 hangs and times out."""
    fake_tool(toolchain, "nextpnr-ice40", NEXTPNR_HANG_ON_3)
    monkeypatch.setattr(build, "NEXTPNR_TIMEOUT", 1)
    monkeypatch.setenv("FAKE_FMAX", FMAX)
    d, r = run(tmp_path, clock_hz=CLOCK)
    assert r.status == "timing-fail" and r.bitstream and r.fmax_hz == 12_000_000 and r.timed_out, r
    return d, r, files(d)


def test_a_rebuild_cut_short_without_a_bitstream_keeps_the_earlier_record(
    toolchain, rec, tmp_path, monkeypatch
):
    d, first, before = cut_short_with_a_bitstream(toolchain, tmp_path, monkeypatch)
    fake_tool(toolchain, "nextpnr-ice40", NEXTPNR_HANG_ALWAYS)
    _d, r = run(tmp_path, clock_hz=CLOCK)
    assert r.status == "timing-fail" and r.bitstream == "design.bin" and r.fmax_hz == 12_000_000, r
    assert "cut short" in r.rebuild_note and "pnr-fail" in r.rebuild_note, r.rebuild_note
    assert "has a bitstream" in r.rebuild_note, r.rebuild_note
    on_disk = json.loads((out_dir(d) / "build.json").read_text())
    assert on_disk["rebuild_note"] == r.rebuild_note and on_disk["bitstream_sha256"] == first.bitstream_sha256
    after = files(d)
    del before["build.json"], after["build.json"]
    assert after == before, "the build directory is the earlier run's, every file of it"
    assert not (out_dir(d) / build.HELD).exists()


def test_a_rebuild_cut_short_with_a_lower_fmax_keeps_the_earlier_record(
    toolchain, rec, tmp_path, monkeypatch
):
    d, _first, before = cut_short_with_a_bitstream(toolchain, tmp_path, monkeypatch)
    fake_tool(toolchain, "nextpnr-ice40", NEXTPNR_HANG_ON_2)  # only seed 1 (10 MHz) completes
    _d, r = run(tmp_path, clock_hz=CLOCK)
    assert r.fmax_hz == 12_000_000 and r.recipe == {"synth": "default", "seed": 2}, r
    assert "fmax" in r.rebuild_note and "10.00 MHz" in r.rebuild_note, r.rebuild_note
    assert files(d)["design.bin"] == before["design.bin"]


def test_a_complete_rebuild_replaces_the_earlier_record(toolchain, rec, tmp_path, monkeypatch):
    d, _first, _before = cut_short_with_a_bitstream(toolchain, tmp_path, monkeypatch)
    fake_tool(toolchain, "nextpnr-ice40", NEXTPNR)
    monkeypatch.setenv("FAKE_FMAX", "default/1=9.0")  # complete, and worse: still the new one counts
    _d, r = run(tmp_path, clock_hz=CLOCK)
    assert not r.timed_out and not r.transient and r.rebuild_note == "", r
    assert r.status == "timing-fail" and r.fmax_hz == 10_000_000, r  # below the earlier 12 MHz
    assert json.loads((out_dir(d) / "build.json").read_text())["attempts"] == r.attempts
    assert not (out_dir(d) / build.HELD).exists()


def test_a_rebuild_of_other_inputs_replaces_the_earlier_record_even_when_worse(
    toolchain, rec, tmp_path, monkeypatch
):
    d, first, _before = cut_short_with_a_bitstream(toolchain, tmp_path, monkeypatch)
    (tmp_path / "repo" / "src" / "a.v").write_text("module tt_um_x;\n// changed\nendmodule\n")
    fake_tool(toolchain, "nextpnr-ice40", NEXTPNR_HANG_ALWAYS)
    _d, r = run(tmp_path, clock_hz=CLOCK)
    assert r.inputs_sha256 != first.inputs_sha256
    assert r.status == "pnr-fail" and r.bitstream is None and r.rebuild_note == "", r
    assert not (out_dir(d) / "design.bin").exists()
    assert not (out_dir(d) / build.HELD).exists()


def test_an_interrupted_rebuild_puts_the_earlier_record_back(toolchain, rec, tmp_path, monkeypatch):
    d, _first, before = cut_short_with_a_bitstream(toolchain, tmp_path, monkeypatch)

    def interrupted(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(build, "_place_and_route", interrupted)
    try:
        run(tmp_path, clock_hz=CLOCK)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("the interrupt was swallowed")
    assert files(d) == before
    assert not (out_dir(d) / build.HELD).exists()
