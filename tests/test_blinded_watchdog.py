# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: F811 - pytest fixtures imported from another test module
"""Ways to blind a disk watchdog that only walks the scratch directory, with the real jail and scope: a file unlinked
while it is still open and written, and a tree that is rebuilt under the walk. Small caps, so that at most a few
hundred MiB are written."""

import time

from test_limits import box, jail, needs_limits, no_units  # noqa: F401

from tthfv import proc, safefile

MiB = 1 << 20


def chunks_written(rw) -> int:
    try:
        return int((rw / "count").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0


@needs_limits
def test_a_file_unlinked_while_open_is_caught_by_the_fall_in_free_space(jail, monkeypatch, tmp_path):
    monkeypatch.setattr(safefile, "MAX_SCRATCH", 100 * MiB)
    monkeypatch.setattr(proc._Watchdog, "FREE_MARGIN", 100 * MiB)
    _ro, rw = jail
    script = (
        f"exec 3> {rw}/hidden; rm {rw}/hidden; n=0; "
        f"while :; do head -c {20 * MiB} /dev/zero >&3; n=$((n+1)); echo $n > {rw}/count; sleep 0.2; done"
    )
    started = time.monotonic()
    r = proc.run_logged(
        ["sh", "-c", script], 120, tmp_path / "w.log", cwd=rw, sandbox=box(jail),
        scratch_check=lambda: safefile.scratch_exceeded(rw), scratch_dir=rw,
    )  # fmt: skip
    assert safefile.scratch_exceeded(rw) == "", (
        "the walk can see nothing: the file is gone from the directory"
    )
    assert r.limited == "disk" and not r.timed_out, r
    assert "free space fell by" in r.detail and "cannot see" in r.detail, r.detail
    written = chunks_written(rw) * 20
    # cap + margin = 200 MiB; the rest is what a writer of about 100 MiB/s gets in the ticks before the kill
    assert written <= 200 + 600, f"{written} MiB were written before the kill"
    assert time.monotonic() - started < 90 and no_units()


@needs_limits
def test_a_tree_rebuilt_under_the_walk_does_not_blind_it(jail, monkeypatch, tmp_path):
    monkeypatch.setattr(safefile, "MAX_SCRATCH", 100 * MiB)
    _ro, rw = jail
    churn = f"( while :; do for i in $(seq 300); do mkdir -p {rw}/c/a$i/b/c; done; rm -rf {rw}/c; done ) & "
    write = f"while :; do head -c {20 * MiB} /dev/zero >> {rw}/big; n=$((n+1)); echo $n > {rw}/count; sleep 0.2; done"
    r = proc.run_logged(
        ["sh", "-c", churn + write], 120, tmp_path / "w.log", cwd=rw, sandbox=box(jail),
        scratch_check=lambda: safefile.scratch_exceeded(rw), scratch_dir=rw,
    )  # fmt: skip
    assert r.limited == "disk" and not r.timed_out, r
    big = (rw / "big").stat().st_size // MiB
    assert big <= 100 + 700, f"{big} MiB were written before the kill (cap 100 MiB)"
    assert no_units()
