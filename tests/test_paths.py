# SPDX-License-Identifier: Apache-2.0
import importlib

import pytest

import tthfv.paths as p


def test_design_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TTHFV_WORK", str(tmp_path))
    importlib.reload(p)
    try:
        assert p.design_dir("build", "tt08/tt_um_x") == tmp_path / "build" / "tt08" / "tt_um_x"
    finally:
        monkeypatch.undo()
        importlib.reload(p)


def test_design_dir_rejects_traversal():
    with pytest.raises(ValueError):
        p.design_dir("build", "../etc/passwd")
