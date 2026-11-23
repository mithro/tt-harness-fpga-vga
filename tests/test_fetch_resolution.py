# SPDX-License-Identifier: Apache-2.0
"""The fetch stage decides once what a design is: top module, compilation units, source digest."""

import json
from pathlib import Path

import pytest
from test_fetch import TOP, commit_files, design, project, work  # noqa: F401 - fixtures

from tthfv import fetch


def fetch_json(work: Path) -> dict:  # noqa: F811
    return json.loads((work / "src" / "tt99" / "tt_um_x" / "fetch.json").read_text())


def test_declared_top_module_is_used_without_fallback(project, work):  # noqa: F811
    c = commit_files(project, {"src/a.v": TOP + "module tt_um_other;\nendmodule\n"})
    r = fetch.fetch(design(project, c))
    assert (r.status, r.top_module_used, r.top_module_fallback) == ("ok", "tt_um_x", False)
    rec = fetch_json(work)
    assert rec["top_module_used"] == "tt_um_x" and rec["top_module_fallback"] is False


def test_wrong_top_module_falls_back_to_the_single_tt_um_module(project, work):  # noqa: F811
    c = commit_files(project, {"src/a.v": "module helper;\nendmodule\nmodule tt_um_real;\nendmodule\n"})
    r = fetch.fetch(design(project, c))
    assert r.status == "ok", r.reason
    assert (r.top_module_used, r.top_module_fallback) == ("tt_um_real", True)
    rec = fetch_json(work)
    assert rec["top_module_used"] == "tt_um_real" and rec["top_module_fallback"] is True


def test_wrong_top_module_with_several_candidates_is_fetch_fail(project):  # noqa: F811
    c = commit_files(project, {"src/a.v": "module tt_um_a;\nendmodule\nmodule tt_um_b;\nendmodule\n"})
    r = fetch.fetch(design(project, c))
    assert r.status == "fetch-fail"
    assert "tt_um_x" in r.reason and "tt_um_a" in r.reason and "tt_um_b" in r.reason
    r = fetch.fetch(design(project, c, top_module="nope"), force=True)
    assert r.status == "fetch-fail" and "nope" in r.reason


def test_no_tt_um_module_at_all_is_fetch_fail(project):  # noqa: F811
    c = commit_files(project, {"src/a.v": "module helper;\nendmodule\n"})
    r = fetch.fetch(design(project, c))
    assert r.status == "fetch-fail" and "0 tt_um_" in r.reason


def test_module_names_in_comments_and_endmodule_are_not_declarations(project):  # noqa: F811
    text = "// module tt_um_ghost;\n/* module tt_um_ghost2; */\nmodule tt_um_real;\nendmodule\n"
    c = commit_files(project, {"src/a.v": text})
    r = fetch.fetch(design(project, c))
    assert r.status == "ok" and r.top_module_used == "tt_um_real"


def test_headers_are_not_compilation_units_but_their_directory_is_searched(project):  # noqa: F811
    c = commit_files(
        project,
        {
            "src/a.v": TOP,
            "inc/defs.svh": "`define A 1\n",
            "src/b.vh": "`define B 1\n",
            "src/cfg.vlt": "`verilator_config\n",
            "src/rom.hex": "00\n",
            "src/sub/c.sv": "module c;\nendmodule\n",
        },
    )
    names = ("a.v", "../inc/defs.svh", "b.vh", "cfg.vlt", "rom.hex", "sub/c.sv")
    r = fetch.fetch(design(project, c, source_files=names))
    assert r.status == "ok", r.reason
    assert [p.name for p in r.synth_sources] == ["a.v", "c.sv"]
    assert [p.name for p in r.sources] == [Path(n).name for n in names]
    assert r.include_dirs[0] == r.src_dir / "src"
    assert r.src_dir / "inc" in r.include_dirs


@pytest.mark.parametrize("name", ["a.vhd", "a.vhdl"])
def test_vhdl_sources_are_not_supported(project, name):  # noqa: F811
    c = commit_files(project, {"src/a.v": TOP, f"src/{name}": "entity e is end;\n"})
    r = fetch.fetch(design(project, c, source_files=("a.v", name)))
    assert r.status == "fetch-fail" and r.reason == "VHDL sources are not supported"
    assert r.sources == ()


def test_only_headers_is_fetch_fail(project):  # noqa: F811
    c = commit_files(project, {"src/a.vh": "`define A\n"})
    r = fetch.fetch(design(project, c, source_files=("a.vh",)))
    assert r.status == "fetch-fail" and "no .v or .sv" in r.reason


def test_sources_sha256_is_recorded_and_follows_every_file_in_src(project, work):  # noqa: F811
    c = commit_files(project, {"src/a.v": TOP, "src/rom.hex": "00\n"})
    d = design(project, c)
    r = fetch.fetch(d)
    assert r.sources_sha256 and fetch_json(work)["sources_sha256"] == r.sources_sha256
    assert fetch.fetch(d).sources_sha256 == r.sources_sha256
    (r.src_dir / "src" / "rom.hex").write_text("ff\n")  # an unlisted file the design reads
    again = fetch.fetch(d)  # reuses the checkout, but digests it again
    assert again.sources_sha256 != r.sources_sha256
    assert fetch_json(work)["sources_sha256"] == again.sources_sha256


def test_sources_sha256_function(tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / ".git").mkdir()
    (src / "a.v").write_text("a")
    (src / "sub" / "b.hex").write_text("b")
    base = fetch.sources_sha256(src)
    (src / ".git" / "HEAD").write_text("ignored")
    assert fetch.sources_sha256(src) == base
    (src / "sub" / "b.hex").write_text("c")
    assert fetch.sources_sha256(src) != base
    (src / "sub" / "b.hex").write_text("b")
    (src / "sub" / "b2.hex").write_text("")  # an empty file still counts
    assert fetch.sources_sha256(src) != base
    (src / "sub" / "b2.hex").unlink()
    (src / "sub" / "b.hex").rename(src / "sub" / "z.hex")  # names count, not only contents
    assert fetch.sources_sha256(src) != base
