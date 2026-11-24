# SPDX-License-Identifier: Apache-2.0
"""The reference under a board stimulus profile (plan 2, task 5): real Verilator on tiny designs, no board."""

import copy
import dataclasses
import gzip
import json
from pathlib import Path

import pytest
from gitrepo import commit_files, git, init
from test_census import _held, _parse, _raw

from tthfv import frames, paths, reference, tools
from tthfv.board import census
from tthfv.errors import UserError
from tthfv.manifest import Design

ROOT = Path(__file__).parent.parent
FPGA4 = ROOT / "boards" / "fpga-4.json"

try:
    tools.tool("verilator")
    _MISSING = ""
except Exception as e:  # noqa: BLE001 - any failure to locate the tool means skip
    _MISSING = f"verilator not available: {e}"

pytestmark = [pytest.mark.needs_tools, pytest.mark.skipif(bool(_MISSING), reason=_MISSING)]

CLOCKS = 3000


@pytest.fixture(autouse=True)
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    return tmp_path / "work"


def verilog(name: str, body: str) -> str:
    return (
        f"module {name}(input [7:0] ui_in, output [7:0] uo_out, input [7:0] uio_in,\n"
        "  output [7:0] uio_out, output [7:0] uio_oe, input ena, input clk, input rst_n);\n"
        f"{body}\nendmodule\n"
    )


COPY_UI = "assign uo_out = ui_in; assign uio_out = 8'h00; assign uio_oe = 8'h00;"
COPY_UIO = "assign uo_out = uio_in; assign uio_out = 8'h00; assign uio_oe = 8'h00;"
COPY_UIO7 = "assign uo_out = {uio_in[7], 7'b0}; assign uio_out = 8'h00; assign uio_oe = 8'h00;"
TIE_DRIVE = "assign uo_out = ui_in; assign uio_out = 8'h02; assign uio_oe = 8'h02;"
LATE_DRIVE = """
  reg [15:0] n;
  always @(posedge clk) n <= rst_n ? n + 16'd1 : 16'd0;
  assign uo_out = ui_in;
  assign uio_out = 8'h02;
  assign uio_oe = (n > 16'd1000) ? 8'h02 : 8'h00;
"""
NEGEDGE = """
  reg [7:0] r;
  always @(negedge clk) r <= rst_n ? r + 8'd1 : 8'd0;
  assign uo_out = r; assign uio_out = 8'h00; assign uio_oe = 8'h00;
"""
POSEDGE = """
  reg [7:0] r;
  always @(posedge clk) r <= rst_n ? r + 8'd1 : 8'd0;
  assign uo_out = r; assign uio_out = 8'h00; assign uio_oe = 8'h00;
"""
NO_RESET = """
  reg [7:0] r;
  always @(posedge clk) r <= r + 8'd1;
  assign uo_out = r; assign uio_out = 8'h00; assign uio_oe = 8'h00;
"""


def project(tmp_path: Path, text: str, name: str = "tt_um_prof", **kw) -> Design:
    """A design in a local git repository, so the whole make_reference pipeline (fetch included) runs."""
    repo = init(tmp_path / f"proj-{name}")
    commit_files(repo, {f"src/{name}.v": verilog(name, text)})
    base = Design(
        id=f"tt99/{name}", shuttle="tt99", macro=name, title="", author="", repo=str(repo),
        commit=git(repo, "rev-parse", "HEAD"), top_module=name, source_files=(f"{name}.v",),
        clock_hz=25_175_000, clock_declared=True, pinout={}, pmods=(), tiles="1x1", render_verdict=None,
        override={}, patch=None, qspi=None, ui_in=0, uio_in=0, scripted=False, candidate=False,
    )  # fmt: skip
    return dataclasses.replace(base, **kw)


def board(tmp_path: Path, name: str = "fpga-4", profile: dict | None = None):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(profile if profile is not None else json.loads(FPGA4.read_text())))
    return reference.load_profile(str(path))


def profile_with_ui_in_bit0_held_high() -> dict:
    raw = _raw()
    _held(raw, "ui_in[0]", 1)
    return census.build_profile(_parse(raw), usb_serial="s", date="2026-11-24")


def run(d: Design, profile=None, **kw):
    r = reference.make_reference(d, clocks=CLOCKS, profile=profile, **kw)
    assert r.status in ("ok", "no-sync"), r.reason
    return r


