# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: F811 - the fixtures toolchain and rec are imported from test_build
"""The build's coverage rules: PDK cell models for a design that fails without them, the SystemVerilog-mode
retry, and the reuse of failed builds made by an older ladder. The toolchain is the fake one of test_build."""

import json

import pytest
from test_build import make_design, make_fetched, rec, toolchain  # noqa: F401 - fixtures

from tthfv import build, paths, pdkcells

INV = "sky130_fd_sc_hd__inv_1"
LATCH = "sky130_fd_sc_hd__dlxtp_1"


def missing(cell: str) -> str:
    return (
        f"ERROR: Module `\\{cell}' referenced in module `\\tt_um_x' in cell `\\i' is not part of the design."
    )


def design_with(tmp_path, *cells: str):
    src = tmp_path / "repo" / "src"
    src.mkdir(parents=True, exist_ok=True)
    body = "".join(f"  {c} c{i} ();\n" for i, c in enumerate(cells))
    (src / "a.v").write_text(
        f"module tt_um_x;\n  // sky130_fd_sc_hd__buf_1 only in a comment\n{body}endmodule\n"
    )
    d = make_design(source_files=("a.v",))
    return d, make_fetched(tmp_path, ("a.v",), d)


def out_dir(d):
    return paths.design_dir("build", d.id).resolve()


def count(rec_dir) -> int:
    p = rec_dir / "yosys.count"
    return len(p.read_text().split()) if p.exists() else 0


def base_digest(d, f):
    wrapper = (build.HARNESS / "tt_fpga_top.v").read_text().replace(build.PLACEHOLDER, f.top_module_used)
    return build._inputs_digest(d, f, wrapper, build._tool_versions(), 8, build.sandbox.limits_record())


