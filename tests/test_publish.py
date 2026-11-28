# SPDX-License-Identifier: Apache-2.0
"""tthfv publish: the FPGA library (fpga-library.json, schema 1) written into a scratch archive directory."""

import dataclasses
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_results import design

from tthfv import build as build_stage
from tthfv import frames, manifest, paths, pdkcells, publish, reference
from tthfv.cli import main
from tthfv.errors import UserError
from tthfv.fetch import FetchResult

PREAMBLE = bytes.fromhex("7eaa997e")
BIN = b"\xff\x00" * 8 + PREAMBLE + b"rest-of-the-bitstream"
COMMIT = "c" * 40
REPO = "https://github.com/o/r.git"
PROFILE = "boards/fpga-4.json"
TTHFV = "a" * 40
VIDEOS = "b" * 40
NOW = datetime(2026, 11, 26, 3, 0, 0, tzinfo=UTC)


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    monkeypatch.setattr(publish, "_tthfv_commit", lambda: TTHFV)
    monkeypatch.setattr(publish, "_videos_repo_commit", lambda: VIDEOS)
    monkeypatch.setattr(publish, "_now", lambda: NOW)
    monkeypatch.setattr(publish, "_tree_is_dirty", lambda: False)
    hw = tmp_path / "hw-default.json"
    hw.write_text(json.dumps({"schema": 1, "records": {}}))
    monkeypatch.setattr(publish, "HARDWARE_RECORD", hw)
    monkeypatch.setattr(publish, "LIVE_ARCHIVE", tmp_path / "live-stand-in")
    return tmp_path / "work"


@pytest.fixture
def archive(tmp_path):
    a = tmp_path / "archive"
    a.mkdir()
    return a


def checker(tmp_path: Path, code: int = 0, record: Path | None = None) -> str:
    """A command line for a fake `tt-vga fpga-library-check`: exits with `code`, optionally records its argv."""
    script = tmp_path / f"check{code}.py"
    script.write_text(
        "import json, sys\n"
        + (f"open({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n" if record else "")
        + "print('checker says hello')\n"
        + f"sys.exit({code})\n"
    )
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"


SOURCES = "5" * 64
SYNTH = ("src/a.v", "src/b.sv")
TOOLS = {"yosys": "Yosys test"}


def put_fetch(work: Path, d, **kw) -> None:
    """work/src/<id>/fetch.json with the fields the build and reference stages take from it."""
    out = work / "src" / d.id
    out.mkdir(parents=True, exist_ok=True)
    rec = {"version": 1, "repo": d.repo, "commit": d.commit, "synth_sources": list(SYNTH),
           "top_module_used": d.top_module, "sources_sha256": SOURCES}  # fmt: skip
    rec.update(kw)
    (out / "fetch.json").write_text(json.dumps(rec))


def inputs_digest(d, fetch: dict, tools: dict, limits, plan=None, reset_vlog: bool = False) -> str:
    """The build stage's own digest of what a build depends on, from a fetch record, as a build would have it."""
    src = Path("/fetch-json")
    fetched = FetchResult(
        status="ok", src_dir=src, sources=(), include_dirs=(), log=src / "log",
        synth_sources=tuple(src / s for s in fetch["synth_sources"]), top_module_used=fetch["top_module_used"],
        sources_sha256=fetch["sources_sha256"],
    )  # fmt: skip
    wrapper = (
        (build_stage.HARNESS / "tt_fpga_top.v")
        .read_text()
        .replace(build_stage.PLACEHOLDER, fetched.top_module_used)
    )
    return build_stage._inputs_digest(
        d, fetched, wrapper, tools, build_stage.DEFAULT_MAX_ATTEMPTS, limits, plan, reset_vlog
    )


def put_build(work: Path, d, data: bytes | None = BIN, **kw) -> dict:
    """A build record with the fields the build stage writes, and the fetch record it was made from."""
    out = work / "build" / d.id
    out.mkdir(parents=True, exist_ok=True)
    put_fetch(work, d)
    fetch = json.loads((work / "src" / d.id / "fetch.json").read_text())
    rec = {
        "id": d.id, "status": "ok", "bitstream": "design.bin",
        "bitstream_sha256": hashlib.sha256(data or b"").hexdigest(), "clock_hz": 25_175_000,
        "fmax_hz": 31_250_000, "top_module_used": d.top_module, "commit": d.commit,
        "sources_sha256": SOURCES, "tools": TOOLS, "limits": None,
        "inputs_sha256": inputs_digest(
            d, fetch, TOOLS, None, pdkcells.plan_named(kw.get("pdk_cells") or []) if kw.get("pdk_cells") else None,
            kw.get("reset_vlog") is True,
        ),
    }  # fmt: skip
    rec.update(kw)
    (out / "build.json").write_text(json.dumps(rec))
    if data is not None:
        (out / "design.bin").write_bytes(data)
    return rec


def profile_object(name: str, data: dict | None = None):
    """The profile a reference of that directory name was made under: ideal, or the real fpga-4 census."""
    if name == "ideal":
        return reference.IDEAL
    return reference.Profile(name, data if data is not None else reference.load_profile(PROFILE).data)


def put_ref(work: Path, d, profile: str = "ideal", made_under=None, **kw) -> None:
    """A reference record with the key the reference stage writes (commit, inputs, ties, profile)."""
    out = work / "reference" / d.id / profile
    out.mkdir(parents=True, exist_ok=True)
    complete = kw.get("frames_complete", 5)
    prof = made_under or profile_object(profile)
    stim = reference.stimulus_of(d, prof)
    rec = {
        "schema": 3, "id": d.id, "profile": profile, "mode": "640x480@60", "frames_complete": complete,
        "frames": [{"index": i, "hash": f"{i:016x}"} for i in range(complete)],
        "ui_in_requested": d.ui_in, "ui_in_mask_used": 0xFF, "uio_in_requested": d.uio_in,
        "clock_hz": 25_175_000,
        "key": {
            "repo": d.repo, "commit": d.commit, "sources_sha256": SOURCES,
            "compilation_units": list(SYNTH), "top_module_used": d.top_module,
            "ui_in": stim.ui_req, "uio_in": stim.uio_requested, "ties": stim.ties,
            "profile": {"name": prof.name, "data": prof.data},
            "clocks": reference.DEFAULT_CLOCKS,
            "verilator_flags": list(d.override.get("verilator_flags") or []),
            "base_flags": list(reference._BASE_FLAGS), "verilator": "Verilator test 1.0",
            "testbench_sha256": hashlib.sha256(reference.TB.read_bytes()).hexdigest(),
            "stimulus_version": reference.STIMULUS_VERSION, "cutter_version": frames.CUTTER_VERSION,
        },
        "uio_oe_any": 0, "uio_oe_ever": 0,
    }  # fmt: skip
    rec.update(kw)
    (out / "reference.json").write_text(json.dumps(rec))


def full(work: Path, id: str = "tt05/tt_um_a", build: dict | None = None, ref: dict | None = None,
         ideal: dict | None = None, **dkw):  # fmt: skip
    dkw.setdefault("ui_in", (ref or {}).get("ui_in_requested", 0))
    d = design(id, **{"repo": REPO, "commit": COMMIT, **dkw})
    put_build(work, d, **(build or {}))
    put_ref(work, d, **(ideal or {}))
    put_ref(work, d, "fpga-4", **(ref or {}))
    return d


def run(work, archive, tmp_path, designs, **kw):
    if "check_cmd" not in kw:
        kw["check_cmd"] = checker(tmp_path)
    return publish.run(designs, archive=archive, work=work, profile=PROFILE, **kw)


def doc(archive: Path) -> dict:
    return json.loads((archive / "fpga-library.json").read_text())


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# ---- the id rule and the reserved names


@pytest.mark.parametrize(
    ("shuttle", "macro", "want"),
    [
        ("tt05", "tt_um_flappy_vga_cutout1", "tt05_flappy_vga_cutout1"),
        ("tt07", "tt_um_MichaelBell_rle_vga", "tt07_michaelbell_rle_vga"),
        ("ttsky25a", "tt_um_vga_example", "ttsky25a_vga_example"),
        ("ttihp25a", "tt_um_yuri_panchul_sea_battle_vga_game", "ttihp25a_yuri_panchul_sea_battl_09a2562b"),
        ("ttsky25a", "tt_um_rebeccargb_vga_timing_experiments", "ttsky25a_rebeccargb_vga_timing__4638a050"),
    ],
)
def test_library_id_vectors_from_the_spec(shuttle, macro, want):
    assert publish.library_id(shuttle, macro) == want
    assert len(want) <= 40


def test_library_id_hashes_the_original_case_string():
    shuttle, macro = "ttihp25a", "tt_um_MixedCase_" + "Vga" * 20
    want_hash = hashlib.sha256(f"{shuttle}/{macro}".encode()).hexdigest()[:8]
    lowered_hash = hashlib.sha256(f"{shuttle}/{macro}".lower().encode()).hexdigest()[:8]
    assert want_hash != lowered_hash
    stem = f"{shuttle}_{macro.removeprefix('tt_um_')}".lower()
    assert publish.library_id(shuttle, macro) == stem[:31] + "_" + want_hash
    assert len(publish.library_id(shuttle, macro)) == 40


def test_library_id_needs_the_tt_um_prefix_and_maps_odd_characters():
    with pytest.raises(ValueError):
        publish.library_id("tt05", "vga_thing")
    assert publish.library_id("tt05", "tt_um_a-b.c") == "tt05_a_b_c"


@pytest.mark.parametrize(
    "name", ["tt_um_counter_7seg", "tt_um_factory_test", "tt_um_pwm_breathe", "tt_um_uart_hello",
             "tt_um_vga_pattern", "idle_display"],
)  # fmt: skip
def test_reserved_names_are_refused(name):
    with pytest.raises(UserError, match=name):
        publish.check_reserved(name)


def test_other_names_pass_the_reserved_check():
    publish.check_reserved("tt05_a")
    assert publish.DEMO_NAMES_COMMIT == "da7d10d"


