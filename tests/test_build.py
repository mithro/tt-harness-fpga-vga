# SPDX-License-Identifier: Apache-2.0
"""Build stage tests. The toolchain is replaced by fake executables written under tmp_path."""

import dataclasses
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from tthfv import build, fetch, paths
from tthfv.fetch import FetchResult
from tthfv.manifest import Design

LOGS = Path(__file__).parent / "data" / "logs"

YOSYS = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "Yosys fake 1.0"; exit 0; fi
echo run >> "$FAKE_REC/yosys.count"
pwd > "$FAKE_REC/yosys.cwd"
echo "$@" >> "$FAKE_REC/yosys.args"
if [ -n "$FAKE_YOSYS_MISSING" ]; then echo "ERROR: Module foo referenced in module top is not part of the design."; exit 1; fi
if [ -n "$FAKE_YOSYS_PDK" ] && ! grep -q 'pdk_cells[.]v' "$2"; then
  printf '%s\\n' "$FAKE_YOSYS_PDK"; exit 1
fi
if [ -n "$FAKE_YOSYS_LOOP" ] && grep -q 'pdk_cells[.]v' "$2"; then
  echo "ERROR: Assertion failed: selection is not empty: @tthfv_pdk_loops a:src=*pdk_cells.v* %i"; exit 1
fi
if [ -n "$FAKE_YOSYS_TOOLCHAIN" ] && ! grep -q '^design -reset-vlog$' "$2"; then
  printf '%s\\n' "$FAKE_YOSYS_TOOLCHAIN/lib/../share/yosys/ice40/cells_sim.v:279: ERROR: Specify DAT width does not match DST width."; exit 1
fi
if [ -n "$FAKE_YOSYS_SVMODE" ]; then
  grep -q 'read_verilog -sv.*a[.]v' "$2" || { echo "a.v:3: ERROR: $FAKE_YOSYS_SVMODE"; exit 1; }
fi
if [ -n "$FAKE_YOSYS_NEEDS_SV" ]; then
  grep -q 'read_verilog -sv.*a[.]v' "$2" || { echo "ERROR: syntax error, unexpected TOK_ID"; exit 1; }
fi
if [ -n "$FAKE_YOSYS_FAIL" ]; then
  case "$(cat "$2")" in *sv2v.v*) ;; *) echo "ERROR: syntax error near foo"; exit 1;; esac
fi
j=$(sed -nE 's/.*-json "([^"]*)".*/\\1/p' "$2"); [ -n "$j" ] && echo '{}' > "$j"
st=$(sed -nE 's/^tee -q -o "([^"]*)" stat -json.*/\\1/p' "$2")
if [ -n "$st" ]; then
  if [ -n "$FAKE_STAT_BAD" ]; then echo "$FAKE_STAT_BAD" > "$st"; else "$FAKE_PYTHON" "$FAKE_HELPER" stat "$st" "$FAKE_YOSYS_LOG"; fi
