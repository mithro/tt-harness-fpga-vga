# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: F811 - pytest fixtures imported from other test modules
"""The build stage reads back only plain files from its scratch directory, and removes it."""

import os

import pytest
from test_build import ICEPACK, fake_tool, make_design, make_fetched, rec, run, toolchain  # noqa: F401

from tthfv import build, paths, safefile


def scratch_of(d):
    return paths.design_dir("build", d.id) / build.SCRATCH


def test_scratch_is_gone_after_a_build_and_the_results_are_in_the_build_directory(toolchain, rec, tmp_path):
    d, r = run(tmp_path)
    assert r.status == "ok", r.reason
    out = paths.design_dir("build", d.id)
    assert not scratch_of(d).exists()
    assert {"design.bin", "out.asc", "out-default.json", "build.json"} <= {p.name for p in out.iterdir()}


def test_keep_scratch_keeps_it(toolchain, rec, tmp_path, monkeypatch):
    monkeypatch.setenv("TTHFV_KEEP_SCRATCH", "1")
    d, r = run(tmp_path)
    assert r.status == "ok", r.reason
    assert (scratch_of(d) / "design.bin").is_file()


@pytest.mark.parametrize(
    ("trick", "word"),
    [
        ('ln -s /etc/hostname "$2"', "plain file"),
        ('mkfifo "$2"', "plain file"),
        ('mkdir "$2"', "plain file"),
        ('head -c 3000000 /dev/zero > "$2"', "too large"),
    ],
)
def test_a_bitstream_that_is_not_a_plain_file_is_a_pack_fail(toolchain, rec, tmp_path, trick, word):
    fake_tool(toolchain, "icepack", f"#!/bin/sh\n{trick}\n")
    d, r = run(tmp_path)
    assert r.status == "pack-fail" and word in r.reason, r.reason
    assert r.bitstream is None
    assert not (paths.design_dir("build", d.id) / "design.bin").exists()


def test_a_second_hard_link_on_the_bitstream_is_a_pack_fail(toolchain, rec, tmp_path):
    fake_tool(toolchain, "icepack", ICEPACK + 'ln "$2" "$2.other"\n')
    d, r = run(tmp_path)
    assert r.status == "pack-fail" and "hard links" in r.reason, r.reason


def test_a_tool_that_fills_scratch_fails_the_stage(toolchain, rec, tmp_path, monkeypatch):
    monkeypatch.setattr(safefile, "MAX_SCRATCH", 100_000)
    fake_tool(
        toolchain,
        "nextpnr-ice40",
        '#!/bin/sh\n[ "$1" = "--version" ] && { echo "nextpnr fake"; exit 0; }\nfor a in "$@"; do last=$a; done\nhead -c 300000 /dev/urandom > "$last"\n'
        "echo ERROR: boom\nexit 1\n",
    )
    d, r = run(tmp_path)
    assert r.status == "pnr-fail" and "scratch" in r.reason, r.reason


def test_a_scratch_symlink_left_by_an_earlier_run_is_not_followed(toolchain, rec, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("x")
    d = make_design()
    out = paths.design_dir("build", d.id)
    out.mkdir(parents=True)
    os.symlink(victim, out / build.SCRATCH)
    r = build.build(d, make_fetched(tmp_path, design=d))
    assert r.status == "ok", r.reason
    assert (victim / "keep").read_text() == "x" and sorted(p.name for p in victim.iterdir()) == ["keep"]
