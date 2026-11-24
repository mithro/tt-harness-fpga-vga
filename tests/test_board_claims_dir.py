# SPDX-License-Identifier: Apache-2.0
"""Review fix I2: a session needs the claims directory every project shares, named explicitly."""

import pytest
from board_fakes import FakeBoard

from tthfv.board import status
from tthfv.board.session import board_session
from tthfv.cli import main
from tthfv.errors import UserError


def test_session_refuses_without_an_explicit_claims_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.0)
    monkeypatch.delenv("TTHFV_BOARD_CLAIMS", raising=False)
    monkeypatch.setenv("TTHFV_WORK", str(tmp_path))
    with FakeBoard() as fake:
        fake.env(monkeypatch)
        with pytest.raises(UserError, match="TTHFV_BOARD_CLAIMS") as e, board_session("fpga-4", "t"):
            pytest.fail("the body must not run")
        assert "shared" in str(e.value)
        assert fake.status_reads == 0 and fake.ws_connections == 0
    assert not (tmp_path / "board-claims").exists()


def test_probe_command_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TTHFV_BOARD_CLAIMS", raising=False)
    with FakeBoard() as fake:
        fake.env(monkeypatch)
        assert main(["board", "probe", "fpga-4", "--out", str(tmp_path / "r.json")]) == 1
    assert "TTHFV_BOARD_CLAIMS" in capsys.readouterr().err


def test_status_works_without_it(monkeypatch, capsys):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.0)
    monkeypatch.delenv("TTHFV_BOARD_CLAIMS", raising=False)
    with FakeBoard() as fake:
        fake.env(monkeypatch)
        assert main(["board", "status", "fpga-4"]) == 0
    assert "usable" in capsys.readouterr().out
