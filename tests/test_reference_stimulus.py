# SPDX-License-Identifier: Apache-2.0
"""Fix round 1 of plan 2 task 5: the stimulus a board really gives a design (requested, applied, unmet), the two
passes, two drivers on one net, a net that does not settle, the low-phase pad. Real Verilator, no board."""

import gzip
import json
import os
from pathlib import Path

import pytest
from test_census import _held, _parse, _raw
from test_reference_profile import (
    CLOCKS,
    COPY_UI,
    COPY_UIO,
    POSEDGE,
    board,
    dump,
    project,
    record,
    ref_dir,
    run,
    work,  # noqa: F401 - the autouse fixture that points WORK at a temporary directory
)

from tthfv import cli, manifest, paths, reference, tools
from tthfv.board import census

try:
    tools.tool("verilator")
    _MISSING = ""
except Exception as e:  # noqa: BLE001 - any failure to locate the tool means skip
    _MISSING = f"verilator not available: {e}"

pytestmark = [pytest.mark.needs_tools, pytest.mark.skipif(bool(_MISSING), reason=_MISSING)]

REAL_WORK = Path(os.environ.get("TTHFV_WORK") or Path(__file__).parent.parent / "work")

# uio_oe[1] follows ui_in[1]; uio_out[1] = 1 so the net is consistent when it is enabled
OE_FOLLOWS_UI1 = "assign uo_out = ui_in; assign uio_out = 8'h02; assign uio_oe = {6'b0, ui_in[1], 1'b0};"
# the design enables uio[1] from the start: a second driver if the MCU drives ui_in[1]
ALWAYS_DRIVES1 = "assign uo_out = ui_in; assign uio_out = 8'h00; assign uio_oe = 8'h02;"
# uio_out[1] is the opposite of what the net reads: it never settles (tied net under fpga-4)
UNSETTLED_TIED = "assign uo_out = ui_in; assign uio_out = {6'b0, ~ui_in[1], 1'b0}; assign uio_oe = 8'h02;"
UNSETTLED_PAD = "assign uo_out = uio_in; assign uio_out = {6'b0, ~uio_in[1], 1'b0}; assign uio_oe = 8'h02;"
# uio_oe[1] switches on at the falling edge before clock 10
NEGEDGE_OE = """
  reg oe;
  always @(negedge clk) oe <= rst_n;
  assign uo_out = ui_in; assign uio_out = 8'h02; assign uio_oe = {6'b0, oe, 1'b0};
"""
# low and high phase differ only after clock ~1000
LATE_NEGEDGE = """
  reg [15:0] n;
  reg x;
  always @(posedge clk) n <= rst_n ? n + 16'd1 : 16'd0;
  always @(negedge clk) x <= (n > 16'd1000) ? ~x : x;
  assign uo_out = {7'b0, x}; assign uio_out = 8'h00; assign uio_oe = 8'h00;
"""


def profile_with_ui_in7_held_low() -> dict:
    raw = _raw()
    _held(raw, "ui_in[7]", 0)
    return census.build_profile(_parse(raw), usb_serial="s", date="2026-11-24")


# ---- Important 1: the manifest's ui_in is what is requested; the board gives what it can


def test_a_board_applies_the_manifests_ui_in_on_the_bits_the_mcu_may_drive(tmp_path):
    d = project(tmp_path, COPY_UI, ui_in=0x80)
    run(d, board(tmp_path))
    j = record(d, "fpga-4")
    assert dump(d, "fpga-4") == bytes([0x80]) * CLOCKS
    assert (j["ui_in_requested"], j["ui_in_mask_used"], j["ui_in_applied"], j["ui_in_unmet"]) == (
        0x80, 0xFF, 0x80, 0,
    )  # fmt: skip
    assert j["key"]["ui_in"] == 0x80  # the requested value is in the key


def test_a_requested_bit_the_mcu_may_not_drive_is_unmet_not_hidden(tmp_path):
    d = project(tmp_path, COPY_UI, ui_in=0x80)
    profile = board(tmp_path, "held7", profile_with_ui_in7_held_low())
    assert profile.data["stimulus"]["ui_in_mask"] == 0x7F
    run(d, profile)
    j = record(d, "held7")
    assert dump(d, "held7") == bytes(CLOCKS)  # the board holds ui_in[7] low
    assert (j["ui_in_requested"], j["ui_in_applied"], j["ui_in_unmet"]) == (0x80, 0, 0x80)


