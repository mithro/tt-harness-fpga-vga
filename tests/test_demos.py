# SPDX-License-Identifier: Apache-2.0
"""tthfv demos: the export of built bitstreams as a demo index, from records written into a scratch WORK."""

import hashlib
import json
from pathlib import Path

import pytest
from test_results import design

from tthfv import demos, manifest, paths
from tthfv.cli import main
from tthfv.errors import UserError

PREAMBLE = bytes.fromhex("7eaa997e")
BIN = b"\xff\x00" * 8 + PREAMBLE + b"rest-of-the-bitstream"
COMMIT = "c" * 40
REPO = "https://github.com/o/r.git"


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    monkeypatch.setattr(demos, "_tthfv_commit", lambda: "t" * 40)
    monkeypatch.setattr(demos, "_videos_repo_commit", lambda: "v" * 40)
    return tmp_path / "work"


def put_build(work: Path, d, data: bytes | None = BIN, **kw) -> dict:
    out = work / "build" / d.id
    out.mkdir(parents=True, exist_ok=True)
    rec = {
        "id": d.id, "status": "ok", "bitstream": "design.bin",
        "bitstream_sha256": hashlib.sha256(data or b"").hexdigest(), "clock_hz": 25_175_000,
        "fmax_hz": 31_250_000, "top_module_used": d.top_module, "commit": d.commit,
    }  # fmt: skip
    rec.update(kw)
    (out / "build.json").write_text(json.dumps(rec))
    if data is not None:
        (out / "design.bin").write_bytes(data)
    return rec


def put_ref(work: Path, d, profile: str = "ideal", **kw) -> None:
    out = work / "reference" / d.id / profile
    out.mkdir(parents=True, exist_ok=True)
    rec = {
        "schema": 3, "id": d.id, "profile": profile, "mode": "640x480@60", "frames_complete": 5,
        "frames": [{"index": 0, "hash": "0123456789abcdef"}], "ui_in_requested": 0, "uio_in_requested": 0,
    }  # fmt: skip
    rec.update(kw)
    (out / "reference.json").write_text(json.dumps(rec))


def full(work: Path, id: str = "tt01/tt_um_a", build: dict | None = None, ref: dict | None = None, **dkw):
    d = design(id, repo=REPO, commit=COMMIT, **dkw)
    put_build(work, d, **(build or {}))
    put_ref(work, d, **(ref or {}))
    return d


def export(work, designs, tmp_path, **kw):
    out = tmp_path / "out"
    summary = demos.export(designs, work=work, out=out, **kw)
    return out, summary


def index(out: Path) -> dict:
    return json.loads((out / "index.json").read_text())


def test_demo_name_check_values():
    assert demos.demo_name("tt05/tt_um_dinogame") == "tt05_tt_um_dinogame"
    assert demos.demo_name("tt08/tt_um_MichaelBell_mandelbrot") == "tt08_tt_um_michaelbell_mandelbrot"


def test_long_names_are_shortened_with_a_hash_of_the_id():
    upper, lower = "a/" + "X" * 50, "a/" + "x" * 50
    n_up, n_low = demos.demo_name(upper), demos.demo_name(lower)
    assert len(n_up) == len(n_low) == 40
    assert n_up != n_low
    assert n_up == "a_" + "x" * 31 + "_" + hashlib.sha256(upper.encode()).hexdigest()[:6]


def test_a_name_collision_fails_and_names_both_ids(work, tmp_path):
    a, b = full(work, "tt01/tt_um_Ab"), full(work, "tt01/tt_um_ab")
    with pytest.raises(UserError) as e:
        export(work, [a, b], tmp_path)
    assert "tt01/tt_um_Ab" in str(e.value) and "tt01/tt_um_ab" in str(e.value)