def test_a_reserved_id_fails_the_whole_run_and_writes_nothing(work, archive, tmp_path, monkeypatch):
    d = full(work)
    monkeypatch.setattr(publish, "library_id", lambda s, m: "idle_display")
    with pytest.raises(UserError, match="idle_display"):
        run(work, archive, tmp_path, [d])
    assert list(archive.iterdir()) == []


def test_a_duplicate_id_fails_the_run_naming_both_designs(work, archive, tmp_path):
    a, b = full(work, "tt05/tt_um_Ab"), full(work, "tt05/tt_um_ab")
    with pytest.raises(UserError) as e:
        run(work, archive, tmp_path, [a, b])
    assert "tt05/tt_um_Ab" in str(e.value) and "tt05/tt_um_ab" in str(e.value)
    assert list(archive.iterdir()) == []


# ---- what is published, what is skipped


def reason_of(work, archive, tmp_path, d, **kw):
    s = run(work, archive, tmp_path, [d], **kw)
    assert s.published == 0
    (skip,) = s.skipped
    assert skip["id"] == d.id and list(skip) == ["id", "reason"]
    return skip["reason"]


def bare(id: str = "tt05/tt_um_a", **kw):
    return design(id, repo=REPO, commit=COMMIT, **kw)


def test_skip_calibration_design(work, archive, tmp_path):
    d = full(work, "calibration/tt_um_a")
    assert reason_of(work, archive, tmp_path, d) == "calibration design"


def test_skip_qspi_design(work, archive, tmp_path):
    d = full(work, qspi="uio[1]")
    assert reason_of(work, archive, tmp_path, d) == "needs a QSPI Pmod"


def test_skip_missing_build(work, archive, tmp_path):
    assert "no build record" in reason_of(work, archive, tmp_path, bare())


def test_skip_failed_build_status_or_no_bitstream_named(work, archive, tmp_path):
    d = bare()
    put_build(work, d, status="pnr-fail")
    assert "pnr-fail" in reason_of(work, archive, tmp_path, d)
    put_build(work, d, bitstream=None)
    assert "names no bitstream" in reason_of(work, archive, tmp_path, d)


def test_skip_bitstream_missing_wrong_sha_too_big_no_preamble(work, archive, tmp_path):
    d = bare()
    put_build(work, d, data=None, bitstream_sha256="0" * 64)
    assert "bitstream missing" in reason_of(work, archive, tmp_path, d)
    put_build(work, d, bitstream_sha256="0" * 64)
    assert "sha256" in reason_of(work, archive, tmp_path, d)
    put_build(work, d, data=BIN + b"\0" * (262144 + 1 - len(BIN)))
    assert "262144" in reason_of(work, archive, tmp_path, d)
    put_build(work, d, data=b"\0" * 100)
    assert "preamble" in reason_of(work, archive, tmp_path, d)
    put_build(work, d, data=b"\0" * 65 + PREAMBLE)
    assert "preamble" in reason_of(work, archive, tmp_path, d)


def test_bitstream_limits_are_inclusive(work, archive, tmp_path):
    d = full(work)
    put_build(work, d, data=b"\0" * 64 + PREAMBLE + b"\0" * (262144 - 68))
    assert run(work, archive, tmp_path, [d]).published == 1


def test_skip_stale_build(work, archive, tmp_path):
    d = full(work, build={"commit": "d" * 40})
    assert "stale build" in reason_of(work, archive, tmp_path, d)


@pytest.mark.parametrize("fmax", [None, 0, -5, 1_000_000_001, 3.5, True, "12", float("nan")])
def test_skip_when_fmax_is_not_an_int_in_range(work, archive, tmp_path, fmax):
    d = full(work, build={"fmax_hz": fmax})
    assert reason_of(work, archive, tmp_path, d) == "no fmax"


@pytest.mark.parametrize("clock", [None, 0, 200_000_001, True, 25.5])
def test_skip_when_clock_is_out_of_range(work, archive, tmp_path, clock):
    d = full(work, build={"clock_hz": clock})
    assert "clock_hz" in reason_of(work, archive, tmp_path, d)


def test_skip_no_ideal_reference_and_failed_one(work, archive, tmp_path):
    d = bare()
    put_build(work, d)
    put_ref(work, d, "fpga-4")
    assert reason_of(work, archive, tmp_path, d) == "no reference"
    r = work / "reference" / d.id / "ideal"
    r.mkdir(parents=True)
    (r / "reference-fail.json").write_text(json.dumps({"status": "sim-fail"}))
    assert "reference failed" in reason_of(work, archive, tmp_path, d)


def test_skip_no_reference_under_the_board_profile(work, archive, tmp_path):
    d = bare()
    put_build(work, d)
    put_ref(work, d)
    assert reason_of(work, archive, tmp_path, d) == "no reference under profile fpga-4"


@pytest.mark.parametrize("which", ["ideal", "ref"])
def test_skip_references_that_are_stale_inconsistent_or_empty(work, archive, tmp_path, which):
    def make(**kw):
        return full(work, **{which: kw})

    assert "stale reference" in reason_of(work, archive, tmp_path, make(key={"commit": "e" * 40}))
    assert "stale reference" in reason_of(work, archive, tmp_path, make(key=None))
    assert "stale reference" in reason_of(work, archive, tmp_path, make(clock_hz=50_000_000))
    assert "inconsistent" in reason_of(work, archive, tmp_path, make(frames_complete=3, frames=[]))
    assert "inconsistent" in reason_of(
        work, archive, tmp_path, make(frames_complete=0, frames=[{"index": 0, "hash": "ab"}])
    )
    assert "no frames" in reason_of(work, archive, tmp_path, make(frames_complete=0))
    assert "schema" in reason_of(work, archive, tmp_path, make(schema=2))


def test_a_manifest_ui_in_changed_after_the_reference_was_made_is_stale(work, archive, tmp_path):
    d = full(work)
    (tmp_path / "before").mkdir()
    assert run(work, tmp_path / "before", tmp_path, [d]).published == 1  # publishable before the change
    changed = dataclasses.replace(d, ui_in=5)
    assert reason_of(work, archive, tmp_path, changed) == "stale reference (inputs changed)"


def test_a_manifest_uio_in_changed_after_the_reference_was_made_is_stale(work, archive, tmp_path):
    d = full(work)
    (tmp_path / "before").mkdir()
    assert run(work, tmp_path / "before", tmp_path, [d]).published == 1
    changed = dataclasses.replace(d, uio_in=3)
    assert reason_of(work, archive, tmp_path, changed) == "stale reference (inputs changed)"


@pytest.mark.parametrize("which", ["ideal", "ref"])
def test_a_record_whose_requested_input_differs_from_the_manifest_is_refused(work, archive, tmp_path, which):
    d = full(work, ui_in=0, **{which: {"ui_in_requested": 7}})
    assert reason_of(work, archive, tmp_path, d) == "stale reference (inputs changed)"


def test_a_board_profile_whose_data_changed_is_stale(work, archive, tmp_path):
    d = full(work)
    assert run(work, archive, tmp_path, [d]).published == 1
    changed = tmp_path / "fpga-4.json"
    data = json.loads(Path(PROFILE).read_text())
    data["date"] = "2099-01-01"
    changed.write_text(json.dumps(data))
    s = publish.run([d], archive=archive, work=work, profile=str(changed), check_cmd=checker(tmp_path),
                    allow_removals=True)  # fmt: skip
    assert s.skipped == [{"id": d.id, "reason": "stale reference (board profile changed)"}]


def test_a_board_profile_renamed_is_stale(work, archive, tmp_path):
    d = bare()
    put_build(work, d)
    put_ref(work, d)
    renamed = tmp_path / "other.json"
    renamed.write_text(Path(PROFILE).read_text())
    put_ref(work, d, "other", made_under=profile_object("fpga-4"))  # made under the old name
    s = publish.run([d], archive=archive, work=work, profile=str(renamed), check_cmd=checker(tmp_path))
    assert s.skipped == [{"id": d.id, "reason": "stale reference (board profile changed)"}]


def test_a_changed_profile_with_other_ties_is_stale(work, archive, tmp_path):
    d = bare()
    put_build(work, d)
    put_ref(work, d)
    other = json.loads(Path(PROFILE).read_text())
    other["drive_conflicts"] = [*other["drive_conflicts"], {"ui_in": 5, "uio": 5}]
    put_ref(work, d, "fpga-4", made_under=reference.Profile("fpga-4", other))
    assert reason_of(work, archive, tmp_path, d) == "stale reference (board profile changed)"


@pytest.mark.parametrize("verdict", [None, "", "OK", "a" * 33, "no_sync", "ok\n"])
def test_skip_without_a_usable_render_verdict(work, archive, tmp_path, verdict):
    d = full(work, render_verdict=verdict)
    assert reason_of(work, archive, tmp_path, d) == "no render verdict"


@pytest.mark.parametrize(
    ("field", "kw"),
    [
        ("title", {"title": "t" * 201}),
        ("author", {"author": "a" * 201}),
        ("repo_url", {"repo": "http://github.com/o/r"}),
        ("repo_url", {"repo": "https://github.com/" + "r" * 500}),
        ("macro", {}),
    ],
)
def test_a_design_whose_field_breaks_the_spec_is_skipped_naming_the_field(work, archive, tmp_path, field, kw):
    id = "tt05/tt_um_" + "m" * 59 if field == "macro" else "tt05/tt_um_a"
    d = full(work, id, **kw)
    assert field in reason_of(work, archive, tmp_path, d)


def test_a_macro_without_the_prefix_is_skipped_not_a_crash(work, archive, tmp_path):
    d = full(work, "tt05/vga_thing")
    assert "macro" in reason_of(work, archive, tmp_path, d)


def test_a_bad_shuttle_field_is_skipped(work, archive, tmp_path):
    d = full(work, "tt05of/tt_um_a", shuttle="TT_05")
    assert "shuttle" in reason_of(work, archive, tmp_path, d)


# ---- the entry


