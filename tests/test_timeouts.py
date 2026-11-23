# SPDX-License-Identifier: Apache-2.0
"""Every external call has a timeout and kills its whole process group; the build has a design budget."""

import os
import shutil
import time
from pathlib import Path

import pytest
from gitrepo import commit_files, init
from test_build import (  # noqa: F401 - fixtures
    SV2V,
    fake_tool,
    make_design,
    make_fetched,
    rec,
    run,
    toolchain,
)
from test_fetch import TOP, design, work  # noqa: F401 - fixtures
from test_proc import gone

from tthfv import build, fetch, manifest, paths, proc, reference, tools
from tthfv.cli import build_parser
from tthfv.errors import UserError

# A tool that starts a grandchild, records its pid and waits for it (a stand-in for yosys-abc).
HANG = """#!/bin/sh
sleep 60 &
echo $! > "$FAKE_REC/child.pid"
wait
"""
# Answers --version, hangs otherwise.
HANG_UNLESS_VERSION = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "fake 1.0"; exit 0; fi
sleep 60 &
echo $! > "$FAKE_REC/child.pid"
wait
"""


def child_gone(rec_dir: Path) -> bool:
    return gone(int((rec_dir / "child.pid").read_text()))


def test_default_timeouts_are_the_documented_ones():
    assert (build.YOSYS_TIMEOUT, build.NEXTPNR_TIMEOUT, build.ICEPACK_TIMEOUT) == (900, 900, 120)
    assert (reference.VERILATE_TIMEOUT, reference.SIM_TIMEOUT, proc.GIT_TIMEOUT) == (900, 600, 300)
    assert build.DEFAULT_DESIGN_TIMEOUT == 3600


def test_yosys_timeout_kills_its_children(toolchain, rec, tmp_path, monkeypatch):  # noqa: F811
    fake_tool(toolchain, "yosys", HANG_UNLESS_VERSION)
    monkeypatch.setattr(build, "YOSYS_TIMEOUT", 1)
    started = time.monotonic()
    _d, r = run(tmp_path)
    assert time.monotonic() - started < 20
    assert r.status == "synth-fail" and "yosys timed out" in r.reason
    assert child_gone(rec)


def test_nextpnr_timeout_kills_its_children(toolchain, rec, tmp_path, monkeypatch):  # noqa: F811
    fake_tool(toolchain, "nextpnr-ice40", HANG_UNLESS_VERSION)
    monkeypatch.setattr(build, "NEXTPNR_TIMEOUT", 1)
    _d, r = run(tmp_path)
    assert r.status == "pnr-fail" and "nextpnr timed out" in r.reason
    assert child_gone(rec)


def test_icepack_timeout_is_pack_fail(toolchain, rec, tmp_path, monkeypatch):  # noqa: F811
    fake_tool(toolchain, "icepack", HANG)
    monkeypatch.setattr(build, "ICEPACK_TIMEOUT", 1)
    _d, r = run(tmp_path)
    assert r.status == "pack-fail" and "icepack timed out" in r.reason
    assert child_gone(rec)


def test_sv2v_timeout_is_a_synth_fail(toolchain, rec, tmp_path, monkeypatch):  # noqa: F811
    fake_tool(toolchain, "sv2v", HANG_UNLESS_VERSION)
    monkeypatch.setenv("FAKE_YOSYS_FAIL", "1")
    monkeypatch.setattr(build, "SV2V_TIMEOUT", 1)
    _d, r = run(tmp_path, names=("a.sv",))
    assert r.status == "synth-fail" and "sv2v" in r.reason and "timed out" in r.reason
    assert child_gone(rec)


def test_a_hanging_version_query_is_a_user_error(toolchain, rec, tmp_path, monkeypatch):  # noqa: F811
    fake_tool(toolchain, "yosys", HANG)
    monkeypatch.setattr(tools, "VERSION_TIMEOUT", 1)
    with pytest.raises(UserError, match="timed out"):
        run(tmp_path)
    assert child_gone(rec)


def test_tools_versions_has_a_timeout(toolchain, rec, monkeypatch):  # noqa: F811
    fake_tool(toolchain, "nextpnr-ice40", HANG)
    monkeypatch.setattr(tools, "VERSION_TIMEOUT", 1)
    with pytest.raises(UserError, match="nextpnr-ice40.*timed out"):
        tools.versions()
    assert child_gone(rec)


def test_tool_versions_are_asked_once_per_process(toolchain, rec, tmp_path):  # noqa: F811
    counting = '#!/bin/sh\nif [ "$1" = "--version" ]; then echo asked >> "$FAKE_REC/yosys.versions"; echo "Yosys c 1"; exit 0; fi\n'
    real = (toolchain / "yosys").read_text()
    fake_tool(toolchain, "yosys", counting + real.split("\n", 2)[2])
    run(tmp_path)
    run(tmp_path, force=True)
    other = make_design(id="tt99/tt_um_y", macro="tt_um_y")
    build.build(other, make_fetched(tmp_path / "other", design=other))
    assert (rec / "yosys.versions").read_text().split() == ["asked"]


# ---- the design budget


def test_the_design_budget_stops_the_sweep_and_keeps_the_best_attempt(toolchain, rec, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FAKE_PNR_SLEEP", "0.5")
    d = make_design(clock_hz=25_175_000)  # every attempt misses timing, so the sweep would run all 8
    f = make_fetched(tmp_path, design=d)
    r = build.build(d, f, design_timeout=1.5)
    assert r.status == "timing-fail" and r.bitstream
    assert 1 <= len(r.attempts) < 8
    assert "design timeout" in r.reason


def test_a_design_budget_used_up_by_one_tool_is_pnr_fail(toolchain, rec, tmp_path, monkeypatch):  # noqa: F811
    fake_tool(toolchain, "nextpnr-ice40", HANG_UNLESS_VERSION)
    d = make_design()
    r = build.build(d, make_fetched(tmp_path, design=d), design_timeout=2)
    assert (r.status, r.reason) == ("pnr-fail", "design timeout")
    assert child_gone(rec)


def test_no_budget_left_means_no_attempt(toolchain, rec, tmp_path):  # noqa: F811
    d = make_design()
    r = build.build(d, make_fetched(tmp_path, design=d), design_timeout=0)
    assert (r.status, r.reason) == ("pnr-fail", "design timeout")
    assert not (rec / "yosys.count").exists()


def test_the_cli_takes_a_design_timeout():
    args = build_parser().parse_args(["build", "--design-timeout", "120", "tt01/x"])
    assert args.design_timeout == 120
    assert build_parser().parse_args(["build", "tt01/x"]).design_timeout == build.DEFAULT_DESIGN_TIMEOUT


# ---- git


@pytest.fixture
def slow_git(tmp_path, monkeypatch):
    """A git that hangs (after starting a grandchild) for the subcommand in $SLOW_GIT, else is the real one."""
    real = shutil.which("git")
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    script = bindir / "git"
    script.write_text(
        f'#!/bin/sh\nif [ "$1" = "$SLOW_GIT" ]; then\n  sleep 60 &\n  echo $! > "$FAKE_REC/child.pid"\n  wait\n  exit 1\nfi\nexec {real} "$@"\n'
    )
    script.chmod(0o755)
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    monkeypatch.setenv("FAKE_REC", str(rec_dir))
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setattr(proc, "GIT_TIMEOUT", 1)
    return rec_dir


def test_a_hanging_git_fetch_is_fetch_fail_and_its_children_die(tmp_path, slow_git, monkeypatch, work):  # noqa: F811
    monkeypatch.setenv("SLOW_GIT", "fetch")
    repo = init(tmp_path / "proj")
    c = commit_files(repo, {"src/a.v": TOP})
    r = fetch.fetch(design(repo, c))
    assert r.status == "fetch-fail" and "timed out" in r.reason
    assert child_gone(slow_git)


def test_a_hanging_gitmodules_listing_is_fetch_fail(tmp_path, slow_git, monkeypatch, work):  # noqa: F811
    monkeypatch.setenv("SLOW_GIT", "config")
    repo = init(tmp_path / "proj")
    c = commit_files(
        repo, {"src/a.v": TOP, ".gitmodules": '[submodule "x"]\n\tpath = x\n\turl = git@github.com:a/b\n'}
    )
    r = fetch.fetch(design(repo, c))
    assert r.status == "fetch-fail" and "timed out" in r.reason
    assert child_gone(slow_git)


def test_a_hanging_git_in_the_manifest_is_a_user_error(tmp_path, slow_git, monkeypatch):
    monkeypatch.setenv("SLOW_GIT", "clone")
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    with pytest.raises(UserError, match="git clone.*timed out"):
        manifest.sync_videos_repo()
    assert child_gone(slow_git)


# ---- sim.log


@pytest.mark.needs_tools
def test_the_sim_log_is_bounded_and_keeps_the_final_message(tmp_path, monkeypatch):
    try:
        tools.tool("verilator")
    except tools.ToolMissing as e:
        pytest.skip(f"verilator not available: {e}")
    from test_reference import COUNTER, make

    text = COUNTER.replace(
        "assign uo_out  = count;",
        "assign uo_out  = count;\n  integer i;\n  always @(posedge clk) begin\n"
        '    for (i = 0; i < 400; i = i + 1) $display("0123456789012345678901234567890123456789012345678901234567890123");\n'
        "    if (count == 8'd200) $finish;\n  end",
    )
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    d, fr = make(tmp_path, text)
    r = reference.simulate(d, fr, clocks=60_000)
    assert r.status == "sim-fail", r.reason
    assert "design finished at clock" in r.reason
    log = r.log.read_text()
    assert "[output truncated:" in log
    assert r.log.stat().st_size < 3 * proc.LOG_CAP  # Verilator's and the testbench's, each capped
