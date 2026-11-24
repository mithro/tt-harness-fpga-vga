# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: F811 - pytest fixtures imported from other test modules
"""Stage directories are host-owned, but something else (an older version of this tool, a previous sandboxed
design) may have left symlinks in them named like every file a stage writes. No stage may write or read
through one, and a stage directory that is itself a symlink is refused."""

import json
import os
from pathlib import Path

import pytest
from fake_ops import FakeOps
from gitrepo import commit_files, init
from test_build import make_design, make_fetched, rec, toolchain  # noqa: F401
from test_fetch import TOP, design, project, work  # noqa: F401

from tthfv import build, fetch, paths, reference, results, runner, sandbox, tools
from tthfv.errors import UserError
from tthfv.manifest import Design

DATA = Path(__file__).parent / "data"
ID = "tt99/tt_um_x"


def plant(directory: Path, names, victim: Path) -> dict[str, tuple[Path, bytes]]:
    """Symlinks in directory called `names`, each to its own canary file in victim; returns name -> (canary, bytes)."""
    directory.mkdir(parents=True, exist_ok=True)
    victim.mkdir(parents=True, exist_ok=True)
    out = {}
    for n in names:
        canary = victim / f"canary-{n}"
        canary.write_text(f"canary {n}: must never change\n" * 3)
        (directory / n).symlink_to(canary)
        out[n] = (canary, canary.read_bytes())
    return out


def untouched(planted, victim: Path) -> None:
    for n, (canary, data) in planted.items():
        assert canary.read_bytes() == data, f"{n}: written through a symlink"
    assert sorted(p.name for p in victim.iterdir()) == sorted(c.name for c, _ in planted.values())


def regular(directory: Path, names) -> None:
    for n in names:
        p = directory / n
        assert p.exists() and not p.is_symlink() and p.is_file(), f"{n} is not a regular file"


# ---- build (fake tools)

BUILD_NAMES = (
    "build.json", "build.json.tmp", "tt_fpga_top.v", "yosys-default.ys", "yosys-default.log",
    "nextpnr-default-1.log", "icepack.log", "design.bin", "out.asc", "out-default.json", "sv2v.v", "sv2v.log",
    "scratch",
)  # fmt: skip


def test_the_build_stage_writes_through_no_symlink_planted_in_its_directory(toolchain, rec, tmp_path):
    d = make_design()
    out = paths.design_dir("build", d.id)
    victim = tmp_path / "victim"
    planted = plant(out, BUILD_NAMES, victim)
    r = build.build(d, make_fetched(tmp_path, design=d))
    assert r.status == "ok", r.reason
    untouched(planted, victim)
    regular(
        out, ("build.json", "tt_fpga_top.v", "yosys-default.ys", "yosys-default.log", "design.bin", "out.asc")
    )
    assert json.loads((out / "build.json").read_text())["status"] == "ok"


def test_a_build_directory_that_is_a_symlink_is_refused(toolchain, rec, tmp_path):
    d = make_design()
    victim = tmp_path / "victim"
    victim.mkdir()
    out = paths.design_dir("build", d.id)
    out.parent.mkdir(parents=True)
    out.symlink_to(victim)
    with pytest.raises(UserError, match="symbolic link"):
        build.build(d, make_fetched(tmp_path, design=d))
    assert list(victim.iterdir()) == []


