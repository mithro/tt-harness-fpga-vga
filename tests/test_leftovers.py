# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: F811 - pytest fixtures imported from other test modules
"""Smaller leftovers of the sandbox review: the trash directory, FIFOs as log names, a symlinked run
directory, and sv2v's output."""

import os
import threading

import pytest
from fake_ops import FakeOps
from test_build import SV2V, fake_tool, rec, run, toolchain  # noqa: F401

from tthfv import paths, runner, safefile
from tthfv.cli import main


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    (tmp_path / "work").mkdir()
    return tmp_path / "work"


# ---- C2: the trash directory


def test_discard_does_not_move_anything_through_a_symlinked_trash(work, tmp_path, monkeypatch):
    victim = tmp_path / "victim"
    victim.mkdir()
    (work / "trash").symlink_to(victim)
    stuck = tmp_path / "stage" / "scratch"
    stuck.mkdir(parents=True)
    (stuck / "f").write_text("x")
    monkeypatch.setattr(safefile, "remove_tree", lambda p: (_ for _ in ()).throw(PermissionError("no")))
    note = safefile.discard(stuck, work / "trash")
    assert "symbolic link" in note or "not a plain directory" in note, note
    assert list(victim.iterdir()) == []
    assert stuck.exists()  # still there: it could not be removed or moved


def test_discard_moves_into_a_real_trash_by_directory_descriptor(work, tmp_path, monkeypatch):
    stuck = tmp_path / "stage" / "scratch"
    stuck.mkdir(parents=True)
    (stuck / "f").write_text("x")
    monkeypatch.setattr(safefile, "remove_tree", lambda p: (_ for _ in ()).throw(PermissionError("no")))
    note = safefile.discard(stuck, work / "trash")
    assert "moved to" in note and not stuck.exists()
    (moved,) = (work / "trash").iterdir()
    assert (moved / "f").read_text() == "x"


def test_empty_trash_removes_what_is_in_it_without_following_links(work, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("k")
    trash = work / "trash"
    (trash / "a" / "b").mkdir(parents=True)
    (trash / "a" / "b" / "f").write_text("x")
    (trash / "link").symlink_to(victim)
    (trash / "plain").write_text("y")
    assert safefile.empty_trash(trash) == 3
    assert list(trash.iterdir()) == [] and (victim / "keep").read_text() == "k"
    assert safefile.empty_trash(trash) == 0
    assert safefile.empty_trash(work / "never-made") == 0


def test_empty_trash_leaves_a_symlinked_trash_alone(work, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("k")
    (work / "trash").symlink_to(victim)
    assert safefile.empty_trash(work / "trash") == 0
    assert (victim / "keep").read_text() == "k"


def test_every_run_starts_by_emptying_the_trash_and_says_how_many(work, tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    state.mkdir()
    (work / "trash" / "x").mkdir(parents=True)
    (work / "trash" / "y").write_text("y")
    monkeypatch.setattr(runner, "REAL_OPS", FakeOps(state))
    monkeypatch.setattr(runner.manifest, "load", lambda **kw: [])
    monkeypatch.setattr(runner, "select", lambda designs, only, candidates: ["tt99/tt_um_a"])
    assert main(["run", "build", "--jobs", "1"]) == 0
    assert "removed 2 entries from" in capsys.readouterr().out
    assert list((work / "trash").iterdir()) == []


# ---- C3: a FIFO planted at a log name must not hang the host


def test_a_fifo_at_a_log_name_does_not_hang_open_log(tmp_path):
    log = tmp_path / "x.log"
    os.mkfifo(log)
    result: list = []

    def attempt():
        with safefile.open_log(log) as f:
            f.write(b"hello\n")
        result.append("done")

    t = threading.Thread(target=attempt, daemon=True)
    t.start()
    t.join(10)
    assert result == ["done"], "open_log blocked on a FIFO"
    assert log.is_file() and log.read_text() == "hello\n"


def test_a_fifo_with_a_reader_is_replaced_too_and_the_log_is_blocking(tmp_path):
    log = tmp_path / "x.log"
    os.mkfifo(log)
    reader = os.open(log, os.O_RDONLY | os.O_NONBLOCK)
    try:
        with safefile.open_log(log) as f:
            assert os.get_blocking(f.fileno())
            f.write(b"a")
    finally:
        os.close(reader)
    assert log.is_file()


# ---- C4: a symlinked run/<id> is that design's error, not the end of the run


def test_a_symlinked_run_directory_is_one_designs_error_and_the_run_goes_on(work, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    (work / "run" / "tt99").mkdir(parents=True)
    (work / "run" / "tt99" / "tt_um_b").symlink_to(victim)
    ids = ["tt99/tt_um_a", "tt99/tt_um_b", "tt99/tt_um_c"]
    s = runner.run("build", ids, jobs=2, ops=FakeOps(state))
    assert s.counts["build"] == {"ok": 2, "error": 1}
    log = s.log.read_text()
    assert "tt99/tt_um_b" in log and "symbolic link" in log
    assert list(victim.iterdir()) == []  # nothing was written through it
    assert (paths.design_dir("run", "tt99/tt_um_a") / "outcome.json").exists()
    assert (paths.design_dir("run", "tt99/tt_um_c") / "outcome.json").exists()
    assert s.total == 3 and s.finished == 3


# ---- C5: sv2v's output is capped and goes through the capped log


def only_sv2v_works(monkeypatch):
    monkeypatch.setenv("FAKE_YOSYS_FAIL", "1")  # fails unless the script reads sv2v.v


def test_sv2v_log_is_capped_and_its_output_is_read_back_through_safefile(
    toolchain, rec, tmp_path, monkeypatch
):
    noisy = SV2V.replace(
        'echo "$@" > "$FAKE_REC/sv2v.args"',
        'echo "$@" > "$FAKE_REC/sv2v.args"\nyes noise | head -c 5000000 >&2',
    )
    fake_tool(toolchain, "sv2v", noisy)
    only_sv2v_works(monkeypatch)
    d, r = run(tmp_path, names=("a.sv",))
    assert r.status == "ok" and r.sv2v is True, r.reason
    out = paths.design_dir("build", d.id)
    assert (out / "sv2v.log").stat().st_size < 1_300_000  # first 1 MiB, a marker, the tail
    assert (out / "sv2v.v").read_text().startswith("module converted")


def test_sv2v_output_over_64_mib_is_a_synth_fail(toolchain, rec, tmp_path, monkeypatch):
    big = SV2V.replace(
        'echo "module converted; endmodule"',
        'for a in "$@"; do case "$a" in --write=*) out=${a#--write=};; esac; done\n'
        'head -c 67108865 /dev/zero | tr "\\\\0" "a" > "$out"',
    )
    fake_tool(toolchain, "sv2v", big)
    only_sv2v_works(monkeypatch)
    _d, r = run(tmp_path, names=("a.sv",))
    assert r.status == "synth-fail" and "sv2v" in r.reason and "too large" in r.reason, r.reason


def test_sv2v_writing_a_symlink_as_its_output_is_refused(toolchain, rec, tmp_path, monkeypatch):
    evil = SV2V.replace(
        'echo "module converted; endmodule" > "$out"',
        'rm -f "$out"; ln -s /etc/passwd "$out"',
    )
    fake_tool(toolchain, "sv2v", evil)
    only_sv2v_works(monkeypatch)
    _d, r = run(tmp_path, names=("a.sv",))
    assert r.status == "synth-fail" and "plain file" in r.reason, r.reason
