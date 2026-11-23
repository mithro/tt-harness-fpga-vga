# SPDX-License-Identifier: Apache-2.0
import tarfile

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


def _make_tarball(path):
    import io

    with tarfile.open(path, "w:gz") as tf:
        data = b"#!/bin/sh\n"
        info = tarfile.TarInfo("oss-cad-suite/bin/verilator")
        info.size = len(data)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(data))


@pytest.fixture
def served(monkeypatch, tmp_path):
    """Point the pinned URL at a file:// tarball; returns (tools_dir, tarball_path)."""
    src = tmp_path / "src"
    src.mkdir()
    tarball = src / "suite.tgz"
    monkeypatch.setattr(tools.pins, "OSS_CAD_SUITE_URL", tarball.as_uri())
    dest = tmp_path / "tools"
    monkeypatch.setenv("TTHFV_TOOLS", str(dest))
    return dest, tarball


def test_install_happy_path(served, capsys):
    import hashlib

    dest, tarball = served
    _make_tarball(tarball)
    tools.install()
    assert tools.tool("verilator") == dest / "oss-cad-suite" / "bin" / "verilator"
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
    assert (dest / "oss-cad-suite.sha256").read_text() == f"{digest}  suite.tgz\n"
    assert not (dest / ".oss-cad-suite.tmp").exists()
    assert digest in capsys.readouterr().out


def test_install_second_call_is_noop(served, monkeypatch, capsys):
    _dest, tarball = served
    _make_tarball(tarball)
    tools.install()

    def boom(*a, **k):
        raise AssertionError("URL opened on a no-op install")

    monkeypatch.setattr(tools.urllib.request, "urlopen", boom)
    tools.install()
    assert "already installed" in capsys.readouterr().out


def test_install_download_failure_leaves_nothing(served):
    dest, _tarball = served  # tarball never created, so the download fails
    with pytest.raises(OSError):
        tools.install()
    assert not (dest / "oss-cad-suite").exists()
    assert not list(dest.glob("*.part"))
    with pytest.raises(tools.ToolMissing):
        tools.tool("verilator")


def test_install_extraction_failure_leaves_no_tree(served):
    dest, tarball = served
    tarball.write_bytes(b"this is not a tarball")
    with pytest.raises(tarfile.TarError):
        tools.install()
    assert not (dest / "oss-cad-suite").exists()
    assert not (dest / ".oss-cad-suite.tmp").exists()
    assert not (dest / "oss-cad-suite.sha256").exists()
    with pytest.raises(tools.ToolMissing):
        tools.tool("verilator")
