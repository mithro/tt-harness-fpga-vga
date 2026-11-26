# SPDX-License-Identifier: Apache-2.0
"""The reference's PDK cell rule (real Verilator): a design that fails to verilate for want of a PDK cell is
verilated again with the models the build uses, the models join its key, and the reference is reused."""
# ruff: noqa: F811 - the fixtures are imported from test_reference_stale

import pytest
from gitrepo import commit_files
from test_reference_stale import _MISSING, COUNTER, design_at, head, project, record, sims, work  # noqa: F401

from tthfv import fetch, frames, pdkcells, reference

pytestmark = [pytest.mark.needs_tools, pytest.mark.skipif(bool(_MISSING), reason=_MISSING)]

# The counter with its output through PDK inverters twice (the picture is the counter's) and a latch.
WITH_CELLS = COUNTER.replace(
    "  assign uo_out  = count;\n",
    "  wire [7:0] inv;\n"
    "  sky130_fd_sc_hd__inv_1 i0 [7:0] (.A(count), .Y(inv));\n"
    "  sky130_fd_sc_hd__inv_1 i1 [7:0] (.A(inv), .Y(uo_out));\n",
)


def sim_log(d) -> str:
    return (reference.reference_dir(d.id, "ideal") / "sim.log").read_text()


def test_a_design_needing_pdk_cells_is_verilated_with_the_models(project, sims):
    c = commit_files(project, {"src/tt_um_counter_vga.v": WITH_CELLS})
    d = design_at(project, c)
    r = reference.make_reference(d, clocks=40)
    assert r.status == "no-sync", r.reason
    rec = record(d)
    plan = pdkcells.plan_named(["sky130_fd_sc_hd__inv_1"])
    assert rec["key"]["pdk_cells"] == plan.key_fields()
    assert rec["key_sha256"] == reference.key_sha256(rec["key"])
    assert rec["pdk_cells"] == ["sky130_fd_sc_hd__inv_1"]  # a plain field beside the key
    assert "pdk_loop_check_note" not in rec  # the check ran
    log = sim_log(d)
    assert "MODMISSING" in log and pdkcells.FILE_NAME in log
    # the picture is the counter's: the inverters cancel
    dump = frames.load_dump(reference.reference_dir(d.id, "ideal") / rec["dump"], host_owned=True)
    assert [int(b) for b in dump[10:20]] == list(range(1, 11))
    # reused, not simulated again
    reference.make_reference(d, clocks=40)
    assert len(sims) == 1


def test_a_cell_without_a_model_stays_failed_naming_it(project, sims):
    c = commit_files(
        project,
        {
            "src/tt_um_counter_vga.v": WITH_CELLS.replace(
                "sky130_fd_sc_hd__inv_1 i1", "sky130_fd_sc_hd__o21a_1 i1"
            )
        },
    )
    d = design_at(project, c)
    r = reference.make_reference(d, clocks=40)
    assert r.status == "verilate-fail"
    assert r.reason.startswith("needs PDK cell model sky130_fd_sc_hd__o21a_1: "), r.reason


RING = COUNTER.replace(
    "  assign uo_out  = count;\n",
    "  wire [2:0] r;\n"
    "  sky130_fd_sc_hd__inv_1 i0 (.A(r[2]), .Y(r[0]));\n"
    "  sky130_fd_sc_hd__inv_1 i1 (.A(r[0]), .Y(r[1]));\n"
    "  sky130_fd_sc_hd__inv_1 i2 (.A(r[1]), .Y(r[2]));\n"
    "  assign uo_out  = count ^ {7'd0, r[0]};\n",
)


def test_a_ring_of_pdk_inverters_is_a_delay_used_as_function(project, sims):
    c = commit_files(project, {"src/tt_um_counter_vga.v": RING})
    d = design_at(project, c)
    r = reference.make_reference(d, clocks=40)
    assert r.status == "verilate-fail"
    assert r.reason.startswith("uses PDK delay as function: "), r.reason


def test_a_design_without_pdk_cells_has_no_pdk_key(project, sims):
    d = design_at(project, head(project))
    reference.make_reference(d, clocks=40)
    assert "pdk_cells" not in record(d)["key"]
    assert "pdk_cells" not in record(d) and "pdk_loop_check_note" not in record(d)
    assert pdkcells.FILE_NAME not in sim_log(d)


def test_a_loop_check_that_cannot_run_says_why(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.v").write_text("module tt_um_x;\n  sky130_fd_sc_hd__inv_1 i ();\nendmodule\n")
    f = fetch.FetchResult(
        "ok", tmp_path, (src / "a.v",), (src,), tmp_path / "fetch.log", "", (src / "a.v",), "tt_um_x"
    )
    scratch, log = tmp_path / "scratch", tmp_path / "sim.log"
    scratch.mkdir()
    log.write_text("")
    monkeypatch.setattr(
        reference, "_run", lambda *a, **k: (1, "ERROR: Module `\\foo' is not part of the design.")
    )
    track = reference._Track()
    plan = pdkcells.plan_named(["sky130_fd_sc_hd__inv_1"])
    assert reference._pdk_loops(f, {"verilator_flags": []}, scratch, src, None, track, log, plan) is False
    want = (
        "PDK loop check could not run, so no loop was found: ERROR: Module `\\foo' is not part of the design."
    )
    assert track.pdk_loop_check_note == want
    assert want in log.read_text()
    monkeypatch.setattr(reference, "_run", lambda *a, **k: (0, ""))
    track = reference._Track()
    assert reference._pdk_loops(f, {"verilator_flags": []}, scratch, src, None, track, log, plan) is False
    assert track.pdk_loop_check_note == ""
