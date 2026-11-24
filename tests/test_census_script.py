# SPDX-License-Identifier: Apache-2.0
"""The MicroPython census script, run under CPython against a model of the RP2350's pads: it must never drive
a pin, must restore what it touched even when a read raises, must leave the clock PWM and MCU-driven pins
alone, and must print one JSON line the host accepts."""

import re
import sys
import time
import types

import pytest

from tthfv.board import census, probe

IN = 0
PULL_UP, PULL_DOWN = 2, 4


class Chip:
    """Pads of the nets: each has a state like the registers show it, and an external behaviour."""

    def __init__(self, nets, external=None, fail_on_read=None):
        self.state = {gpio: {"funcsel": 5, "oe": 0, "pue": 0, "pde": 0} for gpio in nets.values()}
        self.external = external or {}  # gpio -> function(pull) -> level
        self.pin_calls = []  # (gpio, mode, pull)
        self.mem_writes = []
        self.fail_on_read = fail_on_read
        self.tied = {}  # gpio -> gpio: the two pads are one net
        self.pwm = [11, 22, 33, 1]

    def _pull(self, gpio):
        s = self.state[gpio]
        return "up" if s["pue"] else "down" if s["pde"] else None

    def level(self, gpio):
        if gpio in self.external:
            return self.external[gpio](self._pull(gpio))
        pulls = [self._pull(gpio)]  # a floating pad follows the pull of itself and of the pad tied to it
        for a, b in self.tied.items():
            if gpio in (a, b):
                pulls += [self._pull(a), self._pull(b)]
        return 1 if "up" in pulls else 0  # a fight between pulls is won by the pull-up in this model


SDK_MAP = {name: census.NETS[net] for net, name in census.SDK_NAMES.items()}