def test_a_published_entry_has_every_field_in_the_spec_order(work, archive, tmp_path):
    d = full(
        work, pinout={"ui": ["a", 5], "uo": ["x" * 70] + ["y"] * 8, "uio": []}, title="A title", author="Ann",
        ref={"ui_in_mask_used": 0x0F, "ui_in_requested": 0x13}, build={"fmax_hz": 31_420_000},
    )  # fmt: skip
    s = run(work, archive, tmp_path, [d])
    assert s.published == 1 and s.timing_not_met == 0
    top = doc(archive)
    assert list(top) == ["schema", "generated", "tthfv_commit", "videos_repo_commit", "designs"]
    assert top["schema"] == 1 and top["generated"] == "2026-11-26T03:00:00Z"
    assert top["tthfv_commit"] == TTHFV and top["videos_repo_commit"] == VIDEOS
    (e,) = top["designs"]
    assert list(e) == [
        "id", "shuttle", "macro", "title", "author", "description", "docs_url", "repo_url", "commit",
        "clock_hz", "pinout", "ui_in", "ui_in_mask", "bin", "bin_bytes", "bin_sha256", "build_status",
        "fmax_hz", "pdk_latches", "validation", "verdict", "qspi", "poster", "preview_gif", "video_10s_webm",
    ]  # fmt: skip
    sha = hashlib.sha256(BIN).hexdigest()
    assert e["id"] == "tt05_a" and e["shuttle"] == "tt05" and e["macro"] == "tt_um_a"
    assert e["title"] == "A title" and e["author"] == "Ann" and e["description"] == ""
    assert e["docs_url"] == "https://tinytapeout.com/chips/tt05/tt_um_a"
    assert e["repo_url"] == "https://github.com/o/r" and e["commit"] == COMMIT
    assert e["clock_hz"] == 25_175_000
    assert e["pinout"] == {
        "ui_in": ["a", "5", "", "", "", "", "", ""], "uo_out": ["x" * 64] + ["y"] * 7, "uio": [""] * 8,
    }  # fmt: skip
    assert list(e["pinout"]) == ["ui_in", "uo_out", "uio"]
    assert (e["ui_in_mask"], e["ui_in"]) == (0x0F, 0x03)
    assert e["bin"] == f"tt05/tt_um_a/tt05_a-{sha[:12]}.bin"
    assert (e["bin_bytes"], e["bin_sha256"]) == (len(BIN), sha)
    assert (e["build_status"], e["fmax_hz"], e["validation"]) == ("ok", 31_420_000, "none")
    assert (e["verdict"], e["qspi"]) == ("ok", "none")
    assert (e["poster"], e["preview_gif"], e["video_10s_webm"]) == (None, None, None)
    assert (archive / e["bin"]).read_bytes() == BIN
    assert (archive / "fpga-library.json").read_text().endswith("}\n")


def test_empty_title_falls_back_to_the_macro_and_timing_fail_has_no_warning_text(work, archive, tmp_path):
    d = full(work, title="", build={"status": "timing-fail", "fmax_hz": 20_000_000})
    s = run(work, archive, tmp_path, [d])
    (e,) = doc(archive)["designs"]
    assert s.timing_not_met == 1
    assert e["title"] == "tt_um_a" and e["build_status"] == "timing-fail" and e["description"] == ""
    assert "timing" not in json.dumps({k: e[k] for k in ("title", "description")})


def test_repo_url_loses_trailing_dot_git_and_slash(work, archive, tmp_path):
    for repo in ("https://github.com/o/r/", "https://github.com/o/r.git/", "https://github.com/o/r.git"):
        d = full(work, repo=repo)
        run(work, archive, tmp_path, [d])
        assert doc(archive)["designs"][0]["repo_url"] == "https://github.com/o/r"


def test_ui_in_is_null_exactly_when_the_mask_is_zero(work, archive, tmp_path):
    d = full(work, ref={"ui_in_mask_used": 0, "ui_in_requested": 0x55})
    run(work, archive, tmp_path, [d])
    (e,) = doc(archive)["designs"]
    assert (e["ui_in_mask"], e["ui_in"]) == (0, None)
    d = full(work, ref={"ui_in_mask_used": 0x01, "ui_in_requested": 0x00})
    run(work, archive, tmp_path, [d])
    (e,) = doc(archive)["designs"]
    assert (e["ui_in_mask"], e["ui_in"]) == (1, 0)


def test_ui_in_never_has_bits_outside_the_mask(work, archive, tmp_path):
    for mask, req in [(0xF0, 0xFF), (0x01, 0xFE), (0xFF, 0xA5), (0x24, 0x7F)]:
        d = full(work, ref={"ui_in_mask_used": mask, "ui_in_requested": req})
        run(work, archive, tmp_path, [d])
        (e,) = doc(archive)["designs"]
        assert e["ui_in"] & ~e["ui_in_mask"] == 0 and e["ui_in"] == req & mask


@pytest.mark.parametrize("bad", [-1, 256, True, "3", None])
def test_a_mask_out_of_range_skips(work, archive, tmp_path, bad):
    d = full(work, ref={"ui_in_mask_used": bad})
    assert "ui_in_mask" in reason_of(work, archive, tmp_path, d)


def test_the_json_is_written_with_allow_nan_false(work, archive, tmp_path, monkeypatch):
    seen = []
    real = json.dumps

    def spy(*a, **k):
        seen.append(k)
        return real(*a, **k)

    monkeypatch.setattr(publish.json, "dumps", spy)
    run(work, archive, tmp_path, [full(work)])
    docs = [k for k in seen if "indent" in k]
    assert docs and all(k.get("allow_nan") is False and k["indent"] == 2 for k in docs)


# ---- media


def test_media_present_and_absent(work, archive, tmp_path):
    d = full(work)
    media = tmp_path / "media"
    base = media / "tt05" / "tt_um_a"
    base.mkdir(parents=True)
    (base / "tt05_tt_um_a_poster.png").write_bytes(b"png")
    (base / "tt05_tt_um_a_10s.webm").write_bytes(b"webm")
    (base / "tt05_tt_um_a_preview.gif").mkdir()  # not a regular file
    (archive / "tt05" / "tt_um_a").mkdir(
        parents=True
    )  # the site reads the archive: the files must be there too
    for name in ("tt05_tt_um_a_poster.png", "tt05_tt_um_a_10s.webm", "tt05_tt_um_a_preview.gif"):
        (archive / "tt05" / "tt_um_a" / name).write_bytes(b"in the archive")
    run(work, archive, tmp_path, [d], media_root=media)
    (e,) = doc(archive)["designs"]
    assert e["poster"] == "tt05/tt_um_a/tt05_tt_um_a_poster.png"
    assert e["video_10s_webm"] == "tt05/tt_um_a/tt05_tt_um_a_10s.webm"
    assert e["preview_gif"] is None


def test_media_root_defaults_to_the_archive(work, archive, tmp_path):
    d = full(work)
    (archive / "tt05" / "tt_um_a").mkdir(parents=True)
    (archive / "tt05" / "tt_um_a" / "tt05_tt_um_a_preview.gif").write_bytes(b"gif")
    run(work, archive, tmp_path, [d])
    assert doc(archive)["designs"][0]["preview_gif"] == "tt05/tt_um_a/tt05_tt_um_a_preview.gif"


def test_media_under_the_media_root_but_absent_from_the_archive_is_null(work, archive, tmp_path):
    d = full(work)
    base = tmp_path / "media" / "tt05" / "tt_um_a"
    base.mkdir(parents=True)
    (base / "tt05_tt_um_a_poster.png").write_bytes(b"png")
    run(work, archive, tmp_path, [d], media_root=tmp_path / "media")
    assert doc(archive)["designs"][0]["poster"] is None


def test_media_reached_through_a_symlinked_directory_is_null(work, archive, tmp_path):
    d = full(work)
    real = tmp_path / "real-shuttle" / "tt_um_a"
    real.mkdir(parents=True)
    (real / "tt05_tt_um_a_poster.png").write_bytes(b"png")
    media = tmp_path / "media"
    media.mkdir()
    (media / "tt05").symlink_to(tmp_path / "real-shuttle")
    run(work, archive, tmp_path, [d], media_root=media)
    assert doc(archive)["designs"][0]["poster"] is None


def test_a_symlinked_directory_in_the_archive_gives_null_media_when_it_is_the_media_root(
    work, archive, tmp_path
):
    d = full(work)
    run(work, archive, tmp_path, [d])  # makes tt05/tt_um_a in the archive
    (archive / "tt05" / "tt_um_a" / "tt05_tt_um_a_poster.png").write_bytes(b"png")
    media = tmp_path / "media"
    media.mkdir()
    (media / "tt05").symlink_to(archive / "tt05")
    run(work, archive, tmp_path, [d], media_root=media)
    assert doc(archive)["designs"][0]["poster"] is None


def test_a_symlinked_media_file_is_not_used(work, archive, tmp_path):
    d = full(work)
    media = tmp_path / "media" / "tt05" / "tt_um_a"
    media.mkdir(parents=True)
    real = tmp_path / "real.png"
    real.write_bytes(b"x")
    (media / "tt05_tt_um_a_poster.png").symlink_to(real)
    run(work, archive, tmp_path, [d], media_root=tmp_path / "media")
    assert doc(archive)["designs"][0]["poster"] is None


# ---- order


def test_entries_are_grouped_by_shuttle_newest_first_then_title_then_id(work, archive, tmp_path):
    ds = [
        full(work, "tt05/tt_um_a", title="Zed"),
        full(work, "tt08/tt_um_b", title="beta"),
        full(work, "tt08/tt_um_c", title="Alpha"),
        full(work, "tt05/tt_um_d", title="Zed"),
        full(work, "ttsky26c/tt_um_e", title="Mid"),
    ]
    run(work, archive, tmp_path, ds)
    assert [e["macro"] for e in doc(archive)["designs"]] == [
        "tt_um_e",
        "tt_um_c",
        "tt_um_b",
        "tt_um_a",
        "tt_um_d",
    ]