def test_a_design_failing_on_a_missing_pdk_cell_is_rebuilt_with_its_models(
    toolchain, rec, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_YOSYS_PDK", missing(INV))
    d, f = design_with(tmp_path, INV)
    r = build.build(d, f, max_attempts=1)
    assert r.status == "ok", r.reason
    assert r.pdk_cells == [INV] and r.pdk_latches is False and r.pdk_note == ""
    assert count(rec) == 2  # without the models, then with them
    out = out_dir(d)
    plan = pdkcells.plan_for(f.synth_sources)
    assert (out / "pdk_cells.v").read_text() == plan.text
    ys = (out / "yosys-default.ys").read_text()
    assert f'"{out / "pdk_cells.v"}"' in ys and "tthfv_pdk_loops" in ys
    assert "-latches" not in ys
    # the models are part of this design's key, and only of this design's
    assert r.inputs_sha256 != base_digest(d, f)
    assert r.inputs_sha256 == build._inputs_digest(
        d,
        f,
        (out / "tt_fpga_top.v").read_text(),
        build._tool_versions(),
        1,
        build.sandbox.limits_record(),
        plan,
    )
    on_disk = json.loads((out / "build.json").read_text())
    assert (
        on_disk["pdk_cells"] == [INV]
        and on_disk["pdk_latches"] is False
        and on_disk["ladder"] == build.LADDER
    )


def test_a_build_with_pdk_models_is_reused(toolchain, rec, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_YOSYS_PDK", missing(INV))
    d, f = design_with(tmp_path, INV)
    first = build.build(d, f, max_attempts=1)
    n = count(rec)
    again = build.build(d, f, max_attempts=1)
    assert count(rec) == n and again == first


def test_latch_cells_build_with_latches_allowed_and_are_recorded(toolchain, rec, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_YOSYS_PDK", missing(LATCH))
    d, f = design_with(tmp_path, LATCH, INV)
    r = build.build(d, f, max_attempts=1)
    assert r.status == "ok", r.reason
    assert r.pdk_cells == [LATCH, INV]  # sorted
    assert r.pdk_latches is True
    assert "LUT loops" in r.pdk_note and LATCH in r.pdk_note
    ys = (out_dir(d) / "yosys-default.ys").read_text()
    assert "synth_ice40 -top tt_fpga_top -latches warn -json" in ys


def test_a_missing_cell_without_a_model_stays_failed_naming_it(toolchain, rec, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_YOSYS_PDK", missing("sky130_fd_sc_hd__o21a_1"))
    d, f = design_with(tmp_path, "sky130_fd_sc_hd__o21a_1")
    r = build.build(d, f, max_attempts=1)
    assert r.status == "synth-fail"
    assert r.reason.startswith("needs PDK cell model sky130_fd_sc_hd__o21a_1: ")
    assert count(rec) == 1 and r.pdk_cells == []


def test_a_loop_through_pdk_cells_is_a_delay_used_as_function(toolchain, rec, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_YOSYS_PDK", missing(INV))
    monkeypatch.setenv("FAKE_YOSYS_LOOP", "1")
    d, f = design_with(tmp_path, INV)
    r = build.build(d, f, max_attempts=1)
    assert r.status == "synth-fail"
    assert r.reason.startswith("uses PDK delay as function: ")
    assert r.pdk_cells == [INV]


def test_a_design_without_pdk_cells_reads_no_models(toolchain, rec, tmp_path):
    d, f = design_with(tmp_path)
    r = build.build(d, f, max_attempts=1)
    assert r.status == "ok" and r.pdk_cells == []
    assert "pdk_cells" not in (out_dir(d) / "yosys-default.ys").read_text()
    assert not (out_dir(d) / "pdk_cells.v").exists()
    assert r.inputs_sha256 == build._inputs_digest(
        d,
        f,
        (out_dir(d) / "tt_fpga_top.v").read_text(),
        build._tool_versions(),
        1,
        build.sandbox.limits_record(),
    )


def test_a_synth_fail_from_an_older_ladder_is_redone_but_a_current_one_is_reused(
    toolchain, rec, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_YOSYS_FAIL", "1")
    d, f = design_with(tmp_path)
    r = build.build(d, f, max_attempts=1)
    assert r.status == "synth-fail"
    n = count(rec)
    assert build.build(d, f, max_attempts=1).status == "synth-fail" and count(rec) == n  # reused
    path = out_dir(d) / "build.json"
    old = json.loads(path.read_text())
    del old["ladder"]  # what the code before the coverage work wrote
    path.write_text(json.dumps(old))
    build.build(d, f, max_attempts=1)
    assert count(rec) > n  # redone


@pytest.mark.parametrize(
    "message",
    [
        "Static cast is only supported in SystemVerilog mode.",
        "Parameter defaults can only be omitted in SystemVerilog mode!",
        "Wildcard port connections are only supported in SystemVerilog mode.",
        "In pure Verilog (not SystemVerilog), parameter/localparam with an initializer must use the parameter/localparam keyword",
    ],
)
def test_a_systemverilog_mode_error_is_retried_with_the_sv_reader(
    toolchain, rec, tmp_path, monkeypatch, message
):
    monkeypatch.setenv("FAKE_YOSYS_SVMODE", message)
    d, f = design_with(tmp_path)
    r = build.build(d, f, max_attempts=1)
    assert r.status == "ok", r.reason
    assert r.sv_reader is True and r.sv_reader_rule == f"a.v:3: ERROR: {message}"
    assert count(rec) == 2


def test_another_error_is_not_retried_with_the_sv_reader(toolchain, rec, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_YOSYS_MISSING", "1")
    d, f = design_with(tmp_path)
    r = build.build(d, f, max_attempts=1)
    assert r.status == "synth-fail" and count(rec) == 1 and r.sv_reader_rule == ""


def test_latch_cells_place_and_route_ignoring_their_loops(toolchain, rec, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_YOSYS_PDK", missing(LATCH))
    d, f = design_with(tmp_path, LATCH)
    r = build.build(d, f, max_attempts=1)
    assert r.status == "ok", r.reason
    assert "--ignore-loops" in (rec / "nextpnr.args").read_text().split()


def test_a_design_without_latch_cells_does_not_ignore_loops(toolchain, rec, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_YOSYS_PDK", missing(INV))
    d, f = design_with(tmp_path, INV)
    build.build(d, f, max_attempts=1)
    assert "--ignore-loops" not in (rec / "nextpnr.args").read_text().split()


def test_an_empty_module_left_for_place_and_route_is_a_hard_macro(toolchain, rec, tmp_path, monkeypatch):
    log = tmp_path / "pnr.log"
    log.write_text(
        "ERROR: cell type 'incrementer' is unsupported (instantiated as 'user_project.timing.counter')\n"
    )
    monkeypatch.setenv("FAKE_PNR_LOG", str(log))
    monkeypatch.setenv("FAKE_PNR_EXIT", "125")
    d, f = design_with(tmp_path)
    (f.synth_sources[0]).write_text(
        f.synth_sources[0].read_text() + "module incrementer(output [3:0] q);\nendmodule\n"
    )
    f = make_fetched(tmp_path, ("a.v",), d)
    r = build.build(d, f, max_attempts=1)
    assert r.status == "pnr-fail"
    assert r.reason.startswith("needs analog/hard macro incrementer: "), r.reason


def test_an_unsupported_cell_the_design_does_not_declare_keeps_its_reason(
    toolchain, rec, tmp_path, monkeypatch
):
    log = tmp_path / "pnr.log"
    log.write_text("ERROR: cell type 'SB_FOO' is unsupported (instantiated as 'x')\n")
    monkeypatch.setenv("FAKE_PNR_LOG", str(log))
    monkeypatch.setenv("FAKE_PNR_EXIT", "125")
    d, f = design_with(tmp_path)
    r = build.build(d, f, max_attempts=1)
    assert r.status == "pnr-fail" and r.reason.startswith("nextpnr exit status 125")
