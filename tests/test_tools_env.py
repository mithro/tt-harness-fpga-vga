# SPDX-License-Identifier: Apache-2.0
"""Tools come only from the pinned toolchain; the rest of PATH stays usable."""

import shutil
import stat

import pytest

from tthfv import tools

TOOLS = ("yosys", "nextpnr-ice40", "icepack", "verilator", "sv2v")


def script(path, text="#!/bin/sh\nexit 0\n"):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A host bin directory with every guarded tool and a harmless one, on PATH; empty pinned toolchain."""
    d = tmp_path / "hostbin"
    d.mkdir()
    for name in (*TOOLS, "git-like"):
        script(d / name)
    monkeypatch.setenv("PATH", f"{d}:/usr/bin")
    monkeypatch.setenv("TTHFV_TOOLS", str(tmp_path / "tools"))
    return d


@pytest.mark.parametrize("name", TOOLS)
def test_host_tools_are_not_found_through_the_environment(host, name):
    assert shutil.which(name, path=tools.env()["PATH"]) is None


def test_other_host_programs_stay_reachable(host):
    path = tools.env()["PATH"]
    assert shutil.which("git-like", path=path) is not None
    assert shutil.which("sh", path=path) is not None


def test_sv2v_is_looked_up_in_the_pinned_bin_only(host, tmp_path):
    assert tools.sv2v() is None
    pinned = tmp_path / "tools" / "oss-cad-suite" / "bin"
    pinned.mkdir(parents=True)
    script(pinned / "sv2v")
    assert tools.sv2v() == pinned / "sv2v"
    assert shutil.which("sv2v", path=tools.env()["PATH"]) == str(pinned / "sv2v")


def test_the_environment_is_an_allow_list_not_a_copy(host, monkeypatch):
    for name in ("VERILATOR_ROOT", "VERILATOR_BIN", "YOSYSHQ_ROOT", "SECRET_TOKEN", "CXX", "PYTHONPATH"):
        monkeypatch.setenv(name, "from-the-host")
    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    e = tools.env()
    assert set(e) <= {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"}
    assert e["HOME"] == "/home/someone" and e["LANG"] == "en_US.UTF-8"


def test_a_host_dir_without_tools_is_left_alone(tmp_path, monkeypatch):
    d = tmp_path / "plain"
    d.mkdir()
    script(d / "git-like")
    monkeypatch.setenv("PATH", str(d))
    monkeypatch.setenv("TTHFV_TOOLS", str(tmp_path / "tools"))
    assert str(d) in tools.env()["PATH"].split(":")