def test_a_stage_parent_that_is_a_symlink_is_refused(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (tmp_path / "work").mkdir(exist_ok=True)
    (tmp_path / "work" / "reference").symlink_to(victim)
    with pytest.raises(UserError, match="symbolic link"):
        paths.design_dir("reference", ID)
    assert list(victim.iterdir()) == []


# ---- fetch

FETCH_NAMES = ("fetch.json", "fetch.json.tmp", "fetch.log")


def test_the_fetch_stage_writes_through_no_symlink_planted_in_its_directory(project, tmp_path, work):
    c = commit_files(project, {"src/a.v": TOP})
    out = work / "src" / "tt99" / "tt_um_x"
    victim = tmp_path / "victim"
    planted = plant(out, FETCH_NAMES, victim)
    r = fetch.fetch(design(project, c))
    assert r.status == "ok", r.reason
    untouched(planted, victim)
    regular(out, FETCH_NAMES[:1] + FETCH_NAMES[2:])
    again = fetch.fetch(design(project, c))  # the second call reads fetch.json: a record it wrote itself
    assert again.status == "ok"


# ---- runner and results


def test_the_runner_writes_through_no_symlink_planted_in_its_run_directory(tmp_path, work):
    run_dir = paths.design_dir("run", ID)
    victim = tmp_path / "victim"
    planted = plant(run_dir, ("outcome.json", "outcome.json.tmp", "error.log", runner.INFLIGHT), victim)
    state = tmp_path / "state"
    state.mkdir()
    runner._guarded(FakeOps(state), "all", ID, False, None, run_dir)
    untouched(planted, victim)
    regular(run_dir, ("outcome.json",))


def test_results_writes_through_no_symlink_planted_in_its_directory(tmp_path):
    out = tmp_path / "work" / "results"
    victim = tmp_path / "victim"
    planted = plant(out, ("index.json", "index.json.tmp", "results.md", "results.md.tmp"), victim)
    results._write(out / "index.json", "{}\n")
    results._write(out / "results.md", "# x\n")
    untouched(planted, victim)
    regular(out, ("index.json", "results.md"))


def test_results_does_not_read_a_record_through_a_symlink(tmp_path):
    work = tmp_path / "work"
    (work / "build" / "tt99").mkdir(parents=True)
    other = tmp_path / "other.json"
    other.write_text('{"status": "ok"}')
    (work / "build" / "tt99" / "tt_um_x").mkdir()
    (work / "build" / "tt99" / "tt_um_x" / "build.json").symlink_to(other)
    with pytest.raises(results.Invalid):
        results._read(work / "build" / "tt99" / "tt_um_x" / "build.json", work)


# ---- reference (real Verilator)

REF_NAMES = (
    "sim.log", "sim.json", "sim.json.tmp", "reference.json", "reference.json.tmp", "reference-fail.json",
    "reference-fail.json.tmp", "uo_out.bin", "uio.bin", "uo_out.bin.gz", "uio.bin.gz", "uo_out.bin.gz.tmp",
    "uio.bin.gz.tmp", "uo_out_low.bin", "uo_out_low.bin.gz", "uo_out_low.bin.gz.tmp", "sens.log",
    "frame-000.png", "frame-001.png", "obj", "scratch", "scratch-sens",
)  # fmt: skip


def synth_design(tmp_path):
    repo = init(tmp_path / "proj")
    text = (DATA / "tt_um_synth_vga.v").read_text().replace("module tt_um_synth_vga", "module tt_um_x")
    c = commit_files(repo, {"src/tt_um_x.v": text})
    return Design(
        id=ID, shuttle="tt99", macro="tt_um_x", title="", author="", repo=str(repo), commit=c,
        top_module="tt_um_x", source_files=("tt_um_x.v",), clock_hz=25_175_000, clock_declared=True,
        pinout={}, pmods=(), tiles="1x1", render_verdict=None, override={}, patch=None, qspi=None,
        ui_in=0, uio_in=0, scripted=False, candidate=False,
    )  # fmt: skip


try:
    tools.tool("verilator")
    _NO_TOOLS = ""
except Exception as e:  # noqa: BLE001
    _NO_TOOLS = f"verilator not available: {e}"


@pytest.mark.needs_tools
@pytest.mark.skipif(bool(_NO_TOOLS), reason=_NO_TOOLS)
@pytest.mark.parametrize("mode", ["none", "bwrap"])
def test_the_reference_stage_writes_through_no_symlink_planted_in_its_directory(
    tmp_path, work, monkeypatch, mode
):
    if mode == "bwrap" and not sandbox.available():
        pytest.skip(f"bwrap cannot run here: {sandbox.unavailable_reason()}")
    monkeypatch.setenv("TTHFV_SANDBOX", mode)
    d = synth_design(tmp_path)
    out = work / "reference" / "tt99" / "tt_um_x" / "ideal"
    victim = tmp_path / "victim"
    victim_ref = work / "reference" / "tt99" / "tt_um_victim" / "reference.json"
    victim_ref.parent.mkdir(parents=True)
    victim_ref.write_text('{"victim": "untouched"}\n')
    planted = plant(out, REF_NAMES, victim)
    (out / "reference.json").unlink()
    (out / "reference.json").symlink_to(victim_ref)  # the one that matters most: another design's record
    result = reference.make_reference(d, 1_300_000)
    assert result.status == "ok", result
    untouched(planted, victim)
    assert victim_ref.read_text() == '{"victim": "untouched"}\n'
    regular(out, ("sim.log", "sim.json", "reference.json", "uo_out.bin.gz", "uio.bin.gz", "frame-000.png"))
    assert json.loads((out / "reference.json").read_text())["id"] == ID


@pytest.mark.needs_tools
@pytest.mark.skipif(bool(_NO_TOOLS), reason=_NO_TOOLS)
def test_a_reference_directory_that_is_a_symlink_is_refused(tmp_path, work, monkeypatch):
    d = synth_design(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    out = work / "reference" / "tt99" / "tt_um_x"
    out.parent.mkdir(parents=True)
    out.symlink_to(victim)
    with pytest.raises(UserError, match="symbolic link"):
        reference.make_reference(d, 1_300_000)
    assert list(victim.iterdir()) == []
    assert os.path.islink(out)
