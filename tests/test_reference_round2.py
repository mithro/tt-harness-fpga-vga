# SPDX-License-Identifier: Apache-2.0
"""Fix round 2 of plan 2 task 5: unmet inputs are never silent, an enable during reset narrows the mask, a failed
sensitivity keeps the reference, both phases count for two drivers, the compare messages. Real Verilator."""

import json

import pytest
from test_reference_profile import CLOCKS, COPY_UI, board, dump, project, record, ref_dir, run, work  # noqa: F401
from test_reference_stimulus import ALWAYS_DRIVES1, profile_with_ui_in7_held_low

from tthfv import compare, frames, results, reference, tools
from tthfv.cli import main
from tthfv.errors import UserError

try:
    tools.tool("verilator")
    _MISSING = ""
except Exception as e:  # noqa: BLE001 - any failure to locate the tool means skip
    _MISSING = f"verilator not available: {e}"

pytestmark = [pytest.mark.needs_tools, pytest.mark.skipif(bool(_MISSING), reason=_MISSING)]

# uio[1] is enabled only while rst_n is low: the enable is gone from clock 10 on
RESET_ONLY_ENABLE = "assign uo_out = ui_in; assign uio_out = 8'h02; assign uio_oe = {6'b0, ~rst_n, 1'b0};"
# uio[1] is enabled only while clk is low and ui_in[1] is 1: it is never seen after a rising edge
LOW_PHASE_ENABLE = (
    "assign uo_out = ui_in; assign uio_out = 8'h02; assign uio_oe = {6'b0, ui_in[1] & ~clk, 1'b0};"
)


def _cli(monkeypatch, d, profile_path=None, extra=()):
    monkeypatch.setattr(reference.manifest, "load", lambda **kw: [d])
    args = ["reference", d.id, "--clocks", str(CLOCKS), *extra]
    if profile_path:
        args += ["--profile", str(profile_path)]
    return main(args)


def _held7(tmp_path):
    path = tmp_path / "held7.json"
    path.write_text(json.dumps(profile_with_ui_in7_held_low()))
    return path


# ---- Important A: an unmet input is never silent


def test_an_unmet_ui_in_bit_is_in_the_note_with_the_level_the_board_holds(tmp_path):
    d = project(tmp_path, COPY_UI, ui_in=0x80)
    run(d, board(tmp_path, "held7", profile_with_ui_in7_held_low()))
    j = record(d, "held7")
    assert j["ui_in_unmet"] == 0x80
    assert "ui_in[7]" in j["ui_in_mask_note"] and "requested 1" in j["ui_in_mask_note"]
    assert "held at 0" in j["ui_in_mask_note"]


def test_the_command_warns_on_stderr_and_marks_the_summary_line(tmp_path, monkeypatch, capsys):
    d = project(tmp_path, COPY_UI, ui_in=0x80)
    rc = _cli(monkeypatch, d, _held7(tmp_path))
    out, err = capsys.readouterr()
    assert "WARNING" in err and "ui_in[7]" in err and d.id in err
    assert "unmet" in out.splitlines()[-1]
    assert rc == 1  # a design without sync is not ok; the point is the line, not the code
    assert reference.ReferenceResult("ok", None, 0).ui_in_unmet == 0


def test_a_no_sync_reference_with_unmet_inputs_says_so_in_its_reason_line(tmp_path, monkeypatch, capsys):
    d = project(tmp_path, COPY_UI, ui_in=0x80)
    _cli(monkeypatch, d, _held7(tmp_path))
    line = capsys.readouterr().out.splitlines()[-1]
    assert line.startswith(f"{d.id} no-sync (") and "ui_in[7]" in line


def test_a_clean_reference_prints_no_warning_and_no_mark(tmp_path, monkeypatch, capsys):
    d = project(tmp_path, COPY_UI, ui_in=0x80)
    board(tmp_path)  # writes fpga-4.json
    _cli(monkeypatch, d, tmp_path / "fpga-4.json")
    out, err = capsys.readouterr()
    assert "WARNING" not in err and "unmet" not in out


def test_a_uio_request_equal_to_the_board_level_is_met(tmp_path):
    d = project(
        tmp_path, "assign uo_out = uio_in; assign uio_out = 8'h00; assign uio_oe = 8'h00;", uio_in=0xC0
    )
    run(d, board(tmp_path))
    j = record(d, "fpga-4")
    assert (j["uio_in_requested"], j["uio_in_applied"], j["uio_in_unmet"]) == (0xC0, 0xC0, 0)
    assert "uio_in_not_applied" not in j
    assert j["uio_in"] == 0xC0 and j["ui_in"] == j["ui_in_applied"]  # the top level holds what was applied


def test_a_uio_request_against_the_board_level_is_unmet_and_the_top_level_is_the_applied_value(tmp_path):
    d = project(
        tmp_path, "assign uo_out = uio_in; assign uio_out = 8'h00; assign uio_oe = 8'h00;", uio_in=0x04
    )
    run(d, board(tmp_path))
    j = record(d, "fpga-4")
    assert dump(d, "fpga-4") == bytes([0xC0]) * CLOCKS
    assert (j["uio_in_requested"], j["uio_in_applied"], j["uio_in_unmet"]) == (0x04, 0xC0, 0x04)
    assert j["uio_in"] == 0xC0  # not the request: the value the design read
    assert "uio[2]" in j["ui_in_mask_note"] and "never drives uio" in j["ui_in_mask_note"]
    assert j["key"]["uio_in"] == 0x04  # the request is in the key