def ref_dir(d: Design, name: str = "ideal") -> Path:
    return paths.design_dir("reference", d.id) / name


def record(d: Design, name: str = "ideal") -> dict:
    return json.loads((ref_dir(d, name) / "reference.json").read_text())


def dump(d: Design, name: str = "ideal") -> bytes:
    return gzip.decompress((ref_dir(d, name) / "uo_out.bin.gz").read_bytes())


# ---- layout and the ideal profile


def test_ideal_is_the_default_and_lives_under_its_name(tmp_path):
    d = project(tmp_path, COPY_UI, ui_in=7)
    run(d)
    j = record(d)
    assert (j["schema"], j["profile"]) == (3, "ideal")
    assert dump(d) == bytes([7]) * CLOCKS
    assert not (paths.design_dir("reference", d.id) / "reference.json").exists()


def test_ideal_with_nothing_attached_is_unchanged_by_the_new_fields(tmp_path):
    d = project(tmp_path, COPY_UIO, uio_in=0x5C)
    run(d)
    j = record(d)
    assert dump(d) == bytes([0x5C]) * CLOCKS
    assert (j["uio_oe_ever"], j["ui_in_mask_used"], j["phase_sensitive"]) == (0, 255, False)
    assert j["key"]["profile"] == {"name": "ideal", "data": None}


# ---- the board profile reaches the design


def test_a_board_profile_replaces_the_fixed_inputs_of_the_manifest(tmp_path):
    d = project(tmp_path, COPY_UI, ui_in=5)
    run(d, board(tmp_path))
    assert dump(d, "fpga-4") == bytes(CLOCKS)  # the board presents ui_in 0, not the manifest's 5


def test_a_design_copying_ui_in_sees_the_profile_value(tmp_path):
    d = project(tmp_path, COPY_UI)
    profile = board(tmp_path, "ui1", profile_with_ui_in_bit0_held_high())
    assert profile.data["stimulus"]["ui_in"] == 1
    run(d, profile)
    assert dump(d, "ui1") == bytes([1]) * CLOCKS


def test_undriven_uio_pins_sit_at_the_profile_levels(tmp_path):
    d = project(tmp_path, COPY_UIO)
    run(d, board(tmp_path))
    assert dump(d, "fpga-4") == bytes([0xC0]) * CLOCKS


def test_a_tied_ui_in_reads_what_the_design_drives_on_uio(tmp_path):
    d = project(tmp_path, TIE_DRIVE)
    run(d, board(tmp_path))
    assert dump(d, "fpga-4") == bytes([0x02]) * CLOCKS  # ui_in[1] reads uio_out[1]; ui_in[2], [3] stay low
    j = record(d, "fpga-4")
    assert (j["uio_oe_ever"], j["ui_in_mask_used"]) == (0x02, 0xF1)


def test_a_late_output_enable_is_seen_and_drops_the_tied_bit_from_the_mask(tmp_path):
    d = project(tmp_path, LATE_DRIVE)
    run(d, board(tmp_path))
    out = dump(d, "fpga-4")
    assert out[200] == 0 and out[CLOCKS - 1] == 2
    j = record(d, "fpga-4")
    assert (j["uio_oe_ever"], j["ui_in_mask_used"]) == (0x02, 0xF1)
    assert "uio[1]" in j["ui_in_mask_note"]


def test_a_design_that_never_enables_a_tied_uio_keeps_the_whole_mask_and_says_why(tmp_path):
    d = project(tmp_path, COPY_UI)
    run(d, board(tmp_path))
    j = record(d, "fpga-4")
    assert (j["uio_oe_ever"], j["ui_in_mask_used"]) == (0, 0xFF)
    assert "never enables" in j["ui_in_mask_note"]


def test_an_invalid_profile_is_refused(tmp_path):
    bad = json.loads(FPGA4.read_text())
    bad["stimulus"]["ui_in_mask"] = 256
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(UserError, match="profile"):
        reference.load_profile(str(path))
    with pytest.raises(UserError, match="profile"):
        reference.load_profile(str(tmp_path / "missing.json"))


def test_profile_name_is_ideal_or_the_file_stem(tmp_path):
    assert reference.load_profile("ideal").name == "ideal"
    assert board(tmp_path, "my-board").name == "my-board"