def test_an_ok_design_has_every_field_in_order_and_the_bin_is_copied(work, tmp_path):
    d = full(
        work, pinout={"ui": ["a", 5], "uo": ["x"] * 9, "uio": []}, title="A title", author="Ann",
        ref={"ui_in_requested": 0, "uio_in_requested": 3},
    )  # fmt: skip
    out, summary = export(work, [d], tmp_path)
    assert (out / "tt01_tt_um_a.bin").read_bytes() == BIN
    idx = index(out)
    assert list(idx) == [
        "schema", "generated_by", "tthfv_commit", "videos_repo_commit", "profile", "demos", "skipped",
    ]  # fmt: skip
    assert idx["schema"] == 1 and idx["generated_by"] == "tthfv demos"
    assert idx["tthfv_commit"] == "t" * 40 and idx["videos_repo_commit"] == "v" * 40
    assert idx["profile"] == "ideal" and idx["skipped"] == []
    (e,) = idx["demos"]
    assert list(e) == [
        "name", "title", "author", "description", "docs_url", "repo_url", "clock_hz", "pinout", "id",
        "shuttle", "macro", "top_module", "ui_in", "uio_in", "source_repo", "source_commit", "bin_sha256",
        "bin_bytes", "status", "warnings",
    ]  # fmt: skip
    assert e["name"] == "tt01_tt_um_a" and e["title"] == "A title" and e["author"] == "Ann"
    assert e["description"] == "tt01 tt_um_a. Not yet validated on hardware."
    assert e["docs_url"] == f"https://github.com/o/r/blob/{COMMIT}/docs/info.md"
    assert e["repo_url"] == f"https://github.com/o/r/tree/{COMMIT}"
    assert e["clock_hz"] == 25_175_000
    assert e["pinout"] == {"ui": ["a", "5", "", "", "", "", "", ""], "uo": ["x"] * 8, "uio": [""] * 8}
    assert (e["id"], e["shuttle"], e["macro"]) == ("tt01/tt_um_a", "tt01", "tt_um_a")
    assert e["top_module"] == "tt_um_a"
    assert (e["ui_in"], e["uio_in"]) == (0, 3)
    assert (e["source_repo"], e["source_commit"]) == (REPO, COMMIT)
    assert e["bin_sha256"] == hashlib.sha256(BIN).hexdigest() and e["bin_bytes"] == len(BIN)
    assert e["status"] == {
        "build": "ok", "fmax_hz": 31_250_000, "clock_declared": True, "reference": "ok",
        "reference_profile": "ideal", "frames": 5, "mode": "640x480@60",
        "first_frame_hash": "0123456789abcdef", "hardware": None,
    }  # fmt: skip
    assert list(e["status"]) == [
        "build", "fmax_hz", "clock_declared", "reference", "reference_profile", "frames", "mode",
        "first_frame_hash", "hardware",
    ]  # fmt: skip
    assert e["warnings"] == []
    assert summary.exported == 1 and summary.timing_not_met == 0


def test_an_empty_title_falls_back_to_the_macro_and_subdir_is_in_the_urls(work, tmp_path):
    d = full(work, title="", subdir="/sub/dir/")
    (e,) = index(export(work, [d], tmp_path)[0])["demos"]
    assert e["title"] == "tt_um_a"
    assert e["docs_url"] == f"https://github.com/o/r/blob/{COMMIT}/sub/dir/docs/info.md"
    assert e["repo_url"] == f"https://github.com/o/r/tree/{COMMIT}/sub/dir"


def test_a_timing_fail_design_carries_the_warning(work, tmp_path):
    d = full(work, title="T", build={"status": "timing-fail", "fmax_hz": 20_563_015})
    out, summary = export(work, [d], tmp_path)
    (e,) = index(out)["demos"]
    warning = (
        "WARNING: timing not met: place-and-route estimates 20.563 MHz, the design's clock is 25.175 MHz;"
        " not validated on hardware; it may glitch or fail on some boards."
    )
    assert e["title"] == "T (timing not met)"
    assert e["description"] == f"tt01 tt_um_a. {warning}"
    assert "Not yet validated" not in e["description"]
    assert e["warnings"] == [warning]
    assert e["status"]["build"] == "timing-fail"
    assert summary.timing_not_met == 1


def test_an_undeclared_clock_is_a_warning(work, tmp_path):
    d = full(work, clock_declared=False)
    (e,) = index(export(work, [d], tmp_path)[0])["demos"]
    assert e["warnings"] == ["clock not declared by the project; 25.175 MHz assumed"]
    assert e["status"]["clock_declared"] is False


