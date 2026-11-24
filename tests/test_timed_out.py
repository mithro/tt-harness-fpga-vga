# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: F811 - pytest fixtures imported from other test modules
"""A result caused by a tool timeout or the design budget is marked timed_out and never reused."""

import json
from pathlib import Path

import pytest
from gitrepo import commit_files, init
from test_build import NEXTPNR, YOSYS, fake_tool, make_design, make_fetched, rec, run, toolchain  # noqa: F401
from test_fetch import TOP, design, work  # noqa: F401

from tthfv import build, paths, reference, results, tools

# YOSYS and NEXTPNR from test_build, hanging for a chosen invocation.
HANGING_YOSYS = YOSYS.replace(
    'echo run >> "$FAKE_REC/yosys.count"', 'echo run >> "$FAKE_REC/yosys.count"\nsleep 60'
)
NEXTPNR_HANG_ON_3 = NEXTPNR.replace(
    'echo "$@" >> "$FAKE_REC/nextpnr.args"',
    'echo "$@" >> "$FAKE_REC/nextpnr.args"\ncase "$*" in *"--seed 3"*) sleep 60;; esac',
)
FMAX = "default/1=10.0,default/2=12.0,default/3=10.0,default/4=10.0"


def record(d) -> dict:
    return json.loads((paths.design_dir("build", d.id) / "build.json").read_text())


def count(rec, name) -> int:
    path = rec / name
    return len(path.read_text().split()) if path.exists() else 0


def test_yosys_timeout_is_recorded_and_the_build_is_redone(toolchain, rec, tmp_path, monkeypatch):
    fake_tool(toolchain, "yosys", HANGING_YOSYS)
    monkeypatch.setattr(build, "YOSYS_TIMEOUT", 1)
    d, r = run(tmp_path)
    assert r.status == "synth-fail" and "yosys timed out" in r.reason
    assert r.timed_out is True and record(d)["timed_out"] is True
    before = count(rec, "yosys.count")
    run(tmp_path)  # no force
    assert count(rec, "yosys.count") > before


def test_nextpnr_timeout_late_in_the_sweep_keeps_the_best_attempt_and_is_not_reused(
    toolchain, rec, tmp_path, monkeypatch
):
    fake_tool(toolchain, "nextpnr-ice40", NEXTPNR_HANG_ON_3)
    monkeypatch.setattr(build, "NEXTPNR_TIMEOUT", 1)
    monkeypatch.setenv("FAKE_FMAX", FMAX)
    d, r = run(tmp_path, clock_hz=20_000_000)
    assert r.status == "timing-fail" and r.bitstream
    assert r.recipe == {"synth": "default", "seed": 2} and r.fmax_hz == 12_000_000
    assert r.timed_out is True and record(d)["timed_out"] is True
    assert "nextpnr timed out" in r.reason
    before = count(rec, "pnr.order")
    run(tmp_path, clock_hz=20_000_000)
    assert count(rec, "pnr.order") > before