fi
cat "$FAKE_YOSYS_LOG"
"""

NEXTPNR = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "nextpnr-ice40 fake 1.0"; exit 0; fi
[ -n "$FAKE_PNR_SLEEP" ] && sleep "$FAKE_PNR_SLEEP"
echo "$@" >> "$FAKE_REC/nextpnr.args"
seed=; json=; asc=; report=; v=
while [ $# -gt 0 ]; do
  case "$1" in --seed) seed=$2;; --json) json=$2;; --asc) asc=$2;; --freq) freq=$2;; --report) report=$2;; esac
  shift
done
synth=$(basename "$json" .json); synth=${synth#out-}
key="$synth/$seed"
echo "$key" >> "$FAKE_REC/pnr.order"
echo "asc $key" > "$asc"
for k in $(echo "$FAKE_NOFIT" | tr ',' ' '); do
  if [ "$k" = "$key" ]; then
    echo "Info: Device utilisation:"
    echo "Info: 	         ICESTORM_LC:    9000/   5280   170%"
    echo "ERROR: Unable to place cell"
    exit 255
  fi
done
if [ -n "$FAKE_FMAX" ]; then
  v=10.00
  for kv in $(echo "$FAKE_FMAX" | tr ',' ' '); do
    [ "${kv%%=*}" = "$key" ] && v=${kv#*=}
  done
  echo "Info: Device utilisation:"
  echo "Info: 	         ICESTORM_LC:     824/   5280    15%"
  verdict=$(awk -v v="$v" -v f="$freq" 'BEGIN { print (v + 0 >= f + 0) ? "PASS" : "FAIL" }')
  if [ -n "$FAKE_SIDE" ]; then
    sv=${FAKE_SIDE#*=}; sname=${FAKE_SIDE%%=*}
    sverdict=$(awk -v v="$sv" -v f="$freq" 'BEGIN { print (v + 0 >= f + 0) ? "PASS" : "FAIL" }')
    echo "Info: Max frequency for clock '$sname': $sv MHz ($sverdict at $freq MHz)"
    echo "Info: Max frequency for clock        'clk\\$SB_IO_IN_\\$glb_clk': $v MHz ($verdict at $freq MHz)"
  else
    echo "Info: Max frequency for clock 'clk\\$SB_IO_IN_\\$glb_clk': $v MHz ($verdict at $freq MHz)"
  fi
else
  awk -v f="$freq" '/Max frequency for clock/ { if (match($0, /: [0-9.]+ MHz/)) { v = substr($0, RSTART + 2, RLENGTH - 6) + 0; sub(/[(](PASS|FAIL) at/, "(" ((v >= f + 0) ? "PASS" : "FAIL") " at") } } { print }' "$FAKE_PNR_LOG"
fi
if [ -n "$report" ] && [ "${FAKE_PNR_EXIT:-0}" = 0 ]; then
  if [ -n "$FAKE_REPORT_RAW" ]; then printf '%s' "$FAKE_REPORT_RAW" > "$report"
  elif [ -z "$FAKE_NO_REPORT" ]; then "$FAKE_PYTHON" "$FAKE_HELPER" nextpnr "$report" "$freq" "$v" "$FAKE_SIDE" "$FAKE_PNR_LOG"; fi
fi
exit ${FAKE_PNR_EXIT:-0}
"""

ICEPACK = """#!/bin/sh
if [ -n "$FAKE_ICEPACK_FAIL" ]; then echo "icepack: boom"; exit 1; fi
if [ -n "$FAKE_BAD_BIN" ]; then printf 'nonsense' > "$2"; exit 0; fi
printf '\\377\\000\\000\\377\\176\\252\\231\\176payload' > "$2"
cat "$1" >> "$2"
"""

