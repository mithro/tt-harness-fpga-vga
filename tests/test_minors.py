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