def test_titles_differing_only_in_case_sort_case_insensitively(work, archive, tmp_path):
    ds = [
        full(work, "tt05/tt_um_a", title="Banana"),
        full(work, "tt05/tt_um_b", title="apple"),
        full(work, "tt05/tt_um_c", title="Cherry"),
    ]
    run(work, archive, tmp_path, ds)
    assert [e["title"] for e in doc(archive)["designs"]] == ["apple", "Banana", "Cherry"]


def test_the_shuttle_table_is_the_final_one():
    assert publish.SHUTTLE_ORDER == (
        "ttihp26b",
        "ttsky26c",
        "ttgf0p3",
        "ttgf26b",
        "ttgf26a",
        "ttsky26b",
        "ttsky26a",
        "ttihp0p4",
        "ttihp26a",
        "ttgf0p2",
        "ttsky25b",
        "ttgf0p1",
        "ttsky25a",
        "ttihp25b",
        "ttcad25a",
        "ttihp0p3",
        "ttihp25a",
        "tt09",
        "ttihp0p2",
        "tt08",
        "ttihp0p1",
        "tt07",
        "tt06",
        "tt05of",
        "tt05",
        "tt04",
        "tt03p5",
        "tt03",
        "tt02",
    )
    assert len(publish.SHUTTLE_ORDER) == 29


def test_an_unknown_shuttle_fails_the_run_naming_it(work, archive, tmp_path):
    d = full(work, "ttnew1/tt_um_a")
    with pytest.raises(UserError, match="ttnew1"):
        run(work, archive, tmp_path, [d])
    assert list(archive.iterdir()) == []


# ---- writing


def test_an_identical_existing_bin_is_left_untouched(work, archive, tmp_path):
    d = full(work)
    run(work, archive, tmp_path, [d])
    (e,) = doc(archive)["designs"]
    path = archive / e["bin"]
    before = path.stat()
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    before = path.stat()
    run(work, archive, tmp_path, [d])
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_a_different_file_at_the_bin_name_fails_the_run_and_is_untouched(work, archive, tmp_path):
    d = full(work)
    run(work, archive, tmp_path, [d])
    path = archive / doc(archive)["designs"][0]["bin"]
    path.write_bytes(b"someone else's bytes")
    index_before = (archive / "fpga-library.json").read_bytes()
    with pytest.raises(UserError, match="already exists"):
        run(work, archive, tmp_path, [d])
    assert path.read_bytes() == b"someone else's bytes"
    assert (archive / "fpga-library.json").read_bytes() == index_before
    assert not (archive / "fpga-library.json.tmp").exists()


def test_a_symlink_or_directory_at_the_bin_name_fails_the_run(work, archive, tmp_path):
    d = full(work)
    run(work, archive, tmp_path, [d])
    path = archive / doc(archive)["designs"][0]["bin"]
    path.unlink()
    path.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(UserError):
        run(work, archive, tmp_path, [d])
    assert path.is_symlink()
    path.unlink()
    path.mkdir()
    with pytest.raises(UserError):
        run(work, archive, tmp_path, [d])
    assert path.is_dir()


def test_a_failing_checker_leaves_the_old_index_and_no_tmp(work, archive, tmp_path):
    d = full(work)
    run(work, archive, tmp_path, [d])
    old = (archive / "fpga-library.json").read_bytes()
    d2 = full(work, "tt08/tt_um_b")
    with pytest.raises(UserError, match="checker says hello"):
        run(work, archive, tmp_path, [d, d2], check_cmd=checker(tmp_path, code=1))
    assert (archive / "fpga-library.json").read_bytes() == old
    assert not (archive / "fpga-library.json.tmp").exists()


def test_a_checker_that_cannot_start_is_a_failure_too(work, archive, tmp_path):
    with pytest.raises(UserError, match="no-such-checker"):
        run(work, archive, tmp_path, [full(work)], check_cmd="/no-such-dir/no-such-checker")
    assert not (archive / "fpga-library.json").exists()
    assert not (archive / "fpga-library.json.tmp").exists()


def test_the_checker_gets_the_tmp_index_and_the_archive(work, archive, tmp_path):
    rec = tmp_path / "argv.json"
    run(work, archive, tmp_path, [full(work)], check_cmd=checker(tmp_path, record=rec))
    assert json.loads(rec.read_text()) == [str(archive / "fpga-library.json.tmp"), "--bins", str(archive)]


def test_the_checker_sees_the_complete_tmp_index_and_the_bins(work, archive, tmp_path):
    seen = tmp_path / "seen.txt"
    script = tmp_path / "peek.py"
    script.write_text(
        "import json, sys, pathlib\n"
        "d = json.load(open(sys.argv[1]))\n"
        f"pathlib.Path({str(seen)!r}).write_text(''.join(str(pathlib.Path(sys.argv[3], e['bin']).is_file()) for e in d['designs']))\n"
    )
    run(work, archive, tmp_path, [full(work)], check_cmd=f"{sys.executable} {script}")
    assert seen.read_text() == "True"


def test_nothing_is_deleted_from_an_archive_with_other_files_and_old_bins(work, archive, tmp_path):
    (archive / "index.json").write_text("videos index")
    old = archive / "tt05" / "tt_um_a"
    old.mkdir(parents=True)
    (old / "tt05_a-000000000000.bin").write_bytes(b"old bin")
    (old / "tt05_tt_um_a_poster.png").write_bytes(b"poster")
    (archive / "tt05" / "unrelated.txt").write_text("x")
    (archive / "fpga-library.json").write_text('{"schema": 1, "designs": []}')
    before = snapshot(archive)
    run(work, archive, tmp_path, [full(work)])
    after = snapshot(archive)
    for name, data in before.items():
        if name != "fpga-library.json":
            assert after[name] == data
    assert doc(archive)["schema"] == 1


def test_a_second_version_of_a_design_adds_a_bin_and_keeps_the_old_one(work, archive, tmp_path):
    d = full(work)
    run(work, archive, tmp_path, [d])
    first = doc(archive)["designs"][0]["bin"]
    new = BIN + b"v2"
    put_build(work, d, data=new)
    run(work, archive, tmp_path, [d])
    second = doc(archive)["designs"][0]["bin"]
    assert first != second
    assert (archive / first).read_bytes() == BIN and (archive / second).read_bytes() == new


def test_modes_are_0644_and_0755_under_a_restrictive_umask(work, archive, tmp_path):
    old = os.umask(0o077)
    try:
        run(work, archive, tmp_path, [full(work)])
    finally:
        os.umask(old)
    e = doc(archive)["designs"][0]
    assert (archive / "fpga-library.json").stat().st_mode & 0o777 == 0o644
    assert (archive / e["bin"]).stat().st_mode & 0o777 == 0o644
    for d in (archive / "tt05", archive / "tt05" / "tt_um_a"):
        assert d.stat().st_mode & 0o777 == 0o755


