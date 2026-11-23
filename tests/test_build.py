# SPDX-License-Identifier: Apache-2.0
"""Build stage tests. The toolchain is replaced by fake executables written under tmp_path."""

import dataclasses
import hashlib
import json
import stat
from pathlib import Path

import pytest

from tthfv import build, paths
from tthfv.fetch import FetchResult
from tthfv.manifest import Design

LOGS = Path(__file__).parent / "data" / "logs"

YOSYS = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "Yosys fake 1.0"; exit 0; fi
echo run >> "$FAKE_REC/yosys.count"
pwd > "$FAKE_REC/yosys.cwd"
echo "$@" >> "$FAKE_REC/yosys.args"
if [ -n "$FAKE_YOSYS_FAIL" ]; then
  case "$(cat "$2")" in *sv2v.v*) ;; *) echo "ERROR: syntax error near foo"; exit 1;; esac
fi
cat "$FAKE_YOSYS_LOG"
"""

NEXTPNR = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "nextpnr-ice40 fake 1.0"; exit 0; fi
[ -n "$FAKE_PNR_SLEEP" ] && sleep "$FAKE_PNR_SLEEP"
echo "$@" > "$FAKE_REC/nextpnr.args"
while [ $# -gt 0 ]; do
  if [ "$1" = "--asc" ]; then echo asc > "$2"; fi
  shift
done
cat "$FAKE_PNR_LOG"
exit ${FAKE_PNR_EXIT:-0}
"""

ICEPACK = """#!/bin/sh
if [ -n "$FAKE_ICEPACK_FAIL" ]; then echo "icepack: boom"; exit 1; fi
if [ -n "$FAKE_BAD_BIN" ]; then printf 'nonsense' > "$2"; exit 0; fi
printf '\\377\\000\\000\\377\\176\\252\\231\\176payload' > "$2"
"""

SV2V = """#!/bin/sh
echo "$@" > "$FAKE_REC/sv2v.args"
echo "module converted; endmodule"
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
    for var in ("FAKE_YOSYS_FAIL", "FAKE_PNR_EXIT", "FAKE_ICEPACK_FAIL", "FAKE_BAD_BIN"):
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


def make_fetched(tmp_path: Path, names=("a.v",)) -> FetchResult:
    src_dir = tmp_path / "repo"
    src = src_dir / "src"
    src.mkdir(parents=True, exist_ok=True)
    for n in names:
        (src / n).write_text(f"// {n}\n")
    return FetchResult("ok", src_dir, tuple(src / n for n in names), (src,), tmp_path / "fetch.log")


def run(tmp_path, names=("a.v",), force=False, **kw):
    d = make_design(source_files=names, **kw)
    return d, build.build(d, make_fetched(tmp_path, names), force=force)


def test_ok_build_records_everything(toolchain, rec, tmp_path):
    d, r = run(tmp_path)
    assert r.status == "ok", r.reason
    out = paths.design_dir("build", d.id)
    bin_path = out / r.bitstream
    assert bin_path.read_bytes().endswith(b"payload")
    assert r.bitstream_sha256 == hashlib.sha256(bin_path.read_bytes()).hexdigest()
    assert r.clock_hz == 20_000_000 and r.fmax_hz == 22_040_000
    assert r.cells["SB_LUT4"] == 761 and r.cells["SB_DFF"] == 69
    assert r.lut_percent == pytest.approx(15.6)
    assert r.tools == {"yosys": "Yosys fake 1.0", "nextpnr-ice40": "nextpnr-ice40 fake 1.0"}
    assert r.seconds >= 0
    on_disk = json.loads((out / "build.json").read_text())
    assert on_disk["id"] == d.id and on_disk["status"] == "ok"
    assert on_disk["bitstream_sha256"] == r.bitstream_sha256
    assert (out / "yosys.log").exists() and (out / "nextpnr.log").exists()
    assert not list(out.glob("*.tmp"))


def test_tool_command_lines(toolchain, rec, tmp_path):
    d, _r = run(tmp_path, clock_hz=25_175_000)
    out = paths.design_dir("build", d.id).resolve()
    pnr = (rec / "nextpnr.args").read_text().split()
    for flag in ("--up5k", "--pcf-allow-unconstrained", "--timing-allow-fail"):
        assert flag in pnr
    assert pnr[pnr.index("--package") + 1] == "sg48"
    assert pnr[pnr.index("--freq") + 1] == "25.175"
    assert pnr[pnr.index("--seed") + 1] == "1"
    assert pnr[pnr.index("--json") + 1] == str(out / "out.json")
    assert pnr[pnr.index("--asc") + 1] == str(out / "out.asc")
    assert pnr[pnr.index("--pcf") + 1].endswith("harness/tt_fpga_fabricfox.pcf")
    ys = (paths.design_dir("build", d.id).resolve() / "yosys.ys").read_text()
    assert "synth_ice40 -top tt_fpga_top -json" in ys
    assert f"-I {tmp_path / 'repo' / 'src'}" in ys or f'-I "{tmp_path / "repo" / "src"}"' in ys
    # yosys runs in the design's src directory, and the wrapper names the design's top module.
    assert (rec / "yosys.cwd").read_text().strip() == str(tmp_path / "repo" / "src")
    wrapper = (out / "tt_fpga_top.v").read_text()
    assert "tt_um_x user_project" in wrapper and "__tt_um_placeholder" not in wrapper


def test_systemverilog_sources_use_sv_reader(toolchain, rec, tmp_path):
    run(tmp_path, names=("a.sv", "b.v"))
    ys = (paths.design_dir("build", "tt99/tt_um_x").resolve() / "yosys.ys").read_text()
    assert "read_verilog -sv" in ys and ys.count("read_verilog") >= 2


def test_yosys_failure_is_synth_fail(toolchain, rec, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_YOSYS_FAIL", "1")
    d, r = run(tmp_path)
    assert r.status == "synth-fail"
    assert "syntax error near foo" in r.reason
    assert r.bitstream is None and r.sv2v is False
    assert not (rec / "nextpnr.args").exists()
    assert json.loads((paths.design_dir("build", d.id) / "build.json").read_text())["status"] == "synth-fail"


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
    assert (rec / "yosys.count").read_text().split() == ["run"]


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
    real = Path.rename

    def spy(self, target):
        renames.append((self.name, Path(target).name))
        return real(self, target)

    monkeypatch.setattr(Path, "rename", spy)
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
    (f.sources[0]).write_text("// changed\n")
    build.build(d, f)
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
    build.build(d, f)
    build.build(d, f)
    assert len((rec / "yosys.count").read_text().split()) == 1
    (f.include_dirs[0] / "rom.hex").write_text("ff\n")
    build.build(d, f)
    assert len((rec / "yosys.count").read_text().split()) == 2
    (f.include_dirs[0] / "sub" / "inc.vh").write_text("`define A 2\n")
    build.build(d, f)
    assert len((rec / "yosys.count").read_text().split()) == 3


def test_nextpnr_failure_that_is_not_capacity_is_pnr_fail(toolchain, tmp_path, monkeypatch):
    log = tmp_path / "pnr.log"
    log.write_text("Info: something\n")
    monkeypatch.setenv("FAKE_PNR_LOG", str(log))
    monkeypatch.setenv("FAKE_PNR_EXIT", "1")
    _, r = run(tmp_path)
    assert r.status == "pnr-fail" and "exit status 1" in r.reason


def test_nextpnr_timeout_says_timed_out(toolchain, tmp_path, monkeypatch):
    monkeypatch.setattr(build, "_NEXTPNR_TIMEOUT", 1)
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
