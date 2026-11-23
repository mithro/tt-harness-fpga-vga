# SPDX-License-Identifier: Apache-2.0
"""`tools install` is keyed on a marker naming the installed version, so a pin bump reinstalls."""

import pytest
from test_tools import _make_tarball, _pin, served  # noqa: F401 - fixtures

from tthfv import tools


def marker(dest):
    return dest / "oss-cad-suite" / ".tthfv-installed"


def test_a_successful_install_writes_the_marker(served):  # noqa: F811
    dest, tarball = served
    _make_tarball(tarball)
    tools.install()
    assert marker(dest).read_text().strip() == tools.pins.OSS_CAD_SUITE_VERSION


def test_a_tree_without_a_marker_is_migrated_not_reinstalled(served, monkeypatch, capsys):  # noqa: F811
    dest, _tarball = served  # no tarball: a download would fail
    exe = dest / "oss-cad-suite" / "bin" / "verilator"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")

    def boom(*a, **k):
        raise AssertionError("downloaded")

    monkeypatch.setattr(tools.urllib.request, "urlopen", boom)
    tools.install()
    assert marker(dest).read_text().strip() == tools.pins.OSS_CAD_SUITE_VERSION
    assert "marker" in capsys.readouterr().out
    capsys.readouterr()
    tools.install()
    assert "already installed" in capsys.readouterr().out


def test_a_marker_naming_another_version_reinstalls(served, capsys):  # noqa: F811
    dest, tarball = served
    _make_tarball(tarball)
    tools.install()
    marker(dest).write_text("2020-01-01\n")
    capsys.readouterr()
    tools.install()
    out = capsys.readouterr().out
    assert "downloading" in out and "already installed" not in out
    assert marker(dest).read_text().strip() == tools.pins.OSS_CAD_SUITE_VERSION


def test_a_pin_bump_reinstalls(served, monkeypatch):  # noqa: F811
    dest, tarball = served
    _make_tarball(tarball)
    tools.install()
    monkeypatch.setattr(tools.pins, "OSS_CAD_SUITE_VERSION", "2099-01-01")
    tools.install()
    assert marker(dest).read_text().strip() == "2099-01-01"


def test_a_failed_install_leaves_no_marker(served):  # noqa: F811
    dest, tarball = served
    tarball.write_bytes(b"not a tarball")
    _pin(tarball)
    with pytest.raises(Exception):  # noqa: B017
        tools.install()
    assert not marker(dest).exists()