def test_nonzero_ui_in_is_in_the_description_and_an_integer(work, tmp_path):
    d = full(work, ref={"ui_in_requested": 0x0A})
    (e,) = index(export(work, [d], tmp_path)[0])["demos"]
    assert e["ui_in"] == 10
    assert e["description"] == "tt01 tt_um_a. Not yet validated on hardware. Set ui_in to 0x0A."


def skipped(work, tmp_path, d, **kw):
    out, _ = export(work, [d], tmp_path, **kw)
    idx = index(out)
    assert idx["demos"] == []
    (s,) = idx["skipped"]
    assert s["id"] == d.id and list(s) == ["id", "reason"]
    assert not list(out.glob("*.bin"))
    return s["reason"]


def bare(id: str = "tt01/tt_um_a"):
    return design(id, repo=REPO, commit=COMMIT)


def test_skip_missing_build(work, tmp_path):
    assert "no build record" in skipped(work, tmp_path, bare())


def test_skip_a_failed_build_or_no_bitstream_named(work, tmp_path):
    d = bare()
    put_build(work, d, status="pnr-fail")
    put_ref(work, d)
    assert "pnr-fail" in skipped(work, tmp_path, d)
    put_build(work, d, bitstream=None)
    assert "names no bitstream" in skipped(work, tmp_path, d)


def test_skip_bitstream_missing(work, tmp_path):
    d = bare()
    put_build(work, d, data=None, bitstream_sha256="0" * 64)
    put_ref(work, d)
    assert "bitstream missing" in skipped(work, tmp_path, d)


def test_skip_wrong_sha(work, tmp_path):
    d = bare()
    put_build(work, d, bitstream_sha256="0" * 64)
    put_ref(work, d)
    assert "sha256" in skipped(work, tmp_path, d)


def test_skip_over_262144_bytes_but_exactly_262144_is_fine(work, tmp_path):
    d = bare()
    put_ref(work, d)
    put_build(work, d, data=BIN + b"\0" * (262144 + 1 - len(BIN)))
    assert "262144" in skipped(work, tmp_path, d)
    put_build(work, d, data=BIN + b"\0" * (262144 - len(BIN)))
    assert export(work, [d], tmp_path)[1].exported == 1


def test_skip_missing_preamble_and_it_must_be_in_the_first_68_bytes(work, tmp_path):
    d = bare()
    put_ref(work, d)
    put_build(work, d, data=b"\0" * 100)
    assert "preamble" in skipped(work, tmp_path, d)
    put_build(work, d, data=b"\0" * 65 + PREAMBLE)  # bytes 65 to 68 inclusive of the sync word end at 69
    assert "preamble" in skipped(work, tmp_path, d)
    put_build(work, d, data=b"\0" * 64 + PREAMBLE)  # ends at byte 68
    assert export(work, [d], tmp_path)[1].exported == 1


def test_skip_stale_commit(work, tmp_path):
    d = bare()
    put_build(work, d, commit="d" * 40)
    put_ref(work, d)
    assert "stale" in skipped(work, tmp_path, d)


def test_skip_no_reference(work, tmp_path):
    d = bare()
    put_build(work, d)
    assert "no reference" in skipped(work, tmp_path, d)


def test_skip_reference_with_no_frames(work, tmp_path):
    d = full(work, ref={"frames_complete": 0, "frames": []})
    assert "no frames" in skipped(work, tmp_path, d)


def test_skip_reference_fail_json(work, tmp_path):
    d = bare()
    put_build(work, d)
    r = work / "reference" / d.id / "ideal"
    r.mkdir(parents=True)
    (r / "reference-fail.json").write_text(json.dumps({"status": "sim-fail", "reason": "boom"}))
    assert "reference failed" in skipped(work, tmp_path, d)


def test_skip_reference_of_the_wrong_schema(work, tmp_path):
    d = full(work, ref={"schema": 2})
    assert "schema" in skipped(work, tmp_path, d)


def test_a_damaged_record_is_a_skip_not_a_crash(work, tmp_path):
    d = full(work)
    (work / "build" / d.id / "build.json").write_text("{truncated")
    assert "build.json" in skipped(work, tmp_path, d)


