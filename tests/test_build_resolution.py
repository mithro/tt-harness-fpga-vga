# SPDX-License-Identifier: Apache-2.0
"""The build works from the fetch stage's resolution of the design (fake toolchain)."""

import json

import pytest
from test_build import (  # noqa: F401 - fixtures
    SV2V,
    fake_tool,
    make_design,
    make_fetched,
    rec,
    run,
    toolchain,
)

from tthfv import build, paths


def ys(d) -> str:
    return (paths.design_dir("build", d.id).resolve() / "yosys-default.ys").read_text()


def test_wrong_top_module_builds_the_single_tt_um_module(toolchain, rec, tmp_path):  # noqa: F811
    d = make_design(top_module="tt_um_wrong")
    src = tmp_path / "repo" / "src"
    src.mkdir(parents=True)
    (src / "a.v").write_text("module tt_um_real;\nendmodule\n")
    f = make_fetched(tmp_path, design=d)
    assert f.top_module_used == "tt_um_real"
    r = build.build(d, f)
    assert r.status == "ok", r.reason
    wrapper = (paths.design_dir("build", d.id) / "tt_fpga_top.v").read_text()
    assert "tt_um_real user_project" in wrapper and "tt_um_wrong" not in wrapper
    on_disk = json.loads((paths.design_dir("build", d.id) / "build.json").read_text())
    assert on_disk["top_module_used"] == "tt_um_real"


def test_a_header_in_the_source_list_is_not_a_compilation_unit(toolchain, rec, tmp_path):  # noqa: F811
    names = ("a.v", "defs.svh", "rom.hex", "cfg.vlt")
    d, r = run(tmp_path, names=names)
    assert r.status == "ok", r.reason
    text = ys(d)
    reads = [ln for ln in text.splitlines() if ln.startswith("read_verilog")]
    assert len(reads) == 2  # the wrapper and a.v
    for ln in reads:
        assert "defs.svh" not in ln and "rom.hex" not in ln and "cfg.vlt" not in ln
    assert f'-I "{tmp_path / "repo" / "src"}"' in text  # the directory stays on the include path


def test_build_json_records_what_was_built(toolchain, rec, tmp_path):  # noqa: F811
    d, r = run(tmp_path, commit="c" * 40, repo="https://example.com/r")
    f = make_fetched(tmp_path, design=d)
    on_disk = json.loads((paths.design_dir("build", d.id) / "build.json").read_text())
    assert on_disk["top_module_used"] == "tt_um_x"
    assert on_disk["sources_sha256"] == f.sources_sha256 != ""
    assert (on_disk["commit"], on_disk["repo"]) == ("c" * 40, "https://example.com/r")
    assert on_disk["verilator_flags"] == []
    assert r.sources_sha256 == f.sources_sha256


def test_changed_hex_file_changes_the_digest_and_rebuilds(toolchain, rec, tmp_path):  # noqa: F811
    d = make_design()
    (tmp_path / "repo" / "src").mkdir(parents=True)
    (tmp_path / "repo" / "src" / "rom.hex").write_text("00\n")
    f1 = make_fetched(tmp_path, design=d)
    build.build(d, f1)
    build.build(d, f1)
    assert (rec / "yosys.count").read_text().split() == ["run"]
    (tmp_path / "repo" / "src" / "rom.hex").write_text("ff\n")
    f2 = make_fetched(tmp_path, design=d)
    assert f2.sources_sha256 != f1.sources_sha256
    r = build.build(d, f2)
    assert (rec / "yosys.count").read_text().split() == ["run", "run"]
    assert r.sources_sha256 == f2.sources_sha256


def test_new_commit_with_identical_sources_is_recorded(toolchain, rec, tmp_path):  # noqa: F811
    d = make_design(commit="a" * 40)
    f = make_fetched(tmp_path, design=d)
    build.build(d, f)
    r = build.build(make_design(commit="b" * 40), f)
    assert r.commit == "b" * 40
    assert (rec / "yosys.count").read_text().split() == ["run", "run"]


def test_defines_among_the_verilator_flags_reach_yosys(toolchain, rec, tmp_path):  # noqa: F811
    flags = ["-DFOO=1", "+define+BAR+BAZ=2", "-Wno-fatal", "-DQUX"]
    d, r = run(tmp_path, override={"verilator_flags": flags})
    assert r.status == "ok", r.reason
    text = ys(d)
    for line in (ln for ln in text.splitlines() if ln.startswith("read_verilog")):
        assert "-D FOO=1" in line and "-D BAR" in line and "-D BAZ=2" in line and "-D QUX" in line
        assert "-Wno-fatal" not in line
    assert r.verilator_flags == flags
    on_disk = json.loads((paths.design_dir("build", d.id) / "build.json").read_text())
    assert on_disk["verilator_flags"] == flags


def test_a_changed_define_rebuilds(toolchain, rec, tmp_path):  # noqa: F811
    f = make_fetched(tmp_path)
    build.build(make_design(override={"verilator_flags": ["-DA=1"]}), f)
    build.build(make_design(override={"verilator_flags": ["-DA=2"]}), f)
    assert (rec / "yosys.count").read_text().split() == ["run", "run"]


def test_a_define_that_cannot_go_in_a_yosys_script_is_rejected(toolchain, rec, tmp_path):  # noqa: F811
    _d, r = run(tmp_path, override={"verilator_flags": ['-DA=1";shell id']})
    assert r.status == "synth-fail" and "unsafe macro definition" in r.reason
    assert not (rec / "yosys.count").exists()


@pytest.mark.parametrize(
    "flags, want",
    [
        (["-DA", "-DB=2"], ["A", "B=2"]),
        (["+define+A+B=2"], ["A", "B=2"]),
        (["--timing", "-Wall", "-D"], []),
        ([], []),
    ],
)
def test_defines_parser(flags, want):
    assert build.defines(flags) == want


def test_sv2v_on_the_host_path_is_never_used(toolchain, rec, tmp_path, monkeypatch):  # noqa: F811
    host = tmp_path / "hostbin"
    host.mkdir()
    fake_tool(host, "sv2v", SV2V)
    monkeypatch.setenv("PATH", f"{host}:{toolchain.parent}")
    monkeypatch.setenv("FAKE_YOSYS_FAIL", "1")
    _d, r = run(tmp_path)
    assert r.status == "synth-fail" and r.sv2v is False
    assert not (rec / "sv2v.args").exists()
    assert "sv2v" not in r.tools


def test_pinned_sv2v_is_used(toolchain, rec, tmp_path, monkeypatch):  # noqa: F811
    fake_tool(toolchain, "sv2v", SV2V)
    monkeypatch.setenv("FAKE_YOSYS_FAIL", "1")
    _d, r = run(tmp_path, names=("a.sv",))
    assert r.status == "ok" and r.sv2v is True
