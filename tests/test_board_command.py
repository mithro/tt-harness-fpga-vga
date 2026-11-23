# SPDX-License-Identifier: Apache-2.0
import json
import re

import pytest
from board_fakes import FakeBoard, FakeRepl

from tthfv.board import probe, status
from tthfv.cli import main

PROBE_OUT = {
    "implementation": ["micropython", [1, 29, 0], "fake with RP2350"],
    "uname": ["rp2", "rp2", "1.29.0", "v1.29.0", "fake with RP2350"],
    "freq_hz": 150000000,
    "mem_free": 400000,
    "sdk_present": True,
    "sdk_version": "3.1.0",
    "enabled": "tt_fpga_platform",
    "is_auto_clocking": False,
    "regs": {"SYSINFO_CHIP_ID": 0x20004927, "PWM_EN": 0, "RESETS_RESET_DONE": 0x1FFFFFFF},
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setenv("TTHFV_BOARD_CLAIMS", str(tmp_path / "claims"))
    return tmp_path


def test_status_prints_one_line_per_board(env, monkeypatch, capsys):
    with FakeBoard() as fake:
        fake.env(monkeypatch)
        assert main(["board", "status", "fpga-4"]) == 0
    out = capsys.readouterr().out
    assert "fpga-4" in out and "a2961e5cac65b25f" in out and "usable" in out


def test_probe_script_only_reads():
    src = probe.PROBE_SOURCE
    for forbidden in ("Pin(", "PWM(", "open(", "DemoBoard", ".init(", "rp2.", "import ttboard", "enable("):
        assert forbidden not in src
    assert not re.search(r"(\bm|mem32)\[[^\]]*\]\s*=(?!=)", src)  # registers are only read
    assert re.findall(r"\bm\[(0x[0-9a-f]+)\]", src)  # and some are read
    assert "'tt' in g" in src


def test_decode_registers():
    regs = probe.decode_registers(
        {
            "SYSINFO_CHIP_ID": 0x20004927,
            "IO_BANK0_GPIO16_CTRL": 0x04,
            "RESETS_RESET_DONE": 0x10000,
            "PWM_EN": 1,
            "PWM_CH0_CSR": 1,
        }
    )
    assert regs["chip_revision"] == 2
    assert regs["chip_part"] == 0x0004
    assert regs["gpio16_funcsel"] == "pwm"
    assert regs["pwm0_enabled"] is True


def test_probe_runs_in_a_session_and_writes_a_record(env, monkeypatch, capsys):
    def run(source):
        assert source == probe.PROBE_SOURCE
        return json.dumps(PROBE_OUT).encode() + b"\r\n", b""

    monkeypatch.setattr("tthfv.board.command.SESSION_ARGS", {"poll_s": 0.05, "restore_timeout_s": 3.0})
    out = env / "rec.json"
    with FakeBoard(repl=FakeRepl(run=run)) as fake:
        fake.env(monkeypatch)
        assert main(["board", "probe", "fpga-4", "--out", str(out)]) == 0
        assert fake.repl.resets == 0  # read-only: no soft reset
    rec = json.loads(out.read_text())
    assert rec["probe"]["sdk_version"] == "3.1.0"
    assert rec["session"]["outcome"] == "ok"
    assert rec["session"]["time_to_loaded_s"] is not None
    assert rec["probe_round_trip_s"] > 0
    assert rec["transcript"]
    assert "3.1.0" in capsys.readouterr().out


def test_probe_prompt_only(env, monkeypatch):
    monkeypatch.setattr("tthfv.board.command.SESSION_ARGS", {"poll_s": 0.05, "restore_timeout_s": 3.0})
    out = env / "rec.json"
    with FakeBoard() as fake:
        fake.env(monkeypatch)
        assert main(["board", "probe", "fpga-4", "--prompt-only", "--out", str(out)]) == 0
        assert fake.repl.scripts == []
        assert bytes(fake.repl.received).startswith(b"\r")
    rec = json.loads(out.read_text())
    assert ">>> " in rec["prompt"]


def test_probe_refused_is_an_error_and_still_recorded(env, monkeypatch, capsys):
    out = env / "rec.json"
    with FakeBoard() as fake:
        fake.env(monkeypatch)
        fake.state_override = "waiting"
        assert main(["board", "probe", "fpga-4", "--out", str(out)]) == 1
    assert "waiting" in capsys.readouterr().err
    assert json.loads(out.read_text())["session"]["outcome"].startswith("not usable")


class _Mem32:
    """machine.mem32 stand-in: records reads, fails the test on any write."""

    def __init__(self, values):
        self.values = values
        self.reads = []

    def __getitem__(self, addr):
        self.reads.append(addr)
        return self.values.get(addr, 0)

    def __setitem__(self, addr, value):
        raise AssertionError(f"probe wrote {value:#x} to {addr:#x}")


def _run_probe(monkeypatch, capsys, mem32, globals_):
    """Run the probe under CPython with stand-ins for `machine`, `os.uname` and `gc`'s MicroPython calls."""
    import collections
    import gc
    import os
    import sys
    import types

    machine = types.ModuleType("machine")
    machine.freq = lambda: 150_000_000
    machine.mem32 = mem32
    monkeypatch.setitem(sys.modules, "machine", machine)
    uname = collections.namedtuple("uname", "sysname nodename release version machine")
    monkeypatch.setattr(os, "uname", lambda: uname("rp2", "rp2", "1.29.0", "v1.29.0", "Board with RP2350"))
    monkeypatch.setattr(gc, "mem_free", lambda: 1000, raising=False)
    monkeypatch.setattr(gc, "mem_alloc", lambda: 10, raising=False)
    exec(compile(probe.PROBE_SOURCE, "probe", "exec"), {"__name__": "__main__", **globals_})  # noqa: S102
    return json.loads(capsys.readouterr().out.strip())


def test_probe_script_runs_and_only_reads_registers(monkeypatch, capsys):
    import types

    mem = _Mem32(
        {probe.RESETS_RESET_DONE: probe.RESET_DONE_PWM, probe.IO_BANK0_GPIO16_CTRL: 0x04, probe.PWM_EN: 1}
    )
    tt = types.SimpleNamespace(
        version="3.1.0",
        revision="n/a",
        shuttle=types.SimpleNamespace(run="FPGA", enabled=types.SimpleNamespace(name="tt_fpga_platform")),
        mode_str="ASIC_RP_CONTROL",
        is_auto_clocking=False,
        auto_clocking_freq=0,
    )
    result = _run_probe(monkeypatch, capsys, mem, {"tt": tt})
    assert result["sdk_present"] is True
    assert result["sdk_version"] == "3.1.0" and result["enabled"] == "tt_fpga_platform"
    assert result["freq_hz"] == 150_000_000 and result["mem_free"] == 1000
    assert result["is_auto_clocking"] is False
    assert set(mem.reads) == {
        probe.SYSINFO_CHIP_ID,
        probe.IO_BANK0_GPIO16_STATUS,
        probe.IO_BANK0_GPIO16_CTRL,
        probe.RESETS_RESET_DONE,
        probe.PWM_CH0_CSR,
        probe.PWM_CH0_DIV,
        probe.PWM_CH0_TOP,
        probe.PWM_EN,
    }


def test_probe_script_without_sdk_reports_it_and_skips_pwm_in_reset(monkeypatch, capsys):
    mem = _Mem32({})  # RESET_DONE all zero: PWM held in reset
    result = _run_probe(monkeypatch, capsys, mem, {})
    assert result["sdk_present"] is False and "sdk_version" not in result
    assert probe.PWM_EN not in mem.reads and probe.PWM_CH0_CSR not in mem.reads
