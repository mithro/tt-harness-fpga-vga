# SPDX-License-Identifier: Apache-2.0
import types

import pytest

from tthfv import cli
from tthfv.cli import main


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0
    assert "tthfv" in capsys.readouterr().out


def test_no_subcommand_is_an_error():
    with pytest.raises(SystemExit) as e:
        main([])
    assert e.value.code == 2


def test_registered_stage_is_dispatched(monkeypatch):
    seen = []
    stage = types.ModuleType("fake_stage")

    def add_parser(sub):
        p = sub.add_parser("fake")
        p.add_argument("--n", type=int, default=0)
        p.set_defaults(func=lambda args: seen.append(args.n) or 7)

    stage.add_parser = add_parser
    monkeypatch.setattr(cli, "STAGES", [stage])
    assert main(["fake", "--n", "3"]) == 7
    assert seen == [3]


def test_missing_tools_is_a_clean_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TTHFV_TOOLS", str(tmp_path))
    assert main(["tools", "versions"]) == 1
    err = capsys.readouterr().err
    assert "tthfv tools install" in err
    assert "Traceback" not in err


def test_unknown_design_is_a_clean_error(monkeypatch, capsys):
    from pathlib import Path

    monkeypatch.setenv("TTHFV_VIDEOS_REPO", str(Path(__file__).parent / "data" / "videos-mini"))
    assert main(["manifest", "show", "tt08/nonexistent"]) == 1
    err = capsys.readouterr().err
    assert "tt08/nonexistent" in err
    assert "Traceback" not in err


def test_tool_missing_is_a_user_error():
    from tthfv.errors import UserError
    from tthfv.tools import ToolMissing

    assert issubclass(ToolMissing, UserError)
