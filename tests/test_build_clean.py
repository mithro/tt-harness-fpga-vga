# SPDX-License-Identifier: Apache-2.0
"""A non-reused build starts from an empty build directory, so its evidence is never a mix of runs."""

from test_build import make_design, make_fetched, rec, toolchain  # noqa: F401 - fixtures

from tthfv import build, paths


def litter(out):
    (out / "old-layout.log").write_text("x")
    (out / "sub").mkdir()
    (out / "sub" / "deep.txt").write_text("x")
    (out / "nextpnr-default-9.log").write_text("x")


def test_files_from_older_layouts_are_removed(toolchain, rec, tmp_path):  # noqa: F811
    d = make_design()
    f = make_fetched(tmp_path, design=d)
    out = paths.design_dir("build", d.id)
    out.mkdir(parents=True)
    litter(out)
    r = build.build(d, f)
    assert r.status == "ok"
    names = {p.name for p in out.iterdir()}
    assert "old-layout.log" not in names and "sub" not in names and "nextpnr-default-9.log" not in names
    assert {"build.json", "design.bin", "tt_fpga_top.v"} <= names


def test_a_reused_build_is_left_alone(toolchain, rec, tmp_path):  # noqa: F811
    d = make_design()
    f = make_fetched(tmp_path, design=d)
    build.build(d, f)
    out = paths.design_dir("build", d.id)
    litter(out)
    build.build(d, f)  # reused: nothing is rebuilt, nothing is cleaned
    assert (out / "old-layout.log").exists()


def test_a_forced_build_cleans_too(toolchain, rec, tmp_path):  # noqa: F811
    d = make_design()
    f = make_fetched(tmp_path, design=d)
    build.build(d, f)
    out = paths.design_dir("build", d.id)
    litter(out)
    build.build(d, f, force=True)
    assert not (out / "old-layout.log").exists()