# ---- the key


def test_the_key_holds_the_whole_profile_and_a_changed_profile_is_not_reused(tmp_path, monkeypatch):
    d = project(tmp_path, COPY_UI)
    a = json.loads(FPGA4.read_text())
    run(d, board(tmp_path, "fpga-4", a))
    assert record(d, "fpga-4")["key"]["profile"] == {"name": "fpga-4", "data": a}
    sims = []
    real = reference.simulate
    monkeypatch.setattr(reference, "simulate", lambda *x, **k: (sims.append(1), real(*x, **k))[1])
    run(d, board(tmp_path, "fpga-4", a))
    assert sims == []  # same profile: reused
    changed = copy.deepcopy(a)
    changed["date"] = "2026-11-25"
    run(d, board(tmp_path, "fpga-4", changed))
    assert len(sims) == 1  # any change to the file re-simulates
    assert record(d, "fpga-4")["key"]["profile"]["data"]["date"] == "2026-11-25"


def test_profiles_do_not_share_a_directory(tmp_path):
    d = project(tmp_path, COPY_UI, ui_in=3)
    run(d)
    run(d, board(tmp_path))
    assert dump(d) == bytes([3]) * CLOCKS and dump(d, "fpga-4") == bytes(CLOCKS)


# ---- phase and reset sensitivity


def test_a_negative_edge_register_is_phase_sensitive(tmp_path):
    d = project(tmp_path, NEGEDGE)
    run(d)
    assert record(d)["phase_sensitive"] is True


def test_a_positive_edge_design_is_not_phase_sensitive(tmp_path):
    d = project(tmp_path, POSEDGE)
    run(d)
    j = record(d)
    assert j["phase_sensitive"] is False
    assert gzip.decompress((ref_dir(d) / j["low_dump"]).read_bytes()) == dump(d)


def test_the_low_phase_dump_is_the_value_after_the_falling_edge(tmp_path):
    d = project(tmp_path, NEGEDGE)
    run(d)
    low = gzip.decompress((ref_dir(d) / record(d)["low_dump"]).read_bytes())
    high = dump(d)
    assert len(low) == len(high) == CLOCKS
    assert low[100] == (high[100] + 1) % 256  # the negedge register has counted by the low phase


def test_without_sensitivity_the_flags_are_null(tmp_path):
    d = project(tmp_path, COPY_UI)
    run(d)
    j = record(d)
    assert j["reset_determined"] is None and j["input_sensitive"] is None


def test_a_counter_without_reset_is_not_reset_determined(tmp_path):
    d = project(tmp_path, NO_RESET)
    run(d, sensitivity=True)
    assert record(d)["reset_determined"] is False


def test_a_counter_with_reset_is_reset_determined(tmp_path):
    d = project(tmp_path, POSEDGE)
    run(d, sensitivity=True)
    assert record(d)["reset_determined"] is True


def test_a_design_copying_an_uncertain_input_is_input_sensitive(tmp_path):
    d = project(tmp_path, COPY_UIO7)
    run(d, board(tmp_path), sensitivity=True)
    assert record(d, "fpga-4")["input_sensitive"] == {"uio_in_uncertain": True}


def test_a_design_ignoring_the_uncertain_inputs_is_not_input_sensitive(tmp_path):
    d = project(tmp_path, COPY_UI)
    run(d, board(tmp_path), sensitivity=True)
    assert record(d, "fpga-4")["input_sensitive"] == {"uio_in_uncertain": False}


def test_ideal_has_no_uncertain_inputs(tmp_path):
    d = project(tmp_path, COPY_UIO7, uio_in=0x80)
    run(d, sensitivity=True)
    assert record(d)["input_sensitive"] == {"uio_in_uncertain": False}


def test_sensitivity_added_later_does_not_resimulate_the_reference(tmp_path, monkeypatch):
    d = project(tmp_path, POSEDGE)
    run(d)
    sims = []
    real = reference.simulate
    monkeypatch.setattr(reference, "simulate", lambda *x, **k: (sims.append(1), real(*x, **k))[1])
    run(d, sensitivity=True)
    assert sims == [] and record(d)["reset_determined"] is True


# ---- the move from the old layout


