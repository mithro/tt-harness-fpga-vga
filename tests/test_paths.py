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


@pytest.mark.parametrize(
    "design_id",
    ["TT08/tt_um_x", "tt08", "tt08/tt_um_x/extra", "tt08/", "/tt_um_x", "tt08/tt um", "tt08/../x"],
)
def test_design_dir_rejects_bad_ids(design_id):
    with pytest.raises(ValueError):
        p.design_dir("build", design_id)


@pytest.mark.parametrize("stage", ["Build", "a/b", "", "..", "a b"])
def test_design_dir_rejects_bad_stage(stage):
    with pytest.raises(ValueError):
        p.design_dir(stage, "tt08/tt_um_x")
