# SPDX-License-Identifier: Apache-2.0
import pytest

from tthfv import tools


def test_tool_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("TTHFV_TOOLS", str(tmp_path))
    with pytest.raises(tools.ToolMissing) as e:
        tools.tool("yosys")
    assert "tthfv tools install" in str(e.value)


def test_tool_found(monkeypatch, tmp_path):
    monkeypatch.setenv("TTHFV_TOOLS", str(tmp_path))
    exe = tmp_path / "oss-cad-suite" / "bin" / "yosys"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert tools.tool("yosys") == exe


def test_tool_rejects_other_names():
    with pytest.raises(ValueError):
        tools.tool("rm")


def test_env_puts_bin_first(monkeypatch, tmp_path):
    monkeypatch.setenv("TTHFV_TOOLS", str(tmp_path))
    first = tools.env()["PATH"].split(":")[0]
    assert first == str(tmp_path / "oss-cad-suite" / "bin")