def test_a_symlinked_shuttle_or_macro_directory_is_refused(work, archive, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (archive / "tt05").symlink_to(target)
    with pytest.raises(UserError, match="symbolic link"):
        run(work, archive, tmp_path, [full(work)])
    assert list(target.iterdir()) == []
    (archive / "tt05").unlink()
    (archive / "tt05").mkdir()
    (archive / "tt05" / "tt_um_a").symlink_to(target)
    with pytest.raises(UserError, match="symbolic link"):
        run(work, archive, tmp_path, [full(work)])
    assert list(target.iterdir()) == []


def test_the_archive_must_exist_and_be_a_directory(work, tmp_path):
    with pytest.raises(UserError, match="archive"):
        run(work, tmp_path / "missing", tmp_path, [full(work)])
    (tmp_path / "file").write_text("x")
    with pytest.raises(UserError, match="archive"):
        run(work, tmp_path / "file", tmp_path, [full(work)])
    assert not (tmp_path / "missing").exists()


def test_the_live_archive_is_refused_also_through_a_symlink(work, tmp_path, monkeypatch):
    live = tmp_path / "live-stand-in"
    real = tmp_path / "backups"
    real.mkdir()
    live.symlink_to(real)
    (real / "sub").mkdir()
    link = tmp_path / "link"
    link.symlink_to(real / "sub")
    for target in (live, real, real / "sub", link):
        with pytest.raises(UserError, match="not approved yet"):
            run(work, target, tmp_path, [full(work)])
    assert list(real.rglob("*")) == [real / "sub"]
    monkeypatch.setenv("TTHFV_PUBLISH_LIVE", "approved-by-owner")
    assert run(work, real, tmp_path, [full(work)]).published == 1


def test_an_archive_beside_the_live_one_is_fine(work, tmp_path):
    other = tmp_path / "live-stand-in-2"
    other.mkdir()
    assert run(work, other, tmp_path, [full(work)]).published == 1


def test_no_git_head_or_no_videos_commit_fails_and_writes_nothing(work, archive, tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "_tthfv_commit", lambda: None)
    with pytest.raises(UserError, match="git"):
        run(work, archive, tmp_path, [full(work)])
    monkeypatch.setattr(publish, "_tthfv_commit", lambda: TTHFV)
    monkeypatch.setattr(publish, "_videos_repo_commit", lambda: None)
    with pytest.raises(UserError, match="videos"):
        run(work, archive, tmp_path, [full(work)])
    assert list(archive.iterdir()) == []


def test_two_runs_give_identical_bytes_apart_from_generated(work, archive, tmp_path, monkeypatch):
    ds = [full(work, "tt05/tt_um_b"), full(work, "tt08/tt_um_a"), bare("tt05/tt_um_c")]
    run(work, archive, tmp_path, ds)
    first = (archive / "fpga-library.json").read_bytes()
    run(work, archive, tmp_path, ds)  # the same clock: identical bytes
    assert (archive / "fpga-library.json").read_bytes() == first
    monkeypatch.setattr(publish, "_now", lambda: datetime(2027, 1, 2, 3, 4, 5, tzinfo=UTC))
    run(work, archive, tmp_path, ds)
    second = (archive / "fpga-library.json").read_bytes()
    assert second != first
    assert second.replace(b"2027-01-02T03:04:05Z", b"2026-11-26T03:00:00Z") == first


def test_an_oserror_while_writing_is_a_user_error_naming_the_path(work, archive, tmp_path, monkeypatch):
    real = os.link

    def boom(src, dst, *a, **k):
        raise PermissionError(13, "Permission denied", str(dst))

    monkeypatch.setattr(os, "link", boom)
    with pytest.raises(UserError, match="Permission denied"):
        run(work, archive, tmp_path, [full(work)])
    monkeypatch.setattr(os, "link", real)
    assert list(archive.rglob(".*tmp*")) == []


# ---- the command


def cli(monkeypatch, ds, argv):
    monkeypatch.setattr(manifest, "load", lambda *a, **k: ds)
    return main(["publish", *argv])


def test_cli_summary_reasons_and_demo_names_commit(work, archive, tmp_path, monkeypatch, capsys):
    ds = [
        full(work, "tt05/tt_um_a"),
        full(work, "tt08/tt_um_t", build={"status": "timing-fail", "fmax_hz": 1_000_000}),
        bare("tt05/tt_um_n"),
        bare("tt05/tt_um_m"),
    ]
    assert cli(monkeypatch, ds, ["--archive", str(archive), "--check-cmd", checker(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"published 2 designs (1 timing not met), skipped 2 -> {archive}"
    assert lines[1] == "  2 x no build record"
    assert "da7d10d" in lines[-1]
    assert "tt05/tt_um_n" not in "\n".join(lines)


def test_cli_list_skipped_prints_every_skipped_id(work, archive, tmp_path, monkeypatch, capsys):
    ds = [bare("tt05/tt_um_n"), bare("tt05/tt_um_m")]
    assert (
        cli(monkeypatch, ds, ["--archive", str(archive), "--check-cmd", checker(tmp_path), "--list-skipped"])
        == 0
    )
    out = capsys.readouterr().out
    assert "tt05/tt_um_m: no build record" in out and "tt05/tt_um_n: no build record" in out


def test_cli_only_restricts_and_an_unknown_id_is_an_error(work, archive, tmp_path, monkeypatch, capsys):
    ds = [full(work, "tt05/tt_um_a"), full(work, "tt05/tt_um_b")]
    args = ["--archive", str(archive), "--check-cmd", checker(tmp_path)]
    assert cli(monkeypatch, ds, [*args, "--only", "tt05/tt_um_b"]) == 0
    assert [e["macro"] for e in doc(archive)["designs"]] == ["tt_um_b"]
    capsys.readouterr()
    assert cli(monkeypatch, ds, [*args, "--only", "tt05/tt_um_zzz"]) == 1
    assert "tt_um_zzz" in capsys.readouterr().err
    assert [e["macro"] for e in doc(archive)["designs"]] == ["tt_um_b"]


def test_cli_profile_name_is_the_stem_of_the_file(work, archive, tmp_path, monkeypatch):
    d = bare(ui_in=1)
    put_build(work, d)
    put_ref(work, d)
    myboard = tmp_path / "myboard.json"
    myboard.write_text(Path(PROFILE).read_text())
    put_ref(work, d, "myboard", ui_in_mask_used=3)
    args = ["--archive", str(archive), "--check-cmd", checker(tmp_path)]
    assert cli(monkeypatch, [d], [*args, "--profile", str(myboard)]) == 0
    assert doc(archive)["designs"][0]["ui_in"] == 1
    assert cli(monkeypatch, [d], [*args, "--allow-removals"]) == 0  # the default is fpga-4: no reference here
    assert doc(archive)["designs"] == []  # so the design is skipped


def two_published(work, archive, tmp_path):
    a, b = full(work, "tt05/tt_um_a"), full(work, "tt05/tt_um_b")
    run(work, archive, tmp_path, [a, b])
    return a, b


def test_a_removal_is_refused_and_nothing_is_written(work, archive, tmp_path):
    a, _b = two_published(work, archive, tmp_path)
    before = snapshot(archive)
    c = full(work, "tt08/tt_um_c")  # new, so its bin would be written
    with pytest.raises(UserError) as e:
        run(work, archive, tmp_path, [a, c])
    assert "tt05_b" in str(e.value) and "1" in str(e.value) and "--allow-removals" in str(e.value)
    assert snapshot(archive) == before
    assert not (archive / "fpga-library.json.tmp").exists()
    assert not (archive / "tt08").exists()


def test_allow_removals_proceeds_and_lists_them(work, archive, tmp_path):
    a, _b = two_published(work, archive, tmp_path)
    old_bin = archive / doc(archive)["designs"][1]["bin"]
    s = run(work, archive, tmp_path, [a], allow_removals=True)
    assert s.removed == ["tt05_b"]
    assert [e["id"] for e in doc(archive)["designs"]] == ["tt05_a"]
    assert old_bin.is_file()  # a removed design's bin stays


def test_a_design_added_with_none_removed_needs_no_flag(work, archive, tmp_path):
    a, b = two_published(work, archive, tmp_path)
    c = full(work, "tt08/tt_um_c")
    s = run(work, archive, tmp_path, [a, b, c])
    assert s.removed == [] and len(doc(archive)["designs"]) == 3


def test_a_skipped_design_that_was_listed_counts_as_a_removal(work, archive, tmp_path):
    a, b = two_published(work, archive, tmp_path)
    put_build(work, b, status="pnr-fail")
    with pytest.raises(UserError, match="tt05_b"):
        run(work, archive, tmp_path, [a, b])


@pytest.mark.parametrize(
    "text", ["{not json", "[1, 2]", '{"designs": 5}', '{"designs": [{"id": 3}]}', '{"designs": [NaN]}', '{"designs": Infinity}'],
)  # fmt: skip
def test_an_unreadable_previous_index_is_refused_even_with_allow_removals(work, archive, tmp_path, text):
    (archive / "fpga-library.json").write_text(text)
    with pytest.raises(UserError, match="fpga-library.json"):
        run(work, archive, tmp_path, [full(work)], allow_removals=True)
    assert (archive / "fpga-library.json").read_text() == text
    assert not (archive / "tt05").exists()


def test_a_previous_index_that_is_a_directory_is_refused(work, archive, tmp_path):
    (archive / "fpga-library.json").mkdir()
    with pytest.raises(UserError, match="fpga-library.json"):
        run(work, archive, tmp_path, [full(work)])
    assert not (archive / "tt05").exists()


def test_cli_removals_need_the_flag_and_are_listed(work, archive, tmp_path, monkeypatch, capsys):
    a, b = two_published(work, archive, tmp_path)
    put_build(work, b, status="pnr-fail")  # b is no longer publishable
    args = ["--archive", str(archive), "--check-cmd", checker(tmp_path)]
    assert cli(monkeypatch, [a, b], args) == 1
    assert "tt05_b" in capsys.readouterr().err
    assert cli(monkeypatch, [a, b], [*args, "--allow-removals"]) == 0
    assert "removed from the index: tt05_b" in capsys.readouterr().out


def first_bin(archive: Path) -> Path:
    return archive / doc(archive)["designs"][0]["bin"]


def test_a_correct_bin_with_a_second_hard_link_is_accepted_and_untouched(work, archive, tmp_path):
    d = full(work)
    run(work, archive, tmp_path, [d])
    path = first_bin(archive)
    os.link(path, tmp_path / "backup-snapshot.bin")
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    before = path.stat()
    assert before.st_nlink == 2
    run(work, archive, tmp_path, [d])
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_nlink) == (before.st_ino, before.st_mtime_ns, 2)


def test_a_wrong_bin_with_a_second_hard_link_is_refused(work, archive, tmp_path):
    d = full(work)
    run(work, archive, tmp_path, [d])
    path = first_bin(archive)
    path.write_bytes(b"wrong")
    os.link(path, tmp_path / "backup-snapshot.bin")
    with pytest.raises(UserError, match="different bytes"):
        run(work, archive, tmp_path, [d])
    assert path.read_bytes() == b"wrong"


def test_a_huge_file_at_a_bin_name_is_a_user_error_not_a_traceback(work, archive, tmp_path):
    d = full(work)
    run(work, archive, tmp_path, [d])
    path = first_bin(archive)
    path.unlink()
    with path.open("wb") as f:
        f.truncate(17 << 20)
    with pytest.raises(UserError, match="different bytes"):
        run(work, archive, tmp_path, [d])


def test_an_oserror_reading_an_existing_bin_is_a_user_error(work, archive, tmp_path):
    d = full(work)
    run(work, archive, tmp_path, [d])
    path = first_bin(archive)
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running with privileges that ignore file modes")
        with pytest.raises(UserError, match=path.name):
            run(work, archive, tmp_path, [d])
    finally:
        path.chmod(0o644)


@pytest.mark.parametrize(
    "text",
    [
        "{}",
        '{"designs": []}',
        '{"schema": 2, "designs": []}',
        '{"schema": true, "designs": []}',
        '{"schema": 1}',
        '{"schema": 1, "designs": {}}',
        '{"schema": 1, "designs": [1]}',
        '{"schema": 1, "designs": [{"title": "x"}]}',
        '{"schema": 1, "designs": [{"id": 5}]}',
    ],
)
def test_a_previous_file_that_is_not_a_library_index_is_refused(work, archive, tmp_path, text):
    (archive / "fpga-library.json").write_text(text)
    with pytest.raises(UserError, match="not a library index"):
        run(work, archive, tmp_path, [full(work)], allow_removals=True)
    assert (archive / "fpga-library.json").read_text() == text
    assert not (archive / "tt05").exists()


def test_a_hard_linked_previous_index_is_read_and_the_other_link_keeps_the_old_bytes(work, archive, tmp_path):
    d = full(work)
    run(work, archive, tmp_path, [d])
    index = archive / "fpga-library.json"
    old = index.read_bytes()
    snapshot_link = tmp_path / "snapshot-index.json"
    os.link(index, snapshot_link)
    assert index.stat().st_nlink == 2
    run(work, archive, tmp_path, [d, full(work, "tt08/tt_um_b")])
    assert snapshot_link.read_bytes() == old
    assert index.read_bytes() != old and index.stat().st_nlink == 1


def test_a_previous_index_over_16_mib_is_a_user_error(work, archive, tmp_path):
    index = archive / "fpga-library.json"
    with index.open("wb") as f:
        f.truncate(17 << 20)
    with pytest.raises(UserError, match="fpga-library.json"):
        run(work, archive, tmp_path, [full(work)], allow_removals=True)
    assert index.stat().st_size == 17 << 20


def test_a_previous_index_that_is_a_symlink_is_refused(work, archive, tmp_path):
    real = tmp_path / "real-index.json"
    real.write_text('{"schema": 1, "designs": []}')
    (archive / "fpga-library.json").symlink_to(real)
    with pytest.raises(UserError, match="fpga-library.json"):
        run(work, archive, tmp_path, [full(work)])


def test_duplicate_ids_in_the_previous_index_are_refused(work, archive, tmp_path):
    text = '{"schema": 1, "designs": [{"id": "x"}, {"id": "x"}]}'
    (archive / "fpga-library.json").write_text(text)
    with pytest.raises(UserError, match="not a library index"):
        run(work, archive, tmp_path, [full(work)], allow_removals=True)
    assert (archive / "fpga-library.json").read_text() == text
    assert not (archive / "tt05").exists()


def test_a_collision_on_the_third_bin_leaves_nothing_new_behind(work, archive, tmp_path):
    ds = [full(work, "ttsky26c/tt_um_a"), full(work, "tt08/tt_um_b"), full(work, "tt05/tt_um_c")]
    sha = hashlib.sha256(BIN).hexdigest()[:12]
    third = archive / "tt05" / "tt_um_c" / f"tt05_c-{sha}.bin"
    third.parent.mkdir(parents=True)
    third.write_bytes(b"someone else")
    before = sorted(str(p.relative_to(archive)) for p in archive.rglob("*"))
    with pytest.raises(UserError, match="different bytes"):
        run(work, archive, tmp_path, ds)
    assert sorted(str(p.relative_to(archive)) for p in archive.rglob("*")) == before


def test_a_tmp_index_that_is_a_directory_or_symlink_is_refused_before_any_write(work, archive, tmp_path):
    tmp = archive / "fpga-library.json.tmp"
    tmp.mkdir()
    with pytest.raises(UserError, match="fpga-library.json.tmp"):
        run(work, archive, tmp_path, [full(work)])
    assert list(archive.iterdir()) == [tmp]
    tmp.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("keep")
    tmp.symlink_to(elsewhere)
    with pytest.raises(UserError, match="fpga-library.json.tmp"):
        run(work, archive, tmp_path, [full(work)])
    assert (
        elsewhere.read_text() == "keep"
        and tmp.is_symlink()
        and [p.name for p in archive.iterdir()] == [tmp.name]
    )


def test_a_leftover_regular_tmp_index_is_replaced(work, archive, tmp_path):
    (archive / "fpga-library.json.tmp").write_text("leftover")
    run(work, archive, tmp_path, [full(work)])
    assert not (archive / "fpga-library.json.tmp").exists()


def a_valid_entry(work, archive, tmp_path, **ref) -> dict:
    run(work, archive, tmp_path, [full(work, ref=ref or None)])
    return doc(archive)["designs"][0]


def test_check_entry_accepts_the_entries_it_writes(work, archive, tmp_path):
    publish._check_entry(a_valid_entry(work, archive, tmp_path))
    publish._check_entry(a_valid_entry(work, archive, tmp_path, ui_in_mask_used=1, ui_in_requested=1))


def test_check_entry_refuses_ui_in_with_a_zero_mask(work, archive, tmp_path):
    e = a_valid_entry(work, archive, tmp_path, ui_in_mask_used=0)
    assert e["ui_in"] is None
    with pytest.raises(publish.Skip, match="ui_in"):
        publish._check_entry({**e, "ui_in": 0})


def test_check_entry_refuses_a_null_ui_in_with_a_mask_and_bits_outside_it(work, archive, tmp_path):
    e = a_valid_entry(work, archive, tmp_path, ui_in_mask_used=3, ui_in_requested=1)
    with pytest.raises(publish.Skip, match="ui_in"):
        publish._check_entry({**e, "ui_in": None})
    with pytest.raises(publish.Skip, match="ui_in"):
        publish._check_entry({**e, "ui_in": 4})


def test_check_entry_refuses_a_bin_that_is_not_the_derived_path(work, archive, tmp_path):
    e = a_valid_entry(work, archive, tmp_path)
    other = e["bin"].replace(e["bin_sha256"][:12], "0" * 12)
    with pytest.raises(publish.Skip, match="bin"):
        publish._check_entry({**e, "bin": other})
    with pytest.raises(publish.Skip, match="bin"):
        publish._check_entry({**e, "bin": e["bin"].replace("tt05_a-", "tt05_x-")})


def test_everything_is_flushed_before_it_is_put_in_place(work, archive, tmp_path, monkeypatch):
    events = []
    real_fsync, real_link, real_replace = os.fsync, os.link, os.replace

    def fsync(fd):
        events.append(("fsync", os.readlink(f"/proc/self/fd/{fd}")))
        return real_fsync(fd)

    def link(src, dst, *a, **k):
        events.append(("link", str(src), str(dst)))
        return real_link(src, dst, *a, **k)

    def replace(src, dst, *a, **k):
        events.append(("replace", str(src), str(dst)))
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(os, "replace", replace)
    run(work, archive, tmp_path, [full(work)])
    link_at = next(i for i, e in enumerate(events) if e[0] == "link")
    bin_tmp = events[link_at][1]
    assert ("fsync", bin_tmp) in events[:link_at]
    replace_at = next(i for i, e in enumerate(events) if e[0] == "replace")
    index_tmp = str(archive / "fpga-library.json.tmp")
    assert ("fsync", index_tmp) in events[:replace_at]
    assert link_at < replace_at
    assert ("fsync", str(archive)) in events[replace_at:]


def test_a_failing_directory_fsync_is_ignored(work, archive, tmp_path, monkeypatch):
    real = os.fsync

    def fsync(fd):
        if os.readlink(f"/proc/self/fd/{fd}") == str(archive):
            raise OSError(5, "Input/output error")
        return real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    assert run(work, archive, tmp_path, [full(work)]).published == 1
    assert (archive / "fpga-library.json").is_file()


def test_only_with_allow_removals_is_refused_and_writes_nothing(work, archive, tmp_path, monkeypatch, capsys):
    ds = [full(work, "tt05/tt_um_a"), full(work, "tt05/tt_um_b")]
    args = ["--archive", str(archive), "--check-cmd", checker(tmp_path)]
    assert cli(monkeypatch, ds, [*args, "--only", "tt05/tt_um_a", "--allow-removals"]) == 1
    assert "--only" in capsys.readouterr().err
    assert list(archive.iterdir()) == []


def test_a_clean_tree_publishes_without_a_warning(work, archive, tmp_path, monkeypatch, capsys):
    assert cli(monkeypatch, [full(work)], ["--archive", str(archive), "--check-cmd", checker(tmp_path)]) == 0
    assert "dirty" not in capsys.readouterr().out


def test_a_dirty_tree_is_refused_unless_allowed(work, archive, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(publish, "_tree_is_dirty", lambda: True)
    ds = [full(work)]
    args = ["--archive", str(archive), "--check-cmd", checker(tmp_path)]
    assert cli(monkeypatch, ds, args) == 1
    assert "uncommitted" in capsys.readouterr().err
    assert list(archive.iterdir()) == []
    assert cli(monkeypatch, ds, [*args, "--allow-dirty"]) == 0
    assert "WARNING: tthfv_commit does not describe a dirty tree" in capsys.readouterr().out
    assert (archive / "fpga-library.json").is_file()


def test_the_dirty_check_asks_git_for_tracked_changes_only(monkeypatch):
    asked = []
    monkeypatch.setattr(manifest, "_git", lambda *a, **k: asked.append(a) or " M tthfv/publish.py\n")
    assert publish._tree_is_dirty() is True
    assert asked == [("status", "--porcelain", "--untracked-files=no")]
    monkeypatch.setattr(manifest, "_git", lambda *a, **k: "")
    assert publish._tree_is_dirty() is False


def test_a_completely_consistent_set_is_published(work, archive, tmp_path):
    d = full(work, ui_in=1, ref={"ui_in_requested": 1, "ui_in_mask_used": 0xF1, "uio_oe_any": 0x04},
             ideal={"ui_in_requested": 1})  # fmt: skip
    s = run(work, archive, tmp_path, [d])
    assert s.skipped == [] and s.published == 1
    assert doc(archive)["designs"][0]["ui_in_mask"] == 0xF1


@pytest.mark.parametrize("which", ["ideal", "ref"])
def test_a_reference_simulated_for_fewer_clocks_than_the_default_is_refused(work, archive, tmp_path, which):
    d = full(work)
    put_ref(work, d, "ideal" if which == "ideal" else "fpga-4")
    path = work / "reference" / d.id / ("ideal" if which == "ideal" else "fpga-4") / "reference.json"
    rec = json.loads(path.read_text())
    rec["key"]["clocks"] = 100_000
    path.write_text(json.dumps(rec))
    assert reason_of(work, archive, tmp_path, d) == (
        f"reference simulated for 100000 clocks, not the default {reference.DEFAULT_CLOCKS}"
    )


def test_a_mask_that_drives_a_tied_pin_the_design_enables_is_refused(work, archive, tmp_path):
    d = full(work, ref={"uio_oe_any": 0b0010, "ui_in_mask_used": 0xFF})  # uio[1] is tied to ui_in[1]
    assert reason_of(work, archive, tmp_path, d) == "mask drives a tied pin the design enables"


def test_a_mask_that_leaves_out_the_tied_pin_is_published(work, archive, tmp_path):
    d = full(work, ref={"uio_oe_any": 0b0010, "ui_in_mask_used": 0xFD})
    assert run(work, archive, tmp_path, [d]).published == 1


@pytest.mark.parametrize("bad", [-1, 256, True, None, "2"])
def test_an_unusable_uio_oe_any_is_refused(work, archive, tmp_path, bad):
    d = full(work, ref={"uio_oe_any": bad})
    assert reason_of(work, archive, tmp_path, d) == "mask drives a tied pin the design enables"


def edit_key(work, d, profile: str, **changes) -> None:
    path = work / "reference" / d.id / profile / "reference.json"
    rec = json.loads(path.read_text())
    rec["key"].update(changes)
    path.write_text(json.dumps(rec))


@pytest.mark.parametrize("profile", ["ideal", "fpga-4"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("testbench_sha256", "0" * 64),
        ("stimulus_version", 0),
        ("cutter_version", -1),
        ("verilator_flags", ["--other"]),
        ("base_flags", ["--other"]),
    ],
)
def test_a_reference_made_by_other_simulation_code_is_stale(work, archive, tmp_path, profile, field, value):
    d = full(work)
    edit_key(work, d, profile, **{field: value})
    assert reason_of(work, archive, tmp_path, d) == "stale reference (simulation code changed)"


def test_a_reference_made_with_another_verilator_version_is_still_published(work, archive, tmp_path):
    d = full(work)
    edit_key(work, d, "fpga-4", verilator="Verilator 9.999 (a tool upgrade alone must not unpublish)")
    assert run(work, archive, tmp_path, [d]).published == 1


@pytest.mark.parametrize("profile", ["ideal", "fpga-4"])
@pytest.mark.parametrize(
    ("field", "value"), [("sources_sha256", "0" * 64), ("top_module_used", "tt_um_other")]
)
def test_a_reference_made_from_other_sources_than_the_bitstream_is_refused(
    work, archive, tmp_path, profile, field, value
):
    d = full(work)
    edit_key(work, d, profile, **{field: value})
    assert reason_of(work, archive, tmp_path, d) == "bitstream and reference were made from different sources"


def test_a_fetch_record_of_other_sources_than_the_bitstream_is_refused(work, archive, tmp_path):
    d = full(work)
    put_fetch(work, d, sources_sha256="0" * 64)
    assert reason_of(work, archive, tmp_path, d) == "bitstream and reference were made from different sources"


def test_a_build_whose_top_module_differs_from_the_references_is_refused(work, archive, tmp_path):
    d = full(work, build={"top_module_used": "tt_um_other"})
    assert reason_of(work, archive, tmp_path, d) == "bitstream and reference were made from different sources"


def test_without_a_fetch_record_the_build_cannot_be_verified(work, archive, tmp_path):
    d = full(work)
    (work / "src" / d.id / "fetch.json").unlink()
    assert reason_of(work, archive, tmp_path, d) == "sources not available to verify the build"


def test_a_build_with_another_inputs_digest_is_refused(work, archive, tmp_path):
    d = full(work, build={"inputs_sha256": "0" * 64})
    assert reason_of(work, archive, tmp_path, d) == "bitstream built from different inputs than today's"


def test_a_build_without_an_inputs_digest_is_refused(work, archive, tmp_path):
    d = full(work)
    path = work / "build" / d.id / "build.json"
    rec = json.loads(path.read_text())
    del rec["inputs_sha256"]
    path.write_text(json.dumps(rec))
    assert reason_of(work, archive, tmp_path, d) == "bitstream built from different inputs than today's"


def test_a_build_made_with_an_older_harness_pin_file_is_refused(work, archive, tmp_path, monkeypatch):
    d = full(work)
    harness = tmp_path / "harness"
    shutil.copytree(build_stage.HARNESS, harness)
    with (harness / "tt_fpga_fabricfox.pcf").open("a") as f:
        f.write("# a changed pin file\n")
    monkeypatch.setattr(build_stage, "HARNESS", harness)
    assert reason_of(work, archive, tmp_path, d) == "bitstream built from different inputs than today's"


def test_a_build_made_with_an_older_top_wrapper_is_refused(work, archive, tmp_path, monkeypatch):
    d = full(work)
    harness = tmp_path / "harness"
    shutil.copytree(build_stage.HARNESS, harness)
    with (harness / "tt_fpga_top.v").open("a") as f:
        f.write("// a changed wrapper\n")
    monkeypatch.setattr(build_stage, "HARNESS", harness)
    assert reason_of(work, archive, tmp_path, d) == "bitstream built from different inputs than today's"


def test_a_manifest_clock_changed_since_the_build_is_refused(work, archive, tmp_path):
    d = full(work)
    changed = dataclasses.replace(d, clock_hz=50_000_000)
    assert reason_of(work, archive, tmp_path, changed) == "bitstream built from different inputs than today's"


def test_cli_failures_exit_1_with_a_message(work, archive, tmp_path, monkeypatch, capsys):
    ds = [full(work)]
    assert cli(monkeypatch, ds, ["--archive", str(archive), "--check-cmd", checker(tmp_path, 1)]) == 1
    assert "checker says hello" in capsys.readouterr().err
    assert cli(monkeypatch, ds, ["--archive", str(tmp_path / "nope"), "--check-cmd", checker(tmp_path)]) == 1
    assert "archive" in capsys.readouterr().err
    assert cli(monkeypatch, ds, ["--archive", str(archive), "--check-cmd", "  "]) == 1
    assert "check-cmd" in capsys.readouterr().err


@pytest.mark.parametrize("exc", [KeyError("targets"), TypeError("bad"), ValueError("bad")])
def test_a_malformed_manifest_exits_1_with_a_message(work, archive, tmp_path, monkeypatch, capsys, exc):
    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(manifest, "load", boom)
    assert main(["publish", "--archive", str(archive), "--check-cmd", checker(tmp_path)]) == 1
    assert "manifest" in capsys.readouterr().err


def test_the_archive_path_is_not_written_anywhere_in_the_index(work, archive, tmp_path):
    run(work, archive, tmp_path, [full(work)])
    assert str(archive) not in (archive / "fpga-library.json").read_text()


# ---- coverage: a deeply nested previous index, PDK latch cells


def test_a_deeply_nested_previous_index_is_a_user_error_not_a_traceback(work, archive, tmp_path):
    text = '{"schema": 1, "designs": ' + "[" * 200_000 + "]" * 200_000 + "}"
    (archive / "fpga-library.json").write_text(text)
    with pytest.raises(UserError, match="fpga-library.json"):
        run(work, archive, tmp_path, [full(work)], allow_removals=True)
    assert (archive / "fpga-library.json").read_text() == text
    assert not (archive / "tt05").exists()


def only_entry(archive: Path) -> dict:
    (e,) = doc(archive)["designs"]
    return e


def test_a_build_with_pdk_latch_cells_is_published_with_pdk_latches_true(work, archive, tmp_path):
    d = full(work, build={"pdk_cells": ["sky130_fd_sc_hd__dlxtp_1"], "pdk_latches": True})
    assert run(work, archive, tmp_path, [d]).published == 1
    assert only_entry(archive)["pdk_latches"] is True


def test_a_build_without_pdk_cells_is_published_with_pdk_latches_false(work, archive, tmp_path):
    assert run(work, archive, tmp_path, [full(work)]).published == 1
    assert only_entry(archive)["pdk_latches"] is False


def test_a_build_with_pdk_cells_and_no_latches_is_published_with_pdk_latches_false(work, archive, tmp_path):
    d = full(work, build={"pdk_cells": ["sky130_fd_sc_hd__inv_1"], "pdk_latches": False})
    assert run(work, archive, tmp_path, [d]).published == 1
    assert only_entry(archive)["pdk_latches"] is False


def test_pdk_latches_is_a_json_boolean_in_the_file_after_fmax_hz(work, archive, tmp_path):
    a = full(work, "tt05/tt_um_a", build={"pdk_cells": ["sky130_fd_sc_hd__dlxtp_1"], "pdk_latches": True})
    b = full(work, "tt05/tt_um_b")
    run(work, archive, tmp_path, [a, b])
    text = (archive / "fpga-library.json").read_text()
    assert '"pdk_latches": true' in text and '"pdk_latches": false' in text
    for e in doc(archive)["designs"]:
        keys = list(e)
        assert keys[keys.index("fmax_hz") + 1] == "pdk_latches"
        assert type(e["pdk_latches"]) is bool


@pytest.mark.parametrize("latches", [None, "no", 0, 1, "true"])
def test_pdk_cells_with_pdk_latches_missing_or_not_a_bool_are_published_as_true(
    work, archive, tmp_path, latches
):
    build = {"pdk_cells": ["sky130_fd_sc_hd__inv_1"]}
    if latches is not None:
        build["pdk_latches"] = latches
    d = full(work, build=build)
    assert run(work, archive, tmp_path, [d]).published == 1
    assert only_entry(archive)["pdk_latches"] is True


def test_publishing_over_an_index_without_pdk_latches_adds_the_key_and_rewrites_no_bin(
    work, archive, tmp_path
):
    a, b = two_published(work, archive, tmp_path)
    index = archive / "fpga-library.json"
    old = doc(archive)
    for e in old["designs"]:
        e.pop("pdk_latches", None)  # the live index of today has no such key
    index.write_text(json.dumps(old, indent=2) + "\n")
    bins = [archive / e["bin"] for e in old["designs"]]
    before = [(p.stat().st_ino, p.stat().st_mtime_ns) for p in bins]
    s = run(work, archive, tmp_path, [a, b])  # no allow_removals
    assert s.removed == [] and s.published == 2
    assert all("pdk_latches" in e and e["pdk_latches"] is False for e in doc(archive)["designs"])
    assert [(p.stat().st_ino, p.stat().st_mtime_ns) for p in bins] == before


def test_a_latch_design_that_needs_qspi_is_still_skipped_for_qspi(work, archive, tmp_path):
    d = full(work, build={"pdk_cells": ["sky130_fd_sc_hd__dlxtp_1"], "pdk_latches": True}, qspi="uio[1]")
    assert reason_of(work, archive, tmp_path, d) == "needs a QSPI Pmod"


def test_a_recovered_build_whose_digest_includes_its_pdk_models_and_globals_reset_is_published(
    work, archive, tmp_path
):
    d = full(work, build={"pdk_cells": ["sky130_fd_sc_hd__inv_1"], "pdk_latches": False, "reset_vlog": True})
    assert run(work, archive, tmp_path, [d]).published == 1


def test_a_recovered_build_whose_pdk_plan_differs_from_todays_is_skipped(work, archive, tmp_path):
    d = full(work, build={"pdk_cells": ["sky130_fd_sc_hd__inv_1"], "pdk_latches": False})
    rec = json.loads((work / "build" / d.id / "build.json").read_text())
    rec["pdk_cells"] = ["sky130_fd_sc_hd__buf_1"]  # the digest was made with another plan
    (work / "build" / d.id / "build.json").write_text(json.dumps(rec))
    assert reason_of(work, archive, tmp_path, d) == "bitstream built from different inputs than today's"
    rec["pdk_cells"] = ["sky130_fd_sc_hd__no_such_cell_1"]  # a cell with no model today
    (work / "build" / d.id / "build.json").write_text(json.dumps(rec))
    assert reason_of(work, archive, tmp_path, d) == "the build's PDK cell models are not today's"


def test_a_recovered_build_without_its_globals_reset_flag_is_skipped(work, archive, tmp_path):
    d = full(work, build={"reset_vlog": True})
    rec = json.loads((work / "build" / d.id / "build.json").read_text())
    del rec["reset_vlog"]
    (work / "build" / d.id / "build.json").write_text(json.dumps(rec))
    assert reason_of(work, archive, tmp_path, d) == "bitstream built from different inputs than today's"


def with_reference_pdk_entry(work: Path, d, entry: dict) -> None:
    """Give every reference of d the key entry a reference made with PDK cell models carries."""
    for path in (work / "reference" / d.id).glob("*/reference.json"):
        rec = json.loads(path.read_text())
        rec["key"]["pdk_cells"] = entry
        path.write_text(json.dumps(rec))


def test_a_reference_made_with_todays_pdk_models_is_published(work, archive, tmp_path):
    d = full(work)
    with_reference_pdk_entry(work, d, pdkcells.plan_named(["sky130_fd_sc_hd__inv_1"]).key_fields())
    assert run(work, archive, tmp_path, [d]).published == 1


def test_a_reference_whose_pdk_model_hash_is_not_todays_is_skipped(work, archive, tmp_path):
    d = full(work)
    entry = {**pdkcells.plan_named(["sky130_fd_sc_hd__inv_1"]).key_fields(), "pdk_cells_sha256": "0" * 64}
    with_reference_pdk_entry(work, d, entry)
    assert reason_of(work, archive, tmp_path, d) == "stale reference (PDK cell models changed)"


@pytest.mark.parametrize(
    "entry",
    [{"pdk_cells": ["sky130_fd_sc_hd__no_such_cell_1"], "pdk_cells_sha256": "0" * 64}, ["not", "a", "dict"]],
)
def test_a_reference_naming_a_cell_without_a_model_today_is_skipped(work, archive, tmp_path, entry):
    d = full(work)
    with_reference_pdk_entry(work, d, entry)
    assert reason_of(work, archive, tmp_path, d) == "stale reference (PDK cell models changed)"


# ---- hardware record: validation "mcu" -------------------------------------------------------------------


def hw_record(id: str = "tt05_a", sha: str | None = None, verdict: str = "exact", **kw) -> dict:
    cause = None if verdict == "exact" else "synthesis differs"
    rec = {
        "bin_sha256": sha or hashlib.sha256(BIN).hexdigest(), "board": "fpga-4", "cause": cause,
        "date": "2026-11-27", "vgacap_revision": "2" * 40, "verdict": verdict,
    }  # fmt: skip
    rec.update(kw)
    return {"schema": 1, "records": {id: rec}}


def put_hw(tmp_path, monkeypatch, content) -> Path:
    p = tmp_path / "hw-record.json"
    p.write_text(content if isinstance(content, str) else json.dumps(content))
    monkeypatch.setattr(publish, "HARDWARE_RECORD", p)
    return p


def validation_of(work, archive, tmp_path) -> str:
    run(work, archive, tmp_path, [full(work)])
    return doc(archive)["designs"][0]["validation"]


def test_exact_with_the_same_bin_sha256_is_mcu(work, archive, tmp_path, monkeypatch):
    put_hw(tmp_path, monkeypatch, hw_record())
    assert validation_of(work, archive, tmp_path) == "mcu"


def test_exact_with_another_bin_sha256_is_none(work, archive, tmp_path, monkeypatch):
    put_hw(tmp_path, monkeypatch, hw_record(sha="0" * 64))
    assert validation_of(work, archive, tmp_path) == "none"


@pytest.mark.parametrize("verdict", ["mismatch", "no-match"])
def test_a_non_exact_verdict_is_none(work, archive, tmp_path, monkeypatch, verdict):
    put_hw(tmp_path, monkeypatch, hw_record(verdict=verdict))
    assert validation_of(work, archive, tmp_path) == "none"


def test_no_record_for_the_id_is_none(work, archive, tmp_path, monkeypatch):
    put_hw(tmp_path, monkeypatch, hw_record(id="tt05_other"))
    assert validation_of(work, archive, tmp_path) == "none"


@pytest.mark.parametrize(
    "content",
    [
        "", "not json", "[]", '{"schema": 1}', '{"schema": 2, "records": {}}', '{"schema": 1, "records": []}',
        '{"schema": 1, "records": {"a": {}}}', "NaN",
        json.dumps(hw_record(verdict="bogus")), json.dumps(hw_record(sha="abc")),
        json.dumps(hw_record(cause="synthesis differs")),  # a cause on an exact record
        json.dumps(hw_record(verdict="mismatch", cause=None)),
        json.dumps(hw_record(board="fpga-5")), json.dumps(hw_record(extra=1)),
    ],
)  # fmt: skip
def test_a_malformed_record_file_fails_the_run_and_writes_nothing(
    work, archive, tmp_path, monkeypatch, content
):
    put_hw(tmp_path, monkeypatch, content)
    d = full(work)
    with pytest.raises(UserError, match="hardware record"):
        run(work, archive, tmp_path, [d])
    assert not (archive / "fpga-library.json").exists()


def test_a_missing_record_file_fails_the_run(work, archive, tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "HARDWARE_RECORD", tmp_path / "absent.json")
    with pytest.raises(UserError, match="hardware record"):
        run(work, archive, tmp_path, [full(work)])


def test_check_entry_allows_none_and_mcu_only(work, archive, tmp_path):
    e = a_valid_entry(work, archive, tmp_path)
    publish._check_entry({**e, "validation": "mcu"})
    for bad in ("full-speed", "", "yes", None):
        with pytest.raises(publish.Skip, match="validation"):
            publish._check_entry({**e, "validation": bad})


def make_record_fixture(tmp_path):
    hw = tmp_path / "hw"
    shas = {}
    for key, verdict, started in (
        ("tt05/tt_um_a", "exact", "2026-11-27T09:00:00Z"),
        ("tt05/tt_um_b", "mismatch", "2026-11-27T10:00:00Z"),
        ("tt05/tt_um_c", "no-match", "2026-11-27T11:00:00Z"),
    ):
        d = hw / key.replace("/", "__")
        (d / "result").mkdir(parents=True)
        sha = hashlib.sha256(key.encode()).hexdigest()
        shas[key] = sha
        (d / "design.json").write_text(
            json.dumps({"key": key, "bin_sha256": sha, "board": "fpga-4", "started": started})
        )
        (d / "result" / "result.json").write_text(json.dumps({"verdict": verdict}))
    (hw / "tt05__tt_um_b.attempt-1").mkdir()  # an earlier capture is ignored
    clog = tmp_path / "classify.log"
    clog.write_text(
        "2 designs\n"
        'tt05/tt_um_b: synthesis differs from RTL  {"rtl": "1diff", "net": "exact"}  (1 captures)\n'
        'tt05/tt_um_c: unexplained  {"rtl": "1diff"}  (2 captures)\n'
    )
    return hw, clog, shas


MAKE_RECORD = paths.REPO_ROOT / "scripts" / "hardware_library" / "make_record.py"


def test_make_record_on_a_tiny_fixture(tmp_path):
    hw, clog, shas = make_record_fixture(tmp_path)
    out = tmp_path / "out" / "fpga-4.json"
    out.parent.mkdir()
    r = subprocess.run(
        [sys.executable, str(MAKE_RECORD), str(hw), str(clog), str(out)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    text = out.read_text()
    data = json.loads(text)
    assert text == json.dumps(data, indent=2, sort_keys=True) + "\n"
    assert set(data) == {"records", "schema"} and data["schema"] == 1
    rev = "2a1caa703dc0196feac0f9fd5d7f6dfcc443f892"

    def rec(key, verdict, cause):
        return {
            "bin_sha256": shas[key], "board": "fpga-4", "cause": cause, "date": "2026-11-27",
            "vgacap_revision": rev, "verdict": verdict,
        }  # fmt: skip

    assert data["records"] == {
        "tt05_a": rec("tt05/tt_um_a", "exact", None),
        "tt05_b": rec("tt05/tt_um_b", "mismatch", "synthesis differs"),
        "tt05_c": rec("tt05/tt_um_c", "no-match", "unexplained"),
    }
    assert "/home" not in text and str(tmp_path) not in text
    publish.load_hardware_record(out)  # what publish reads accepts what the script writes


def test_make_record_refuses_a_non_exact_design_without_a_cause(tmp_path):
    hw, clog, _ = make_record_fixture(tmp_path)
    clog.write_text("0 designs\n")
    out = tmp_path / "o.json"
    r = subprocess.run(
        [sys.executable, str(MAKE_RECORD), str(hw), str(clog), str(out)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0 and not out.exists()