def install(monkeypatch, chip, gpio_map=SDK_MAP):
    class Pin:
        IN = 0
        OUT = 1
        PULL_UP = 2
        PULL_DOWN = 4

        def __init__(self, n, mode=None, pull=None):
            self.n = n
            if mode is None:
                return
            chip.pin_calls.append((n, mode, pull))
            assert mode == IN, "the census drove a pin"
            chip.state[n].update(funcsel=5, oe=0, pue=int(pull == PULL_UP), pde=int(pull == PULL_DOWN))

        def value(self):
            if chip.fail_on_read == self.n:
                raise OSError("read failed")
            return chip.level(self.n)

    class Mem:
        def __getitem__(self, addr):
            if 0x40028000 <= addr < 0x40028000 + 8 * 48:
                n, off = divmod(addr - 0x40028000, 8)
                if off == 0:
                    return (chip.state[n]["oe"] << 13) | (chip.level(n) << 17)
                return chip.state[n]["funcsel"]
            if 0x40038004 <= addr < 0x40038004 + 4 * 48:
                s = chip.state[(addr - 0x40038004) // 4]
                return (s["pue"] << 3) | (s["pde"] << 2)
            if addr == 0x40020008:
                return 0x10000
            return chip.pwm[{0x400A8000: 0, 0x400A8004: 1, 0x400A8010: 2, 0x400A80F0: 3}[addr]]

        def __setitem__(self, addr, value):
            chip.mem_writes.append((addr, value))
            raise AssertionError("the census wrote a register")

    machine = types.ModuleType("machine")
    machine.Pin = Pin
    machine.mem32 = Mem()
    monkeypatch.setitem(sys.modules, "machine", machine)
    monkeypatch.setattr(time, "sleep_ms", lambda ms: None, raising=False)
    if gpio_map is not None:
        mod = types.ModuleType("ttboard.pins.gpio_map")
        mod.GPIOMapTTDBv3 = type("GPIOMapTTDBv3", (), gpio_map)
        monkeypatch.setitem(sys.modules, "ttboard.pins.gpio_map", mod)


def run(capsys):
    exec(compile(census.census_source(), "census", "exec"), {"__name__": "__main__"})  # noqa: S102
    return capsys.readouterr().out


def board():
    ext = {census.NETS["ui_in[0]"]: lambda pull: 1}
    for i in range(8):
        ext[census.NETS[f"uo_out[{i}]"]] = lambda pull: 0
    return ext


def test_script_accepts_a_board_and_never_drives_or_writes(monkeypatch, capsys):
    chip = Chip(census.NETS, board())
    chip.state[16].update(funcsel=4, oe=1)  # the project clock: PWM, output enabled
    chip.tied = {census.NETS["ui_in[2]"]: census.NETS["uio[2]"]}
    install(monkeypatch, chip, SDK_MAP | {"_hidden": 3, "name": "x"})
    raw = census.parse_output(run(capsys))
    assert raw["nets"]["clk"]["tested"] == 0 and raw["nets"]["ui_in[0]"]["pu"] == [1, 1, 1]
    assert raw["nets"]["ui_in[0]"]["pd"] == [1, 1, 1] and raw["nets"]["ui_in[5]"]["pd"] == [0, 0, 0]
    assert raw["sdk_map"] == SDK_MAP
    assert chip.pin_calls and all(mode == IN for _, mode, _ in chip.pin_calls)
    assert not chip.mem_writes
    assert all(g != 16 for g, _, _ in chip.pin_calls)  # the clock pin was never touched
    p = census.build_profile(raw, usb_serial="x", date="2026-11-24")
    assert ["ui_in[2]", "uio[2]"] in p["ties"] and ["ui_in[1]", "uio[1]"] not in p["ties"]
    assert p["nets"]["ui_in[0]"]["class"] == "driven-high"
    assert p["mcu_may_drive"] == [1, 2, 3, 4, 5, 6, 7]


def test_script_restores_the_pull_each_pin_had(monkeypatch, capsys):
    chip = Chip(census.NETS, board())
    chip.state[census.NETS["ui_in[4]"]].update(pue=1)
    chip.state[census.NETS["ui_in[5]"]].update(pde=1)
    install(monkeypatch, chip)
    raw = census.parse_output(run(capsys))
    s4, s5, s6 = (chip.state[census.NETS[f"ui_in[{i}]"]] for i in (4, 5, 6))
    assert (s4["pue"], s4["pde"]) == (1, 0)
    assert (s5["pue"], s5["pde"]) == (0, 1)
    assert (s6["pue"], s6["pde"]) == (0, 0)
    assert all(r["restored"] == 1 for r in raw["nets"].values())


def test_script_restores_in_a_finally_when_a_read_raises(monkeypatch, capsys):
    chip = Chip(census.NETS, board(), fail_on_read=census.NETS["uio[3]"])
    chip.state[census.NETS["ui_in[1]"]].update(pue=1)
    install(monkeypatch, chip)
    with pytest.raises(OSError, match="read failed"):
        run(capsys)
    for gpio, st in chip.state.items():
        assert st["funcsel"] == 5 and st["oe"] == 0
        want = (1, 0) if gpio == census.NETS["ui_in[1]"] else (0, 0)
        assert (st["pue"], st["pde"]) == want, gpio


def test_script_skips_a_pin_the_mcu_drives(monkeypatch, capsys):
    chip = Chip(census.NETS, board())
    chip.state[census.NETS["ui_in[3]"]].update(oe=1)  # an MCU output (the SDK's MCU-controls-inputs mode)
    install(monkeypatch, chip)
    raw = census.parse_output(run(capsys))
    assert raw["nets"]["ui_in[3]"]["tested"] == 0
    assert all(g != census.NETS["ui_in[3]"] for g, _, _ in chip.pin_calls)
    assert raw["ties"][2]["tested"] == 0


def test_script_has_no_forbidden_calls():
    src = (census.MP_DIR / "census.py").read_text()
    for forbidden in (
        "Pin.OUT",
        "mode=",
        ".on()",
        ".off()",
        "kbd_intr",
        "stdin",
        "input(",
        "PWM(",
        "soft_reset",
    ):
        assert forbidden not in src
    assert not re.search(r"\bm\[[^\]]*\]\s*=(?!=)", src)  # registers are only read
    assert re.findall(r"\bm\[", src)  # and some are read


def test_script_register_constants_match_the_probe():
    src = (census.MP_DIR / "census.py").read_text().lower()
    assert f"{probe.RESETS_RESET_DONE:#x}" in src and f"{probe.RESET_DONE_PWM:#x}" in src
    for addr in (probe.PWM_CH0_CSR, probe.PWM_CH0_DIV, probe.PWM_CH0_TOP, probe.PWM_EN):
        assert f"{addr:#x}" in src
    assert "0x40028000" in src and probe.IO_BANK0_GPIO16_STATUS == 0x40028000 + 8 * 16


def test_script_is_small_enough_for_the_board_heap():
    assert len(census.census_source().encode()) < 6000 and len(census.info_source().encode()) < 4000


def test_script_without_the_nets_marker_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(census, "MP_DIR", tmp_path)
    (tmp_path / "census.py").write_text("NETS = {}\n")
    with pytest.raises(census.CensusError, match="NETS"):
        census.census_source()