def old_layout_from(d: Design, key_of_old) -> Path:
    """Turn the finished new-layout reference of d into one in the old place (work/reference/<id>/), as plan 1
    left it: schema 2, no profile fields, and the key key_of_old(current key)."""
    new, old = ref_dir(d), paths.design_dir("reference", d.id)
    j = json.loads((new / "reference.json").read_text())
    for field in (
        "profile", "uio_oe_ever", "ui_in_mask_used", "ui_in_mask_note", "phase_sensitive", "low_dump",
        "low_dump_sha256", "reset_determined", "input_sensitive",
    ):  # fmt: skip
        j.pop(field)
    j["schema"] = 2
    j["key"] = key_of_old(j["key"])
    j["key_sha256"] = reference.key_sha256(j["key"])
    for p in new.iterdir():
        if p.name not in ("reference.json", "uo_out_low.bin.gz"):
            p.rename(old / p.name)
    (old / "reference.json").write_text(json.dumps(j))
    (new / "uo_out_low.bin.gz").unlink()
    (new / "reference.json").unlink()
    new.rmdir()
    return old


def legacy(key: dict) -> dict:
    return {k: v for k, v in key.items() if k not in ("profile", "ties")}


def test_an_old_layout_reference_with_a_matching_key_is_read_as_ideal(tmp_path, monkeypatch):
    d = project(tmp_path, POSEDGE)
    run(d)
    want = dump(d)
    old = old_layout_from(d, legacy)
    sims = []
    real = reference.simulate
    monkeypatch.setattr(reference, "simulate", lambda *x, **k: (sims.append(1), real(*x, **k))[1])
    r = run(d)
    assert sims == []  # nothing re-simulated
    assert dump(d) == want
    j = record(d)
    assert (j["schema"], j["profile"], j["migrated_from_schema"]) == (3, "ideal", 2)
    assert j["phase_sensitive"] is None  # a plan 1 reference has no low phase: unknown, never guessed
    assert j["key"]["profile"] == {"name": "ideal", "data": None}
    assert j["key_sha256"] == reference.key_sha256(j["key"])
    assert not (old / "reference.json").exists() and not (old / "uo_out.bin.gz").exists()
    assert r.frames_complete == j["frames_complete"]


def test_an_old_layout_reference_with_a_stale_key_is_resimulated_and_kept_until_replaced(
    tmp_path, monkeypatch
):
    d = project(tmp_path, POSEDGE)
    run(d)
    old = old_layout_from(d, lambda k: {**legacy(k), "testbench_sha256": "0" * 64})
    stale_json = (old / "reference.json").read_text()
    seen = []
    real = reference.simulate

    def watching(*x, **k):
        seen.append((old / "reference.json").read_text() == stale_json and (old / "uo_out.bin.gz").is_file())
        return real(*x, **k)

    monkeypatch.setattr(reference, "simulate", watching)
    run(d)
    assert seen == [True]  # the old reference was still there while the new one was being made
    assert record(d)["phase_sensitive"] is False and "migrated_from_schema" not in record(d)
    assert not (old / "reference.json").exists()  # replaced, so removed


def test_a_failed_new_simulation_leaves_the_old_reference_alone(tmp_path, monkeypatch):
    d = project(tmp_path, POSEDGE)
    run(d)
    old = old_layout_from(d, lambda k: {**legacy(k), "testbench_sha256": "0" * 64})
    before = (old / "reference.json").read_text()
    broken = project(tmp_path, "assign = ;", name="tt_um_prof")
    broken = dataclasses.replace(broken, id=d.id)
    r = reference.make_reference(broken, clocks=CLOCKS)
    assert r.status == "verilate-fail"
    assert (old / "reference.json").read_text() == before and (old / "uo_out.bin.gz").is_file()


# ---- the frame cutter is unaffected by the profile fields


def test_write_reference_records_the_profile_and_schema_3(tmp_path):
    d = project(tmp_path, COPY_UI)
    run(d)
    j = record(d)
    assert j["schema"] == 3
    for field in (
        "profile",
        "uio_oe_ever",
        "ui_in_mask_used",
        "phase_sensitive",
        "low_dump",
        "reset_determined",
    ):
        assert field in j, field
    assert frames.RESET_CLOCKS == j["reset_clocks"]