def test_a_manifest_uio_in_cannot_be_honoured_on_a_board_and_says_so(tmp_path):
    d = project(tmp_path, COPY_UIO, uio_in=0x04)
    run(d, board(tmp_path))
    j = record(d, "fpga-4")
    assert dump(d, "fpga-4") == bytes([0xC0]) * CLOCKS  # the profile's levels, not the request
    assert (j["uio_in_requested"], j["uio_in_applied"], j["uio_in_unmet"]) == (4, 0xC0, 4)
    assert "uio_in" in j["ui_in_mask_note"]


def test_ideal_honours_a_manifest_uio_in(tmp_path):
    d = project(tmp_path, COPY_UIO, uio_in=0x04)
    run(d)
    j = record(d)
    assert dump(d) == bytes([4]) * CLOCKS
    assert (j["uio_in_requested"], j["uio_in_applied"], j["uio_in_unmet"]) == (4, 4, 0)
    assert (j["ui_in_requested"], j["ui_in_applied"], j["ui_in_unmet"]) == (0, 0, 0)


# ---- Important 3: two passes, for real


def test_a_requested_tied_bit_needs_pass_two_and_gets_it(tmp_path):
    d = project(tmp_path, COPY_UI, ui_in=0x02)
    run(d, board(tmp_path))
    j = record(d, "fpga-4")
    assert dump(d, "fpga-4") == bytes([0x02]) * CLOCKS  # the MCU drives ui_in[1]
    assert (j["reference_pass"], j["ui_in_mask_used"], j["ui_in_applied"], j["ui_in_unmet"]) == (
        2,
        0xFF,
        2,
        0,
    )
    assert "pass 2" in j["ui_in_mask_note"]
    assert j["two_driver_clocks"] == 0


def test_pass_two_is_not_made_when_the_requested_level_is_the_presented_one(tmp_path):
    d = project(tmp_path, COPY_UI, ui_in=0x01)  # bit 0 is not tied
    run(d, board(tmp_path))
    j = record(d, "fpga-4")
    assert j["reference_pass"] == 1 and "pass 1" in j["ui_in_mask_note"]
    assert "not needed" in j["ui_in_mask_note"]


def test_a_design_whose_uio_oe_depends_on_the_driven_input_falls_back_to_pass_one(tmp_path):
    d = project(tmp_path, OE_FOLLOWS_UI1, ui_in=0x02)
    run(d, board(tmp_path))
    j = record(d, "fpga-4")
    assert j["reference_pass"] == 1 and j["two_driver_clocks"] == 0
    assert "pass 2" in j["ui_in_mask_note"] and "fell back" in j["ui_in_mask_note"]
    assert dump(d, "fpga-4") == bytes(CLOCKS)  # pass 1: ui_in[1] undriven, so the design never enables
    assert (j["ui_in_mask_used"], j["ui_in_applied"], j["ui_in_unmet"]) == (0xF1, 0, 0x02)


# ---- Important 4: two drivers on one net


def test_two_drivers_on_a_tied_net_are_counted_and_the_design_wins(tmp_path):
    d = project(tmp_path, ALWAYS_DRIVES1, ui_in=0x02)
    sim = reference.simulate(d, _fetched(tmp_path, d), clocks=200, profile=board(tmp_path), ui_drive=0xFF)
    assert sim.status == "ok", sim.log.read_text()
    assert sim.two_driver_clocks == 200 - 0 and sim.uio_oe_ever == 0x02
    assert sim.dump.read_bytes() == bytes([0x00]) * 200  # uio_out[1] = 0 wins over the MCU's 1


def _fetched(tmp_path, d):
    from tthfv import fetch

    src = tmp_path / f"proj-{d.macro}"
    return fetch.resolve(d, src, src, tmp_path / "fetch.log")


# ---- Important 2: a net that does not settle is never a good reference


@pytest.mark.parametrize(
    ("text", "profile"), [(UNSETTLED_TIED, "fpga-4"), (UNSETTLED_PAD, "ideal")], ids=["tied", "pad"]
)
def test_a_net_that_never_settles_is_a_failure_naming_the_clock(tmp_path, text, profile):
    d = project(tmp_path, text)
    prof = board(tmp_path) if profile == "fpga-4" else None
    r = reference.make_reference(d, clocks=CLOCKS, profile=prof)
    assert r.status == "sim-unsettled"
    assert "did not settle" in r.reason and "clock" in r.reason
    out = ref_dir(d, profile)
    assert not (out / "reference.json").exists()
    assert json.loads((out / "reference-fail.json").read_text())["status"] == "sim-unsettled"


# ---- the low phase settles the pad too