def test_compare_reports_the_unmet_inputs_next_to_its_verdict(tmp_path, capsys):
    d = project(tmp_path, COPY_UI, ui_in=0x80)
    run(d, board(tmp_path, "held7", profile_with_ui_in7_held_low()))
    out = ref_dir(d, "held7")
    cap = frames.load_dump(out / "uo_out.bin.gz")
    c = compare.compare(cap, out, build_json=None)
    assert c.verdict == "exact" and c.ui_in_unmet == 0x80 and c.uio_in_unmet == 0
    path = tmp_path / "capture.bin"
    path.write_bytes(cap.tobytes())
    assert main(["compare", str(path), str(out)]) == 0
    text = capsys.readouterr().out
    assert text.startswith("exact") and "unmet" in text and "ui_in[7]" in text


def test_results_marks_a_reference_whose_inputs_were_unmet():
    assert "unmet" in results._status_cell({"status": "ok", "inputs_unmet": True})
    assert "unmet" not in results._status_cell({"status": "ok", "inputs_unmet": False})


# ---- Important B: an enable during reset counts for the mask


@pytest.mark.parametrize("request_ui_in", [0x00, 0x02])
def test_an_enable_only_during_reset_still_drops_the_tied_bit_from_the_mask(tmp_path, request_ui_in):
    d = project(tmp_path, RESET_ONLY_ENABLE, ui_in=request_ui_in)
    run(d, board(tmp_path))
    j = record(d, "fpga-4")
    assert j["uio_oe_ever"] == 0  # documented: from clock 10 on
    assert j["uio_oe_any"] == 0x02  # every sample, reset and before clock 0 included
    assert j["ui_in_mask_used"] == 0xFD
    assert j["ui_in_unmet"] == request_ui_in & 0x02


# ---- Minors


def test_a_failed_sensitivity_leaves_the_reference_and_says_why(tmp_path, monkeypatch):
    d = project(tmp_path, COPY_UI)
    run(d, board(tmp_path))

    def failing(*a, **k):
        raise UserError("sensitivity: boom")

    monkeypatch.setattr(reference, "_sensitivity", failing)
    r = reference.make_reference(d, clocks=CLOCKS, profile=board(tmp_path), sensitivity=True)
    assert r.status == "no-sync"
    j = record(d, "fpga-4")
    assert j["reset_determined"] is None and j["input_sensitive"] is None
    assert "boom" in j["sensitivity_note"] and "not determined" in j["sensitivity_note"]


def test_a_failed_sensitivity_on_a_fresh_reference_keeps_it_too(tmp_path, monkeypatch):
    d = project(tmp_path, COPY_UI)

    def failing(*a, **k):
        raise UserError("sensitivity: boom")

    monkeypatch.setattr(reference, "_sensitivity", failing)
    reference.make_reference(d, clocks=CLOCKS, profile=board(tmp_path), sensitivity=True)
    assert (ref_dir(d, "fpga-4") / "reference.json").exists()
    assert "boom" in record(d, "fpga-4")["sensitivity_note"]


def test_a_two_driver_clock_seen_only_in_the_low_phase_is_counted(tmp_path):
    d = project(tmp_path, LOW_PHASE_ENABLE, ui_in=0x02)
    from test_reference_stimulus import _fetched

    sim = reference.simulate(d, _fetched(tmp_path, d), clocks=200, profile=board(tmp_path), ui_drive=0xFF)
    assert sim.status == "ok", sim.log.read_text()
    assert sim.two_driver_clocks == 200  # clocks, not samples: each clock is counted once


def test_pass_two_is_rejected_when_the_enable_is_seen_only_in_the_low_phase(tmp_path):
    d = project(tmp_path, LOW_PHASE_ENABLE, ui_in=0x02)
    run(d, board(tmp_path))
    j = record(d, "fpga-4")
    assert j["reference_pass"] == 1 and "fell back" in j["ui_in_mask_note"]
    assert j["ui_in_unmet"] == 0x02


def test_one_clock_with_two_drivers_in_both_phases_is_one_clock(tmp_path):
    d = project(tmp_path, ALWAYS_DRIVES1, ui_in=0x02)
    from test_reference_stimulus import _fetched

    sim = reference.simulate(d, _fetched(tmp_path, d), clocks=100, profile=board(tmp_path), ui_drive=0xFF)
    assert sim.two_driver_clocks == 100


# ---- compare's messages


def test_compare_on_a_schema_2_directory_says_where_the_new_reference_lives(tmp_path):
    old = tmp_path / "tt99" / "tt_um_x"
    old.mkdir(parents=True)
    (old / "reference.json").write_text(json.dumps({"schema": 2}))
    c = compare.compare(__import__("numpy").zeros(10, "uint8"), old, build_json=None)
    assert c.verdict == "refused"
    assert "<id>/<profile>" in c.reason and "--force" not in c.reason


def test_compare_on_the_profile_parent_directory_names_the_profiles_that_exist(tmp_path):
    parent = tmp_path / "tt99" / "tt_um_x"
    for name in ("ideal", "fpga-4"):
        (parent / name).mkdir(parents=True)
        (parent / name / "reference.json").write_text("{}")
    (parent / "scratch").mkdir()
    c = compare.compare(__import__("numpy").zeros(10, "uint8"), parent, build_json=None)
    assert c.verdict == "refused"
    assert "fpga-4" in c.reason and "ideal" in c.reason and "scratch" not in c.reason