def test_the_profile_selects_the_reference_directory(work, tmp_path):
    d = bare()
    put_build(work, d)
    put_ref(work, d, "boardx", frames_complete=7)
    out, _ = export(work, [d], tmp_path, profile="boardx")
    (e,) = index(out)["demos"]
    assert index(out)["profile"] == "boardx"
    assert e["status"]["frames"] == 7 and e["status"]["reference_profile"] == "boardx"
    assert "no reference" in skipped(work, tmp_path, d)  # the default profile has none


def test_a_collision_leaves_the_existing_export_untouched(work, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "index.json").write_text("old")
    (out / "old.bin").write_bytes(b"old")
    a, b = full(work, "tt01/tt_um_Ab"), full(work, "tt01/tt_um_ab")
    with pytest.raises(UserError):
        demos.export([a, b], work=work, out=out)
    assert sorted(p.name for p in out.iterdir()) == ["index.json", "old.bin"]
    assert (out / "index.json").read_text() == "old" and (out / "old.bin").read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("out")) == ["out"]


def test_a_new_export_replaces_the_old_one_whole(work, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "stale.bin").write_bytes(b"old")
    demos.export([full(work)], work=work, out=out)
    assert sorted(p.name for p in out.iterdir()) == ["index.json", "tt01_tt_um_a.bin"]
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("out")) == ["out"]


def test_demos_are_sorted_by_name(work, tmp_path):
    ds = [full(work, "tt02/tt_um_z"), full(work, "tt01/tt_um_b"), full(work, "tt01/tt_um_a")]
    names = [e["name"] for e in index(export(work, ds, tmp_path)[0])["demos"]]
    assert names == ["tt01_tt_um_a", "tt01_tt_um_b", "tt02_tt_um_z"]


def test_index_is_deterministic_and_formatted(work, tmp_path):
    ds = [full(work, "tt01/tt_um_b"), full(work, "tt01/tt_um_a"), bare("tt01/tt_um_c")]
    first = (export(work, ds, tmp_path)[0] / "index.json").read_bytes()
    second = (export(work, ds, tmp_path)[0] / "index.json").read_bytes()
    assert first == second
    assert first.endswith(b"}\n") and b'\n  "schema": 1,' in first


def _cli(monkeypatch, ds, argv):
    monkeypatch.setattr(manifest, "load", lambda *a, **k: ds)
    return main(["demos", *argv])


def test_cli_default_out_is_demos_in_the_work_directory_and_summary(work, monkeypatch, capsys):
    ds = [
        full(work, "tt01/tt_um_a"),
        full(work, "tt01/tt_um_t", build={"status": "timing-fail"}),
        bare("tt01/tt_um_n"),
        bare("tt01/tt_um_m"),
    ]
    assert _cli(monkeypatch, ds, []) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"exported 2 designs (1 with timing not met), skipped 2 -> {work / 'demos'}"
    assert lines[1:] == ["  2 x no build record"]
    assert (work / "demos" / "index.json").is_file()


def test_cli_only_restricts_and_an_unknown_id_is_an_error(work, monkeypatch, capsys, tmp_path):
    ds = [full(work, "tt01/tt_um_a"), full(work, "tt01/tt_um_b")]
    out = tmp_path / "o"
    assert _cli(monkeypatch, ds, ["--out", str(out), "--only", "tt01/tt_um_b"]) == 0
    idx = index(out)
    assert [e["id"] for e in idx["demos"]] == ["tt01/tt_um_b"] and idx["skipped"] == []
    assert _cli(monkeypatch, ds, ["--out", str(out), "--only", "tt01/tt_um_zzz"]) == 1
    assert "tt_um_zzz" in capsys.readouterr().err
    assert [e["id"] for e in index(out)["demos"]] == ["tt01/tt_um_b"]


def test_cli_collision_exits_1_with_a_message(work, monkeypatch, capsys, tmp_path):
    ds = [full(work, "tt01/tt_um_Ab"), full(work, "tt01/tt_um_ab")]
    assert _cli(monkeypatch, ds, ["--out", str(tmp_path / "o")]) == 1
    assert "tt_um_Ab" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()