def test_an_ordinary_timing_fail_is_reused(toolchain, rec, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FMAX", FMAX)
    d, r = run(tmp_path, clock_hz=20_000_000)
    assert r.status == "timing-fail" and r.timed_out is False and record(d)["timed_out"] is False
    before = count(rec, "pnr.order")
    again = run(tmp_path, clock_hz=20_000_000)[1]
    assert again.status == "timing-fail" and count(rec, "pnr.order") == before


def test_the_design_budget_expiring_is_timed_out_and_not_reused(toolchain, rec, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PNR_SLEEP", "1.0")
    d = make_design(clock_hz=25_175_000)
    f = make_fetched(tmp_path, design=d)
    r = build.build(d, f, design_timeout=2.5)
    assert "design timeout" in r.reason and r.bitstream
    assert r.timed_out is True
    before = count(rec, "pnr.order")
    build.build(d, f, design_timeout=2.5)
    assert count(rec, "pnr.order") > before


def test_no_budget_at_all_is_timed_out(toolchain, rec, tmp_path):
    d = make_design()
    f = make_fetched(tmp_path, design=d)
    assert build.build(d, f, design_timeout=0).timed_out is True


def test_an_icepack_timeout_is_timed_out(toolchain, rec, tmp_path, monkeypatch):
    fake_tool(toolchain, "icepack", "#!/bin/sh\nsleep 60\n")
    monkeypatch.setattr(build, "ICEPACK_TIMEOUT", 1)
    r = run(tmp_path)[1]
    assert r.status == "pack-fail" and r.timed_out is True


def test_a_build_json_without_the_field_counts_as_not_timed_out(toolchain, rec, tmp_path):
    d, r = run(tmp_path)
    path = paths.design_dir("build", d.id) / "build.json"
    data = json.loads(path.read_text())
    del data["timed_out"]
    path.write_text(json.dumps(data))
    before = count(rec, "pnr.order")
    again = run(tmp_path)[1]
    assert again.status == r.status and count(rec, "pnr.order") == before


# ---- reference


@pytest.fixture
def sim(tmp_path, monkeypatch, work):
    """make_reference on a real checkout with a fake Verilator: calls[i] is what call i returns."""
    monkeypatch.setattr(tools, "tool", lambda name: "/bin/true")
    monkeypatch.setattr(reference, "_verilator_version", lambda: "Verilator fake")
    repo = init(tmp_path / "proj")
    c = commit_files(repo, {"src/a.v": TOP})
    calls: list[list[str]] = []
    plan: list[int] = []

    def fake_run(args, log, cwd, timeout, box=None, track=None):
        calls.append(args)
        rc = plan[len(calls) - 1]
        if "-Mdir" in args and rc == 0:  # a verilation that worked leaves the executable
            obj = args[args.index("-Mdir") + 1]
            (Path(obj) / "Vtop").write_text("")
        return rc, ""

    monkeypatch.setattr(reference, "_run", fake_run)
    return design(repo, c), calls, plan


def fail_record(d) -> dict:
    return json.loads((paths.design_dir("reference", d.id) / "reference-fail.json").read_text())


def test_a_verilator_timeout_is_recorded_and_rerun(sim):
    d, calls, plan = sim
    plan += [reference.TIMED_OUT, reference.TIMED_OUT]
    r = reference.make_reference(d, clocks=40)
    assert r.status == "verilate-fail" and r.timed_out is True
    assert fail_record(d)["timed_out"] is True
    reference.make_reference(d, clocks=40)
    assert len(calls) == 2  # not reused


def test_a_simulation_timeout_is_recorded_and_rerun(sim):
    d, calls, plan = sim
    plan += [0, reference.TIMED_OUT, 0, reference.TIMED_OUT]
    r = reference.make_reference(d, clocks=40)
    assert r.status == "sim-fail" and "timed out" in r.reason and r.timed_out is True
    assert fail_record(d)["timed_out"] is True
    reference.make_reference(d, clocks=40)
    assert len(calls) == 4  # verilated and simulated again


def test_an_ordinary_failure_is_not_timed_out(sim):
    d, _calls, plan = sim
    plan += [1]
    assert reference.make_reference(d, clocks=40).timed_out is False
    assert fail_record(d)["timed_out"] is False


# ---- results


def test_results_shows_timed_out_in_the_table_and_the_index(work, tmp_path, monkeypatch):
    from test_results import design as rdesign
    from test_results import put

    put(
        work,
        "build",
        "tt01/tt_um_t",
        "build.json",
        {"status": "pnr-fail", "reason": "x", "timed_out": True, "commit": "c" * 40},
    )
    put(
        work, "build", "tt01/tt_um_o", "build.json", {"status": "pnr-fail", "reason": "x", "commit": "c" * 40}
    )
    put(work, "reference", "tt01/tt_um_t", "reference-fail.json",
        {"status": "sim-fail", "reason": "y", "timed_out": True})  # fmt: skip
    recs = results.collect([rdesign("tt01/tt_um_t"), rdesign("tt01/tt_um_o")], work)
    by_id = {r["id"]: r for r in recs}
    assert by_id["tt01/tt_um_t"]["build"]["timed_out"] is True
    assert by_id["tt01/tt_um_t"]["reference"]["timed_out"] is True
    assert by_id["tt01/tt_um_o"]["build"]["timed_out"] is False
    md = results.render_markdown(recs)
    row_t = next(r for r in md.splitlines() if r.startswith("| tt01/tt_um_t"))
    row_o = next(r for r in md.splitlines() if r.startswith("| tt01/tt_um_o"))
    assert "pnr-fail (timed out)" in row_t and "sim-fail (timed out)" in row_t
    assert "timed out" not in row_o