SV2V = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "sv2v fake 1.0"; exit 0; fi
echo "$@" > "$FAKE_REC/sv2v.args"
out=
for a in "$@"; do case "$a" in --write=*) out=${a#--write=};; esac; done
[ -n "$out" ] && { echo "module converted; endmodule" > "$out"; }
exit 0
"""


def fake_tool(bin_dir: Path, name: str, text: str) -> None:
    p = bin_dir / name
    p.write_text(text)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def toolchain(tmp_path, monkeypatch):
    bin_dir = tmp_path / "tools" / "oss-cad-suite" / "bin"
    bin_dir.mkdir(parents=True)
    for name, text in (("yosys", YOSYS), ("nextpnr-ice40", NEXTPNR), ("icepack", ICEPACK)):
        fake_tool(bin_dir, name, text)
    (bin_dir / "verilator").write_text("")
    rec = tmp_path / "rec"
    rec.mkdir()
    monkeypatch.setenv("TTHFV_TOOLS", str(tmp_path / "tools"))
    monkeypatch.setenv("FAKE_REC", str(rec))
    monkeypatch.setenv("FAKE_YOSYS_LOG", str(LOGS / "yosys.log"))
    monkeypatch.setenv("FAKE_PNR_LOG", str(LOGS / "nextpnr.log"))
    monkeypatch.setenv("FAKE_PYTHON", sys.executable)
    monkeypatch.setenv("FAKE_HELPER", str(Path(__file__).parent / "fake_report.py"))
    for var in (
        "FAKE_FMAX",
        "FAKE_SIDE",
        "FAKE_NOFIT",
        "FAKE_PNR_SLEEP",
        "FAKE_YOSYS_FAIL",
        "FAKE_YOSYS_NEEDS_SV",
        "FAKE_YOSYS_MISSING",
        "FAKE_YOSYS_PDK",
        "FAKE_YOSYS_LOOP",
        "FAKE_YOSYS_SVMODE",
        "FAKE_YOSYS_TOOLCHAIN",
        "FAKE_PNR_EXIT",
        "FAKE_ICEPACK_FAIL",
        "FAKE_BAD_BIN",
        "FAKE_STAT_BAD",
        "FAKE_REPORT_RAW",
        "FAKE_NO_REPORT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(paths, "WORK", tmp_path / "work")
    return bin_dir


@pytest.fixture
def rec(toolchain, tmp_path) -> Path:
    return tmp_path / "rec"


def make_design(**kw) -> Design:
    # The real nextpnr fixture log reports 22.04 MHz, so 20 MHz passes and 25.175 MHz fails.
    base = Design(
        id="tt99/tt_um_x", shuttle="tt99", macro="tt_um_x", title="", author="", repo="",
        commit="", top_module="tt_um_x", source_files=("a.v",), clock_hz=20_000_000,
        clock_declared=True, pinout={}, pmods=(), tiles="1x1", render_verdict=None, override={},
        patch=None, qspi=None, ui_in=0, uio_in=0, scripted=False, candidate=False,
    )  # fmt: skip
    return dataclasses.replace(base, **kw)


def make_fetched(tmp_path: Path, names=("a.v",), design: Design | None = None) -> FetchResult:
    """The fetch stage's resolution of a checkout under tmp_path/repo. Files that already exist are kept,
    so a test can edit one and call this again to get the new digest."""
    src_dir = tmp_path / "repo"
    src = src_dir / "src"
    src.mkdir(parents=True, exist_ok=True)
    for n in names:
        if not (src / n).exists():
            hdl = n.endswith((".v", ".sv"))
            (src / n).write_text("module tt_um_x;\nendmodule\n" if hdl else f"// {n}\n")
    r = fetch.resolve(design or make_design(source_files=names), src_dir, src_dir, tmp_path / "fetch.log")
    assert r.status == "ok", r.reason
    return r


def run(tmp_path, names=("a.v",), force=False, max_attempts=8, **kw):
    d = make_design(source_files=names, **kw)
    return d, build.build(d, make_fetched(tmp_path, names, d), force=force, max_attempts=max_attempts)


def test_ok_build_records_everything(toolchain, rec, tmp_path):
    d, r = run(tmp_path)
    assert r.status == "ok", r.reason
    out = paths.design_dir("build", d.id)
    bin_path = out / r.bitstream
    assert b"payload" in bin_path.read_bytes()
    assert r.bitstream_sha256 == hashlib.sha256(bin_path.read_bytes()).hexdigest()
    assert r.clock_hz == 20_000_000 and r.fmax_hz == 22_040_000
    assert r.cells["SB_LUT4"] == 761 and r.cells["SB_DFF"] == 69
    assert r.lut_percent == pytest.approx(15.6)
    assert r.tools["yosys"] == "Yosys fake 1.0" and r.tools["nextpnr-ice40"] == "nextpnr-ice40 fake 1.0"
    assert r.tools["icepack"].startswith("icepack (no --version flag")
    assert r.seconds >= 0
    on_disk = json.loads((out / "build.json").read_text())
    assert on_disk["id"] == d.id and on_disk["status"] == "ok"
    assert on_disk["bitstream_sha256"] == r.bitstream_sha256
    assert (out / "yosys-default.log").exists() and (out / "nextpnr-default-1.log").exists()
    assert not list(out.glob("*.tmp"))


def test_tool_command_lines(toolchain, rec, tmp_path):
    d, _r = run(tmp_path, clock_hz=25_175_000, max_attempts=1)
    out = paths.design_dir("build", d.id).resolve()
    pnr = (rec / "nextpnr.args").read_text().split()
    for flag in ("--up5k", "--pcf-allow-unconstrained", "--timing-allow-fail"):
        assert flag in pnr
    assert pnr[pnr.index("--package") + 1] == "sg48"
    assert pnr[pnr.index("--freq") + 1] == "25.175"
    assert pnr[pnr.index("--seed") + 1] == "1"
    assert pnr[pnr.index("--json") + 1] == str(out / "scratch" / "out-default.json")
    assert pnr[pnr.index("--asc") + 1].endswith(".asc")
    assert pnr[pnr.index("--pcf") + 1].endswith("harness/tt_fpga_fabricfox.pcf")
    ys = (paths.design_dir("build", d.id).resolve() / "yosys-default.ys").read_text()
    assert "synth_ice40 -top tt_fpga_top -json" in ys
    assert f"-I {tmp_path / 'repo' / 'src'}" in ys or f'-I "{tmp_path / "repo" / "src"}"' in ys
    # yosys runs in the design's src directory, and the wrapper names the design's top module.
    assert (rec / "yosys.cwd").read_text().strip() == str(tmp_path / "repo" / "src")
    wrapper = (out / "tt_fpga_top.v").read_text()
    assert "tt_um_x user_project" in wrapper and "__tt_um_placeholder" not in wrapper


def test_systemverilog_sources_use_sv_reader(toolchain, rec, tmp_path):
    run(tmp_path, names=("a.sv", "b.v"))
    ys = (paths.design_dir("build", "tt99/tt_um_x").resolve() / "yosys-default.ys").read_text()
    assert "read_verilog -sv" in ys and ys.count("read_verilog") >= 2


def test_yosys_failure_is_synth_fail(toolchain, rec, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_YOSYS_FAIL", "1")
    d, r = run(tmp_path)
    assert r.status == "synth-fail"
    assert "syntax error near foo" in r.reason
    assert r.bitstream is None and r.sv2v is False
    assert not (rec / "nextpnr.args").exists()
    assert json.loads((paths.design_dir("build", d.id) / "build.json").read_text())["status"] == "synth-fail"


def test_plain_verilog_with_sv_constructs_is_retried_with_the_sv_reader(
    toolchain, rec, tmp_path, monkeypatch
):
    # Tiny Tapeout's own flow reads every file with -sv, so a .v file using `>>=` must still build.
    monkeypatch.setenv("FAKE_YOSYS_NEEDS_SV", "1")
    d, r = run(tmp_path, names=("a.v",))
    assert r.status == "ok", r.reason
    assert (rec / "yosys.count").read_text().split() == ["run", "run"]  # plain read first, then -sv
    ys = (paths.design_dir("build", d.id).resolve() / "yosys-default.ys").read_text()
    assert "read_verilog -sv" in ys and "a.v" in ys
    assert r.sv2v is False and r.sv_reader is True


def test_sv_reader_retry_is_used_for_later_variants_without_a_third_run(
    toolchain, rec, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_YOSYS_NEEDS_SV", "1")
    monkeypatch.setenv("FAKE_FMAX", "default/1=10.0,default/2=10.0,default/3=10.0,default/4=10.0")
    _d, r = run(tmp_path, names=("a.v",), clock_hz=20_000_000)
    assert r.status == "timing-fail"
    # default: plain (fail) + -sv; nodffe goes straight to -sv
    assert (rec / "yosys.count").read_text().split() == ["run"] * 3


def test_synth_fail_retries_through_sv2v_once(toolchain, rec, tmp_path, monkeypatch):
    fake_tool(toolchain, "sv2v", SV2V)
    monkeypatch.setenv("FAKE_YOSYS_FAIL", "1")
    _d, r = run(tmp_path, names=("a.sv",))
    assert r.status == "ok", r.reason
    assert r.sv2v is True
    assert (rec / "yosys.count").read_text().split() == ["run", "run"]
    assert "a.sv" in (rec / "sv2v.args").read_text()


def test_no_sv2v_means_no_retry(toolchain, rec, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_YOSYS_FAIL", "1")
    monkeypatch.setenv("PATH", str(toolchain.parent))  # no sv2v anywhere on PATH
    _, r = run(tmp_path)
    assert r.status == "synth-fail" and r.sv2v is False
    assert (rec / "yosys.count").read_text().split() == ["run", "run"]  # plain, then -sv; no sv2v
    assert not (rec / "sv2v.args").exists()


def test_nextpnr_over_capacity_is_no_fit(toolchain, tmp_path, monkeypatch):
    log = tmp_path / "pnr.log"
    log.write_text(
        "Info: Device utilisation:\nInfo: \t ICESTORM_LC: 9000/ 5280 170%\nERROR: Unable to place\n"
    )
    monkeypatch.setenv("FAKE_PNR_LOG", str(log))
    monkeypatch.setenv("FAKE_PNR_EXIT", "255")
    _, r = run(tmp_path)
    assert r.status == "no-fit"
    assert "Unable to place" in r.reason
    assert r.bitstream is None


def test_missed_timing_is_timing_fail_and_keeps_the_bitstream(toolchain, tmp_path):
    d, r = run(tmp_path, clock_hz=25_175_000)
    assert r.status == "timing-fail"
    assert "22.04" in r.reason and "25.175" in r.reason
    assert r.fmax_hz == 22_040_000
    assert r.bitstream and (paths.design_dir("build", d.id) / r.bitstream).exists()
    assert r.bitstream_sha256


def test_bad_preamble_is_pack_fail(toolchain, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_BAD_BIN", "1")
    _d, r = run(tmp_path)
    assert r.status == "pack-fail" and "preamble" in r.reason
    assert r.bitstream is None


def test_preamble_is_found_after_a_comment_header(toolchain, tmp_path):
    _, r = run(tmp_path)  # the fake icepack writes "# icepack fake\n" first
    assert r.status == "ok"


def test_icepack_error_is_pack_fail(toolchain, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ICEPACK_FAIL", "1")
    _, r = run(tmp_path)
    assert r.status == "pack-fail" and "boom" in r.reason


def test_build_json_is_renamed_into_place_and_stale_one_removed(toolchain, tmp_path, monkeypatch):
    d = make_design()
    out = paths.design_dir("build", d.id)
    out.mkdir(parents=True)
    (out / "build.json").write_text('{"status": "stale"}')
    renames = []
    real = os.replace

    def spy(src, dst, **kw):
        renames.append((Path(src).name, Path(dst).name))
        return real(src, dst, **kw)

    monkeypatch.setattr(os, "replace", spy)
    r = build.build(d, make_fetched(tmp_path), force=True)
    assert ("build.json.tmp", "build.json") in renames
    assert json.loads((out / "build.json").read_text())["status"] == r.status != "stale"


def test_unchanged_inputs_reuse_the_result_unless_forced(toolchain, rec, tmp_path):
    d = make_design()
    f = make_fetched(tmp_path)
    first = build.build(d, f)
    again = build.build(d, f)
    assert again == first
    assert (rec / "yosys.count").read_text().split() == ["run"]
    build.build(d, f, force=True)
    assert (rec / "yosys.count").read_text().split() == ["run", "run"]
    (f.sources[0]).write_text("module tt_um_x; // changed\nendmodule\n")
    build.build(d, make_fetched(tmp_path))
    assert len((rec / "yosys.count").read_text().split()) == 3


def test_failed_fetch_is_a_user_error(toolchain, tmp_path):
    bad = FetchResult("fetch-fail", tmp_path, (), (), tmp_path / "l", "git fetch failed")
    with pytest.raises(build.UserError, match="git fetch failed"):
        build.build(make_design(), bad)


@pytest.mark.parametrize("bad", ['a"b.v', "a;b.v", "a\\b.v", "a#b.v", "a`b.v", "a\nb.v"])
def test_unsafe_file_names_are_rejected_before_yosys_runs(toolchain, rec, tmp_path, bad):
    _d, r = run(tmp_path, names=(bad,))
    assert r.status == "synth-fail"
    assert "unsafe" in r.reason and "a" in r.reason
    assert not (rec / "yosys.count").exists()


def test_yosys_gets_a_script_file_not_an_inline_script(toolchain, rec, tmp_path):
    d, _r = run(tmp_path)
    out = paths.design_dir("build", d.id)
    args = (rec / "yosys.args").read_text().split()
    assert "-s" in args and "-p" not in args
    script = Path(args[args.index("-s") + 1])
    assert script.parent == out.resolve()
    assert "synth_ice40 -top tt_fpga_top" in script.read_text()


def test_changing_an_unlisted_file_in_src_rebuilds(toolchain, rec, tmp_path):
    d = make_design()
    f = make_fetched(tmp_path)
    (f.include_dirs[0] / "rom.hex").write_text("00\n")
    (f.include_dirs[0] / "sub").mkdir()
    (f.include_dirs[0] / "sub" / "inc.vh").write_text("`define A 1\n")
    f = make_fetched(tmp_path)
    build.build(d, f)
    build.build(d, f)
    assert len((rec / "yosys.count").read_text().split()) == 1
    (f.include_dirs[0] / "rom.hex").write_text("ff\n")
    build.build(d, make_fetched(tmp_path))
    assert len((rec / "yosys.count").read_text().split()) == 2
    (f.include_dirs[0] / "sub" / "inc.vh").write_text("`define A 2\n")
    build.build(d, make_fetched(tmp_path))
    assert len((rec / "yosys.count").read_text().split()) == 3


def test_nextpnr_failure_that_is_not_capacity_is_pnr_fail(toolchain, tmp_path, monkeypatch):
    log = tmp_path / "pnr.log"
    log.write_text("Info: something\n")
    monkeypatch.setenv("FAKE_PNR_LOG", str(log))
    monkeypatch.setenv("FAKE_PNR_EXIT", "1")
    _, r = run(tmp_path)
    assert r.status == "pnr-fail" and "exit status 1" in r.reason


def test_nextpnr_timeout_says_timed_out(toolchain, tmp_path, monkeypatch):
    monkeypatch.setattr(build, "NEXTPNR_TIMEOUT", 1)
    monkeypatch.setenv("FAKE_PNR_SLEEP", "5")
    _, r = run(tmp_path)
    assert r.status == "pnr-fail" and "nextpnr timed out" in r.reason


def test_real_icepack_header_is_accepted(toolchain, tmp_path):  # FF 00 00 FF, preamble at byte 4
    _, r = run(tmp_path)
    assert r.status == "ok"


def test_wrapper_must_have_exactly_one_placeholder(toolchain, tmp_path, monkeypatch):
    fake = tmp_path / "harness"
    fake.mkdir()
    (fake / "tt_fpga_top.v").write_text("__tt_um_placeholder a;\n__tt_um_placeholder b;\n")
    (fake / "tt_fpga_fabricfox.pcf").write_text("")
    monkeypatch.setattr(build, "HARNESS", fake)
    with pytest.raises(build.UserError, match="placeholder"):
        run(tmp_path)


def test_a_corrupted_bitstream_is_not_reused(toolchain, rec, tmp_path):
    d = make_design()
    f = make_fetched(tmp_path)
    r = build.build(d, f)
    bin_path = paths.design_dir("build", d.id) / r.bitstream
    bin_path.write_bytes(b"x" * bin_path.stat().st_size)
    build.build(d, f)
    assert len((rec / "yosys.count").read_text().split()) == 2


def fmax_env(monkeypatch, **by_key):
    monkeypatch.setenv("FAKE_FMAX", ",".join(f"{k.replace('_', '/')}={v}" for k, v in by_key.items()))


def order(rec) -> list[str]:
    return (rec / "pnr.order").read_text().split()


def test_first_recipe_passing_is_one_attempt(toolchain, rec, tmp_path, monkeypatch):
    fmax_env(monkeypatch, default_1=30)
    _, r = run(tmp_path, clock_hz=25_175_000)
    assert r.status == "ok" and r.recipe == {"synth": "default", "seed": 1}
    assert r.attempts == [{"synth": "default", "seed": 1, "status": "ok", "fmax_hz": 30_000_000}]
    assert order(rec) == ["default/1"]


def test_sweep_stops_at_the_first_passing_recipe(toolchain, rec, tmp_path, monkeypatch):
    fmax_env(monkeypatch, default_1=20, default_2=21, default_3=30, default_4=99)
    d, r = run(tmp_path, clock_hz=25_175_000)
    assert r.status == "ok" and r.recipe == {"synth": "default", "seed": 3}
    assert [a["status"] for a in r.attempts] == ["timing-fail", "timing-fail", "ok"]
    assert order(rec) == ["default/1", "default/2", "default/3"]
    assert r.fmax_hz == 30_000_000
    out = paths.design_dir("build", d.id)
    assert b"default/3" in (out / r.bitstream).read_bytes()
    for seed in (1, 2, 3):
        assert (out / f"nextpnr-default-{seed}.log").exists()
    assert json.loads((out / "build.json").read_text())["recipe"] == {"synth": "default", "seed": 3}
    assert (rec / "yosys.count").read_text().split() == ["run"]  # synthesised once for 3 seeds


def test_no_passing_recipe_keeps_the_best_attempt(toolchain, rec, tmp_path, monkeypatch):
    fmax_env(
        monkeypatch,
        default_1=20, default_2=23, default_3=21, default_4=22,
        nodffe_1=19, nodffe_2=24.5, nodffe_3=18, nodffe_4=17,
    )  # fmt: skip
    d, r = run(tmp_path, clock_hz=25_175_000)
    assert r.status == "timing-fail"
    assert len(r.attempts) == 8 and order(rec)[-1] == "nodffe/4"
    assert r.recipe == {"synth": "nodffe", "seed": 2} and r.fmax_hz == 24_500_000
    assert b"nodffe/2" in (paths.design_dir("build", d.id) / r.bitstream).read_bytes()
    assert (rec / "yosys.count").read_text().split() == ["run", "run"]  # once per synth variant


def test_max_attempts_one_restores_single_run(toolchain, rec, tmp_path, monkeypatch):
    fmax_env(monkeypatch, default_1=20, default_2=30)
    _, r = run(tmp_path, clock_hz=25_175_000, max_attempts=1)
    assert r.status == "timing-fail" and len(r.attempts) == 1 and order(rec) == ["default/1"]


def test_max_attempts_is_part_of_the_reuse_digest(toolchain, rec, tmp_path, monkeypatch):
    fmax_env(monkeypatch, default_1=20)
    d = make_design(clock_hz=25_175_000)
    f = make_fetched(tmp_path)
    build.build(d, f, max_attempts=1)
    build.build(d, f, max_attempts=2)
    assert len((rec / "yosys.count").read_text().split()) == 2  # second run re-synthesised


def test_no_fit_under_default_tries_the_other_synth_once(toolchain, rec, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_NOFIT", "default/1")
    fmax_env(monkeypatch, nodffe_1=30)
    _, r = run(tmp_path, clock_hz=25_175_000)
    assert r.status == "ok" and r.recipe == {"synth": "nodffe", "seed": 1}
    assert [(a["synth"], a["seed"], a["status"]) for a in r.attempts] == [
        ("default", 1, "no-fit"),
        ("nodffe", 1, "ok"),
    ]
    assert order(rec) == ["default/1", "nodffe/1"]


def test_no_fit_everywhere_is_no_fit(toolchain, rec, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_NOFIT", "default/1,nodffe/1")
    _, r = run(tmp_path, clock_hz=25_175_000)
    assert r.status == "no-fit" and len(r.attempts) == 2 and r.bitstream is None


def _rebuilds(rec) -> int:
    return len((rec / "yosys.count").read_text().split())


def test_sv2v_appearing_invalidates_the_reuse_digest(toolchain, rec, tmp_path):
    d, f = make_design(), make_fetched(tmp_path)
    build.build(d, f)
    build.build(d, f)
    assert _rebuilds(rec) == 1
    fake_tool(toolchain, "sv2v", SV2V)
    build.build(d, f)
    assert _rebuilds(rec) == 2


def test_icepack_version_is_in_the_reuse_digest(toolchain, rec, tmp_path, monkeypatch):
    d, f = make_design(), make_fetched(tmp_path)
    build.build(d, f)
    monkeypatch.setattr(build.tools, "icepack_version", lambda: "icepack other")
    build.build(d, f)
    assert _rebuilds(rec) == 2


def test_yosys_command_is_in_the_reuse_digest(toolchain, rec, tmp_path, monkeypatch):
    d, f = make_design(), make_fetched(tmp_path)
    build.build(d, f)
    real = build._synth_command
    monkeypatch.setattr(build, "_synth_command", lambda synth: [*real(synth), "-nobram"])
    build.build(d, f)
    assert _rebuilds(rec) == 2


def test_nextpnr_flags_are_in_the_reuse_digest(toolchain, rec, tmp_path, monkeypatch):
    d, f = make_design(), make_fetched(tmp_path)
    build.build(d, f)
    real = build._pnr_flags
    monkeypatch.setattr(build, "_pnr_flags", lambda hz, seed: [*real(hz, seed), "--placer", "sa"])
    build.build(d, f)
    assert _rebuilds(rec) == 2


def test_main_clock_failing_while_a_side_clock_passes_is_timing_fail(toolchain, rec, tmp_path, monkeypatch):
    # The dottee case: a derived clock passes comfortably, the main clock does not.
    monkeypatch.setenv("FAKE_SIDE", "side=99.0")
    fmax_env(monkeypatch, **{"default/1": "10.0", "default/2": "10.0"})
    _d, r = run(tmp_path, max_attempts=2)
    assert r.status == "timing-fail"
    assert r.fmax_hz == 10_000_000
    assert r.clocks["side"] == {"fmax_hz": 99_000_000, "constraint_hz": 20_000_000, "pass": True}
    assert r.clocks["clk$SB_IO_IN_$glb_clk"] == {
        "fmax_hz": 10_000_000,
        "constraint_hz": 20_000_000,
        "pass": False,
    }
    assert "clk$SB_IO_IN_$glb_clk" in r.reason and "10.00 MHz" in r.reason


def test_main_clock_passing_while_a_side_clock_fails_is_timing_fail_naming_it(
    toolchain, rec, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_SIDE", "side=5.0")
    fmax_env(monkeypatch, **{"default/1": "40.0"})
    _d, r = run(tmp_path, max_attempts=1)
    assert r.status == "timing-fail"
    assert r.fmax_hz == 40_000_000  # top level is the main clock's
    assert "side" in r.reason and "5.00 MHz" in r.reason
    assert "clk$SB_IO_IN_$glb_clk" not in r.reason


def test_all_clocks_passing_is_ok_and_recorded(toolchain, rec, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SIDE", "side=99.0")
    fmax_env(monkeypatch, **{"default/1": "40.0"})
    _d, r = run(tmp_path, max_attempts=1)
    assert r.status == "ok"
    assert set(r.clocks) == {"side", "clk$SB_IO_IN_$glb_clk"}
    assert all(c["pass"] for c in r.clocks.values())


def test_best_failing_attempt_is_the_one_with_the_highest_main_clock(toolchain, rec, tmp_path, monkeypatch):
    # The side clock is always slower than the main clock here, but the kept attempt is chosen by main clock.
    monkeypatch.setenv("FAKE_SIDE", "side=5.0")
    fmax_env(monkeypatch, **{"default/1": "12.0", "default/2": "15.0", "default/3": "13.0"})
    _d, r = run(tmp_path, max_attempts=3)
    assert r.status == "timing-fail" and r.fmax_hz == 15_000_000
    assert r.recipe == {"synth": "default", "seed": 2}


def test_sv_retry_only_follows_a_syntax_error(toolchain, rec, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_YOSYS_MISSING", "1")
    _d, r = run(tmp_path, names=("a.v",))
    assert r.status == "synth-fail" and "not part of the design" in r.reason
    assert (rec / "yosys.count").read_text().split() == ["run"]


def test_build_stamp_version_was_bumped_so_old_cached_results_are_not_reused():
    assert build._STAMP_VERSION >= 3
