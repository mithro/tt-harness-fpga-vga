# SPDX-License-Identifier: Apache-2.0
"""Fix round 1, minors: exact flip-flop primitive names, the trash emptied by every command, the warning for
TTHFV_SANDBOX=none, `peak` without limits."""

import json
import re
from pathlib import Path

import pytest

from tthfv import build, limits, paths, reports, sandbox, tools
from tthfv.cli import main


def stat(**cells) -> bytes:
    return json.dumps({"design": {"num_cells_by_type": cells}}).encode()


def test_a_user_module_named_like_a_flip_flop_is_not_counted():
    got = reports.parse_stat(stat(SB_DFF_FORGE=5, SB_DFFXYZ=3, SB_DFFESR=2, SB_DFFN=1, SB_DFF=4))
    assert got["SB_DFF"] == 7  # SB_DFF 4 + SB_DFFESR 2 + SB_DFFN 1; not the two names that are not primitives


def test_the_flip_flop_names_are_the_ones_of_yosys_own_cell_library():
    try:
        sim = (tools.suite_dir() / "share" / "yosys" / "ice40" / "cells_sim.v").read_text()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"toolchain not available: {e}")
    names = set(re.findall(r"^module (SB_DFF\w*)", sim, re.MULTILINE))
    assert names == set(reports.DFF_PRIMITIVES)


def test_peak_is_none_when_there_are_no_limits(monkeypatch):
    assert build.BuildResult(id="x/y", status="ok").peak is None
    assert sandbox.limits_record() is None  # the suite runs without the sandbox and limits


@pytest.fixture
def trash(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    (tmp_path / "work" / "trash" / "x").mkdir(parents=True)
    (tmp_path / "work" / "trash" / "y").write_text("y")
    return tmp_path / "work" / "trash"


def test_build_and_reference_empty_the_trash_like_run(trash, monkeypatch, capsys):
    monkeypatch.setattr(build.manifest, "load", lambda **kw: [])
    monkeypatch.setattr(build.manifest, "by_id", lambda designs, i: (_ for _ in ()).throw(SystemExit("stop")))
    for command in ("build", "reference"):
        (trash / "z").write_text("z")
        with pytest.raises(SystemExit):
            main([command, "tt99/tt_um_a"])
        assert "removed 3 entries" in capsys.readouterr().out, command
        assert list(trash.iterdir()) == []
        (trash / "x").mkdir(exist_ok=True)
        (trash / "y").write_text("y")


def test_sandbox_none_says_the_resource_limits_are_off_too(monkeypatch, capsys):
    monkeypatch.setenv("TTHFV_SANDBOX", "none")
    monkeypatch.setattr(sandbox, "_warned", False)
    monkeypatch.setattr(limits, "_warned", False)
    sandbox.announce()
    sandbox.announce()
    err = capsys.readouterr().err
    assert err.count("WARNING") == 1 and "resource limits" in err and "off" in err


def test_the_sv2v_flag_is_flagged_as_unverified_where_it_is_used():
    assert "NOT verified against a real sv2v" in Path(build.__file__).read_text()
    assert "sv2v" in (Path(__file__).parents[1] / "README.md").read_text().split("## Known limitations")[1]


def run_wrapper(tmp_path, expected_parent: int):
    """The scope's wrapper script on its own (no systemd), started by this process, told that the process that
    started the scope is `expected_parent`; the tool it would run touches a marker."""
    import os
    import subprocess

    rfd, wfd = os.pipe()
    grfd, gwfd = os.pipe()  # the guard's lifeline: its write end stays open here until the wrapper is done
    marker = tmp_path / "ran"
    script = limits.WRAPPER.format(fd=wfd, gfd=grfd, unit="tthfv-test-1", ppid=expected_parent)
    done = subprocess.run(
        ["bash", "-c", script, "bash", "sh", "-c", f"touch {marker}"],
        pass_fds=(wfd, grfd), check=False, capture_output=True, timeout=30,
    )  # fmt: skip
    for fd in (rfd, wfd, grfd, gwfd):
        os.close(fd)
    return done.returncode, marker.exists()


def test_the_wrapper_does_not_start_the_tool_when_its_parent_is_not_the_one_that_started_it(tmp_path):
    """The race between fork and the death signal: a worker that died before setpriv's prctl leaves a wrapper whose
    parent is no longer the worker. By construction: a wrong expected parent is that situation."""
    import os

    rc, ran = run_wrapper(tmp_path, os.getpid() + 1_000_000)
    assert (rc, ran) == (99, False)
    rc, ran = run_wrapper(tmp_path, os.getpid())
    assert (rc, ran) == (0, True)


def test_the_scope_command_line_names_the_parent_it_expects():
    import os

    argv = limits.scope_argv(["bwrap"], limits.Limits(), "tthfv-1-1", 10, 9, 8)
    assert f'[ "$PPID" = "{os.getpid()}" ]' in argv[argv.index("-c") + 1]
    argv = limits.scope_argv(["bwrap"], limits.Limits(), "tthfv-1-1", 10, 9, 8, parent_pid=4242)
    assert '[ "$PPID" = "4242" ]' in argv[argv.index("-c") + 1]


def test_a_command_says_which_stale_slices_it_stopped(monkeypatch, capsys):
    monkeypatch.setenv("TTHFV_SANDBOX", "bwrap")
    monkeypatch.setenv("TTHFV_LIMITS", "scope")
    monkeypatch.setattr(limits, "sweep_stale_slices", lambda: ["tthfv_run_1_2.slice", "tthfv_run_3_4.slice"])
    assert sandbox.command(lambda: 0) == 0
    out = capsys.readouterr().out
    assert out == "stopped 2 stale run slice(s) of dead owners: tthfv_run_1_2.slice, tthfv_run_3_4.slice\n"
    monkeypatch.setattr(limits, "sweep_stale_slices", list)
    sandbox.command(lambda: 0)
    assert capsys.readouterr().out == ""