def test_a_pad_driven_at_the_falling_edge_is_settled_in_the_low_phase(tmp_path):
    d = project(tmp_path, NEGEDGE_OE)
    run(d, board(tmp_path))
    j = record(d, "fpga-4")
    low = gzip.decompress((ref_dir(d, "fpga-4") / j["low_dump"]).read_bytes())
    assert (
        low[9] == 0x02
    )  # the falling edge before clock 10 enabled uio[1]; the tied ui_in[1] read it in that low phase
    assert dump(d, "fpga-4")[9] == 0x00


def test_a_phase_difference_that_only_appears_mid_run_is_seen(tmp_path):
    d = project(tmp_path, LATE_NEGEDGE)
    run(d)
    j = record(d)
    low = gzip.decompress((ref_dir(d) / j["low_dump"]).read_bytes())
    high = dump(d)
    assert j["phase_sensitive"] is True
    assert low[:900] == high[:900] and low[1200:1300] != high[1200:1300]


def test_a_design_with_no_phase_difference_has_the_low_dump_equal_to_the_high(tmp_path):
    d = project(tmp_path, POSEDGE)
    run(d)
    assert record(d)["phase_sensitive"] is False


# ---- sensitivity details


def test_the_sensitivity_note_says_what_was_flipped_and_what_was_not(tmp_path):
    d = project(tmp_path, COPY_UI)
    run(d, board(tmp_path), sensitivity=True)
    note = record(d, "fpga-4")["sensitivity_note"]
    assert "uio_in bits 0xc0 flipped" in note
    assert "not flipped" in note and "floating_assumed_low" in note and "ui_in" in note


def test_a_directory_made_in_scratch_before_a_sensitivity_run_does_not_crash_it(tmp_path, monkeypatch):
    d = project(tmp_path, POSEDGE)
    real = reference._verilate

    def planting(design, fetched, key, scratch, *a, **k):
        failed = real(design, fetched, key, scratch, *a, **k)
        (scratch / "seed1").mkdir()
        (scratch / "seed1" / "junk").write_text("x")
        return failed

    monkeypatch.setattr(reference, "_verilate", planting)
    run(d, board(tmp_path), sensitivity=True)
    assert record(d, "fpga-4")["reset_determined"] is True


def test_the_scratch_is_checked_after_each_sensitivity_run(tmp_path, monkeypatch):
    d = project(tmp_path, POSEDGE)
    calls = []
    real = reference._scratch_too_big

    def too_big_later(scratch):
        calls.append(1)
        return "the design filled its scratch directory: boom" if len(calls) > 3 else real(scratch)

    run(d, board(tmp_path))
    monkeypatch.setattr(reference, "_scratch_too_big", too_big_later)
    reference.make_reference(d, clocks=CLOCKS, profile=board(tmp_path), sensitivity=True)
    assert "boom" in record(d, "fpga-4")["sensitivity_note"]  # recorded, the reference itself stays


# ---- an interrupted stage leaves no half-made profile directory


def test_an_interrupted_simulation_leaves_no_profile_directory(tmp_path, monkeypatch):
    d = project(tmp_path, COPY_UI)

    def interrupted(design, fetched, clocks, force=False, profile=reference.IDEAL, **kw):
        out = reference.reference_dir(design.id, profile.name)
        out.mkdir(parents=True)
        (out / "sim.log").write_text("partial")
        raise KeyboardInterrupt

    monkeypatch.setattr(reference, "simulate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        reference.make_reference(d, clocks=CLOCKS)
    assert not ref_dir(d).exists()


# ---- compare's help names the layout


def test_compares_help_names_the_profile_directory(capsys):
    with pytest.raises(SystemExit):
        cli.main(["compare", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "<profile>" in out


# ---- ideal reproduces the first batch's calibration hash with the real toolchain


def test_ideal_reproduces_the_first_batch_hash_of_the_calibration_bars(tmp_path):
    videos = REAL_WORK / "videos-repo"
    if not videos.is_dir():
        pytest.skip(f"no synced videos repo at {videos} (run `tthfv manifest sync`)")
    designs = {d.id: d for d in manifest.load(videos_repo=videos, extras=manifest.EXTRAS_DEFAULT)}
    d = designs["calibration/tt_um_vgacal_bars"]
    r = reference.make_reference(d)
    if r.status != "ok" and not (paths.design_dir("reference", d.id) / "ideal" / "reference.json").exists():
        pytest.skip(f"the calibration design could not be fetched here: {r.reason}")
    assert record(d)["frames"][0]["hash"] == "abfdb0998fff1d25"
