# SPDX-License-Identifier: Apache-2.0
"""The pin census: pure classification, ties, mcu_may_drive and stimulus; strict parsing of the board's JSON
line; and `tthfv board census` against the fake board."""

import copy
import json

import pytest
from board_fakes import FakeBoard, FakeRepl

from tthfv.board import census, session, status
from tthfv.cli import main


def test_net_list_is_the_26_tiny_tapeout_nets():
    names = list(census.NETS)
    assert len(names) == 26 == len(set(names))
    assert census.NETS["clk"] == 16 and census.NETS["rst_n"] == 14
    assert [census.NETS[f"ui_in[{i}]"] for i in range(8)] == list(range(17, 25))
    assert [census.NETS[f"uio[{i}]"] for i in range(8)] == list(range(25, 33))
    assert [census.NETS[f"uo_out[{i}]"] for i in range(8)] == list(range(33, 41))


@pytest.mark.parametrize(
    ("pd", "pu", "want"),
    [(0, 1, "floating"), (1, 1, "driven-high"), (0, 0, "driven-low")],
)
def test_classify(pd, pu, want):
    assert census.classify(pd, pu) == want


def test_classify_rejects_a_pin_that_reads_against_both_pulls():
    with pytest.raises(census.CensusError, match="against both"):
        census.classify(1, 0)


SDK_MAP = {name: census.NETS[net] for net, name in census.SDK_NAMES.items()} | {
    "MANUAL_PROJCLK": 15,
    "RP_LED": 11,
}


def _raw(**overrides):
    """A raw board answer like fpga-4's: ui_in all floating, uio[0..5] floating, uio[6..7] driven high, uo_out
    driven low, clk MCU-driven (not tested); pairs 1..3 read as one net, the other pairs as independent."""
    nets = {}
    for name, gpio in census.NETS.items():
        found = {"funcsel": 5, "oe": 0, "level": 0, "pue": 0, "pde": 1}
        pd, pu, tested = [0, 0, 0], [1, 1, 1], 1
        if name in ("uio[6]", "uio[7]"):
            pd, pu = [1, 1, 1], [1, 1, 1]
        if name.startswith("uo_out"):
            pd, pu = [0, 0, 0], [0, 0, 0]
        if name == "clk":
            found = {"funcsel": 4, "oe": 1, "level": 1, "pue": 0, "pde": 1}
            pd, pu, tested = [], [], 0
        nets[name] = {"gpio": gpio, "found": found, "tested": tested, "pd": pd, "pu": pu, "restored": 1}
    ties = []
    for k in range(8):
        one_net = k in (1, 2, 3)
        ties.append(
            {
                "a": f"ui_in[{k}]",
                "b": f"uio[{k}]",
                "tested": 1,
                "a_up_b_down": [1, 1] if one_net else [1, 0],
                "a_down_b_up": [1, 1] if one_net else [0, 1],
            }
        )
    out = {
        "v": 1,
        "nets": nets,
        "ties": ties,
        "pwm_before": [1, 2, 3, 1],
        "pwm_after": [1, 2, 3, 1],
        "sdk_map": dict(SDK_MAP),
        "restore_failed": [],
        "mem_free": [400000, 399000],
        "retries": 0,
    }
    out.update(overrides)
    return out


def _parse(raw):
    return census.parse_output(json.dumps(raw))


def test_parse_accepts_a_good_answer_and_takes_the_last_line():
    raw = _parse(_raw())
    assert raw["nets"]["uio[6]"]["pd"] == [1, 1, 1]
    text = "noise\r\n" + json.dumps(_raw()) + "\r\n"
    assert census.parse_output(text)["v"] == 1


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda r: r.pop("ties"), "keys"),
        (lambda r: r.update(extra=1), "keys"),
        (lambda r: r.update(v=2), "version"),
        (lambda r: r["nets"].pop("uo_out[3]"), "nets"),
        (lambda r: r["nets"].update({"bogus": copy.deepcopy(r["nets"]["clk"])}), "nets"),
        (lambda r: r["nets"]["ui_in[0]"].update(gpio=99), "gpio"),
        (lambda r: r["nets"]["ui_in[0]"].update(pd="1"), "pd"),
        (lambda r: r["nets"]["ui_in[0]"].update(pd=[1, 1]), "pd"),
        (lambda r: r["nets"]["ui_in[0]"].update(pd=[1, 1, 2]), "pd"),
        (lambda r: r["nets"]["ui_in[0]"].update(pd=[True, True, True]), "pd"),
        (lambda r: r["nets"]["clk"].update(pd=[0, 0, 0]), "pd"),
        (lambda r: r["nets"]["ui_in[0]"].update(tested=2), "tested"),
        (lambda r: r["nets"]["ui_in[0]"]["found"].pop("oe"), "found"),
        (lambda r: r["nets"]["ui_in[0]"]["found"].update(oe="1"), "found"),
        (lambda r: r["nets"]["ui_in[0]"].update(restored=0), "restored"),
        (lambda r: r.update(pwm_after=[9, 9, 9, 9]), "PWM"),
        (lambda r: r["ties"].pop(), "ties"),
        (lambda r: r["ties"][0].update(a="ui_in[2]"), "ties"),
        (lambda r: r["ties"][0].update(a_up_b_down=[0]), "ties"),
        (lambda r: r["ties"][0].update(tested=0), "ties"),
        (lambda r: r.update(sdk_map=[1]), "sdk_map"),
        (lambda r: r.update(sdk_map={}), "not found"),
        (lambda r: r["sdk_map"].update(UI_IN3=21), r"ui_in\[3\].*UI_IN3"),
        (lambda r: r.update(restore_failed=["ui_in[2]"]), r"restore.*ui_in\[2\]"),
        (lambda r: r.update(restore_failed="no"), "restore_failed"),
        (lambda r: r.update(mem_free=[1]), "mem_free"),
        (lambda r: r.update(mem_free=[1, "2"]), "mem_free"),
        (lambda r: r.update(retries=-1), "retries"),
        (lambda r: r.update(retries="1"), "retries"),
    ],
)
def test_parse_rejects_anything_unexpected(mutate, message):
    raw = _raw()
    mutate(raw)
    with pytest.raises(census.CensusError, match=f"(?i){message}"):
        _parse(raw)


def test_parse_rejects_non_json_and_non_objects():
    for text in ("", "not json", "[1, 2]", '"x"'):
        with pytest.raises(census.CensusError):
            census.parse_output(text)


def test_parse_rejects_reads_that_disagree_with_themselves():
    raw = _raw()
    raw["nets"]["ui_in[4]"]["pd"] = [0, 1, 0]
    with pytest.raises(census.CensusError, match=r"ui_in\[4\].*unstable"):
        census.build_profile(_parse(raw), usb_serial="s", date="2026-11-24")


def _profile(**overrides):
    return census.build_profile(_parse(_raw(**overrides)), usb_serial="a2961e5cac65b25f", date="2026-11-24")


def _held(raw, name, level):
    raw["nets"][name]["pd"] = raw["nets"][name]["pu"] = [level] * 3


def test_profile_shape_classes_and_basis():
    p = _profile()
    assert list(p) == [
        "usb_serial",
        "date",
        "nets",
        "ties",
        "tie_tests",
        "drive_conflicts",
        "mcu_may_drive",
        "stimulus",
    ]
    assert p["usb_serial"] == "a2961e5cac65b25f" and p["date"] == "2026-11-24"
    assert len(p["nets"]) == 26
    assert p["nets"]["ui_in[4]"] == {
        "gpio": 21,
        "read_pulldown": 0,
        "read_pullup": 1,
        "class": "floating",
        "basis": "measured",
    }
    assert p["nets"]["uio[6]"]["class"] == "driven-high"
    assert p["nets"]["uio[6]"]["basis"] == "indistinguishable"
    assert p["nets"]["uo_out[0]"]["class"] == "driven-low"
    assert p["nets"]["uo_out[0]"]["basis"] == "indistinguishable"
    assert p["nets"]["clk"] == {
        "gpio": 16,
        "read_pulldown": None,
        "read_pullup": None,
        "class": "mcu-configured",
        "basis": "not-tested",
    }


def test_all_eight_pairs_are_tested_and_ties_derive_from_the_verdicts():
    p = _profile()
    assert [t["a"] for t in p["tie_tests"]] == [f"ui_in[{k}]" for k in range(8)]
    verdicts = [t["verdict"] for t in p["tie_tests"]]
    assert verdicts == ["independent", "tied", "tied", "tied", "independent", "independent",
                        "undetermined", "undetermined"]  # fmt: skip
    assert p["ties"] == [["ui_in[1]", "uio[1]"], ["ui_in[2]", "uio[2]"], ["ui_in[3]", "uio[3]"]]
    assert p["tie_tests"][1] == {
        "a": "ui_in[1]",
        "b": "uio[1]",
        "a_up_b_down": [1, 1],
        "a_down_b_up": [1, 1],
        "verdict": "tied",
        "reason": "equal readings under opposed pulls, both ways round (consistent with a connection; a "
        "resistive link would read the same)",
    }
    assert p["tie_tests"][0]["a_up_b_down"] == [1, 0] and p["tie_tests"][0]["a_down_b_up"] == [0, 1]
    assert "own pull" in p["tie_tests"][0]["reason"]


def test_a_failed_negative_control_is_visible_in_the_data():
    raw = _raw()
    for rec in raw["ties"]:
        rec["a_up_b_down"] = rec["a_down_b_up"] = [1, 1]
    p = census.build_profile(_parse(raw), usb_serial="s", date="2026-11-24")
    assert len(p["ties"]) == 6  # pairs 0..5 all "tied": the method has no negative control left


def test_mixed_readings_are_undetermined_with_a_reason():
    raw = _raw()
    raw["ties"][1]["a_down_b_up"] = [1, 0]
    p = census.build_profile(_parse(raw), usb_serial="s", date="2026-11-24")
    t = p["tie_tests"][1]
    assert t["verdict"] == "undetermined" and "mixed" in t["reason"]
    assert ["ui_in[1]", "uio[1]"] not in p["ties"]


def test_a_driven_net_makes_its_tie_undetermined_not_guessed():
    raw = _raw()
    _held(raw, "uio[2]", 1)
    p = census.build_profile(_parse(raw), usb_serial="s", date="2026-11-24")
    t = p["tie_tests"][2]
    assert t["verdict"] == "undetermined" and "driven-high" in t["reason"]
    assert ["ui_in[2]", "uio[2]"] not in p["ties"] and ["ui_in[1]", "uio[1]"] in p["ties"]


def test_untested_pair_is_undetermined():
    raw = _raw()
    raw["ties"][2].update(tested=0, a_up_b_down=[], a_down_b_up=[])
    p = census.build_profile(_parse(raw), usb_serial="s", date="2026-11-24")
    assert p["tie_tests"][2]["verdict"] == "undetermined" and "not tested" in p["tie_tests"][2]["reason"]
    assert p["tie_tests"][2]["a_up_b_down"] == []


def test_drive_conflicts_come_from_the_ties():
    p = _profile()
    assert p["drive_conflicts"] == [{"ui_in": 1, "uio": 1}, {"ui_in": 2, "uio": 2}, {"ui_in": 3, "uio": 3}]
    raw = _raw()
    for rec in raw["ties"]:
        rec["a_up_b_down"], rec["a_down_b_up"] = [1, 0], [0, 1]
    assert census.build_profile(_parse(raw), usb_serial="s", date="d")["drive_conflicts"] == []


def test_effective_ui_in_mask_drops_bits_whose_tied_uio_can_be_an_output():
    p = _profile()
    assert p["stimulus"]["ui_in_mask"] == 0xFF
    assert census.effective_ui_in_mask(p, 0x00) == 0xFF  # the design never enables a uio output
    assert census.effective_ui_in_mask(p, 0x04) == 0xFB  # uio[2] may be an output: ui_in[2] dropped
    assert census.effective_ui_in_mask(p, 0xF1) == 0xFF  # outputs on uio bits with no tie change nothing
    assert census.effective_ui_in_mask(p, 0xFF) == 0xF1  # unknown: bits 1..3 dropped on this board
    q = copy.deepcopy(p)
    q["stimulus"]["ui_in_mask"] = 0xFE
    assert census.effective_ui_in_mask(q, 0xFF) == 0xF0  # starts from the profile's own mask
    q["drive_conflicts"] = []
    assert census.effective_ui_in_mask(q, 0xFF) == 0xFE


def test_effective_ui_in_mask_rejects_a_mask_that_is_not_a_byte():
    p = _profile()
    for bad in (-1, 256, "0xff", True):
        with pytest.raises(census.CensusError, match="uio_oe_possible"):
            census.effective_ui_in_mask(p, bad)


def test_mcu_may_drive_is_the_floating_ui_in_bits_only():
    p = _profile()
    assert p["mcu_may_drive"] == [0, 1, 2, 3, 4, 5, 6, 7]
    raw = _raw()
    _held(raw, "ui_in[5]", 0)
    p = census.build_profile(_parse(raw), usb_serial="s", date="2026-11-24")
    assert p["mcu_may_drive"] == [0, 1, 2, 3, 4, 6, 7]


def test_stimulus_is_the_level_of_undriven_bits_with_what_is_uncertain_or_assumed():
    p = _profile()
    assert p["stimulus"] == {
        "ui_in": 0x00,
        "uio_in": 0xC0,
        "ui_in_mask": 0xFF,
        "uio_in_uncertain": 0xC0,
        "floating_assumed_low": {"ui_in": 0xFF, "uio": 0x3F},
    }
    raw = _raw()
    _held(raw, "ui_in[5]", 1)  # held high, not drivable
    _held(raw, "uio[0]", 0)  # driven low
    p = census.build_profile(_parse(raw), usb_serial="s", date="2026-11-24")
    assert p["stimulus"] == {
        "ui_in": 0x20,
        "uio_in": 0xC0,
        "ui_in_mask": 0xDF,
        "uio_in_uncertain": 0xC1,
        "floating_assumed_low": {"ui_in": 0xDF, "uio": 0x3E},
    }


def test_a_ui_in_net_the_mcu_configured_stops_the_derivation():
    raw = _raw()
    raw["nets"]["ui_in[2]"].update(tested=0, pd=[], pu=[])
    with pytest.raises(census.CensusError, match=r"ui_in\[2\]"):
        census.build_profile(_parse(raw), usb_serial="s", date="2026-11-24")


def test_profiles_agree_ignores_the_date_only():
    a, b = _profile(), _profile()
    b["date"] = "2026-11-25"
    assert census.differences(a, b) == []
    b["nets"]["ui_in[4]"]["read_pullup"] = 0
    b["nets"]["ui_in[4]"]["class"] = "driven-low"
    diff = census.differences(a, b)
    assert diff and "ui_in[4]" in diff[0]


def test_uo_out_levels_may_differ_between_runs_but_not_whether_they_are_driven():
    a, b = _profile(), _profile()
    b["nets"]["uo_out[2]"].update(read_pulldown=1, read_pullup=1, **{"class": "driven-high"})
    assert census.differences(a, b) == []
    b["nets"]["uo_out[2]"].update(
        read_pulldown=0, read_pullup=1, **{"class": "floating", "basis": "measured"}
    )
    assert any("uo_out[2]" in d for d in census.differences(a, b))
    c = _profile()
    c["nets"]["uio[6]"].update(read_pulldown=0, read_pullup=0, **{"class": "driven-low"})
    assert any("uio[6]" in d for d in census.differences(a, c))  # uio levels do count


def test_profile_round_trips_through_validation():
    p = _profile()
    census.validate_profile(json.loads(json.dumps(p)))


def _invalid(mutate, message):
    bad = copy.deepcopy(_profile())
    mutate(bad)
    with pytest.raises(census.CensusError, match=f"(?i){message}"):
        census.validate_profile(bad)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p["nets"]["clk"].update(**{"class": "shiny"}), "class"),
        (lambda p: p["stimulus"].update(ui_in_mask="0xfe"), "stimulus"),
        (lambda p: p["stimulus"].pop("uio_in_uncertain"), "stimulus"),
        (lambda p: p["stimulus"]["floating_assumed_low"].pop("uio"), "floating_assumed_low"),
        (lambda p: p["stimulus"]["floating_assumed_low"].update(ui_in=256), "floating_assumed_low"),
        (lambda p: p.pop("drive_conflicts"), "keys"),
        (lambda p: p.update(ties_undetermined=[]), "keys"),
        (lambda p: p["nets"]["ui_in[4]"].pop("basis"), "keys"),
        (lambda p: p["nets"]["ui_in[4]"].update(basis="guessed"), "basis"),
        (lambda p: p["nets"]["ui_in[4]"].update(basis="indistinguishable"), "basis"),
        (lambda p: p["nets"]["uio[6]"].update(basis="measured"), "basis"),
        (lambda p: p["nets"]["clk"].update(basis="measured"), "basis"),
        # a class must agree with its reads
        (lambda p: p["nets"]["ui_in[4]"].update(read_pulldown=1), "agree"),
        (lambda p: p["nets"]["uio[6]"].update(read_pullup=0), "agree"),
        (lambda p: p["nets"]["uo_out[0]"].update(read_pulldown=1, read_pullup=1), "agree"),
        (lambda p: p["nets"]["clk"].update(read_pullup=1), "agree"),
        (lambda p: p["tie_tests"][1].update(verdict="maybe"), "verdict"),
        (lambda p: p["tie_tests"][1].pop("reason"), "keys"),
        (lambda p: p["tie_tests"][0].update(verdict="tied"), "ties"),
        (lambda p: p["drive_conflicts"].pop(), "drive_conflicts"),
        (lambda p: p["drive_conflicts"][0].update(uio=2), "drive_conflicts"),
    ],
)
def test_validate_profile_rejects(mutate, message):
    _invalid(mutate, message)


# --- the CLI against the fake board ---


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(session, "CONFIRM_AFTER_S", 0.0)
    monkeypatch.setenv("TTHFV_BOARD_CLAIMS", str(tmp_path / "claims"))
    monkeypatch.setenv("TTHFV_WORK", str(tmp_path / "work"))
    monkeypatch.setattr("tthfv.board.command.SESSION_ARGS", {"poll_s": 0.05, "restore_timeout_s": 3.0})
    return tmp_path


def _run_with(answer: bytes | None):
    def run(source):
        assert "def _tthfv_census" in source
        return (answer if answer is not None else json.dumps(_raw()).encode()) + b"\r\n", b""

    return run


def test_cli_writes_the_profile_and_the_record_and_soft_resets_once(env, monkeypatch, capsys):
    out = env / "fpga-4.json"
    with FakeBoard(repl=FakeRepl(run=_run_with(None))) as fake:
        fake.env(monkeypatch)
        assert main(["board", "census", "fpga-4", "--out", str(out)]) == 0
        assert fake.repl.resets == 1  # changes_state=True: the full clean-up
        (script,) = fake.repl.scripts
    assert '"ui_in[0]": 17' in script and "tthfv:nets" in script  # the nets are injected as a literal
    p = json.loads(out.read_text())
    census.validate_profile(p)
    assert p["usb_serial"] == "a2961e5cac65b25f"
    assert p["stimulus"]["ui_in_mask"] == 0xFF
    records = list((env / "work" / "board").glob("fpga-4-census-*.json"))
    assert len(records) == 1
    rec = json.loads(records[0].read_text())
    assert rec["session"]["outcome"] == "ok" and rec["raw"]["v"] == 1 and rec["transcript"]
    assert "ties" in capsys.readouterr().out


def test_cli_check_against_reports_agreement_and_difference(env, monkeypatch, capsys):
    first = env / "first.json"
    with FakeBoard(repl=FakeRepl(run=_run_with(None))) as fake:
        fake.env(monkeypatch)
        assert main(["board", "census", "fpga-4", "--out", str(first)]) == 0
        assert (
            main(
                [
                    "board",
                    "census",
                    "fpga-4",
                    "--out",
                    str(env / "second.json"),
                    "--check-against",
                    str(first),
                ]
            )
            == 0
        )
    assert "agree" in capsys.readouterr().out
    raw = _raw()
    raw["nets"]["ui_in[6]"]["pd"] = raw["nets"]["ui_in[6]"]["pu"] = [1, 1, 1]
    with FakeBoard(repl=FakeRepl(run=_run_with(json.dumps(raw).encode()))) as fake:
        fake.env(monkeypatch)
        assert (
            main(
                ["board", "census", "fpga-4", "--out", str(env / "third.json"), "--check-against", str(first)]
            )
            == 1
        )
    assert "ui_in[6]" in capsys.readouterr().err


@pytest.mark.parametrize(
    "answer", [b"hello", b'{"v": 1}', b"[]", json.dumps({**_raw(), "pwm_after": [0]}).encode()]
)
def test_cli_fails_loudly_on_malformed_output_and_still_cleans_up(env, monkeypatch, capsys, answer):
    out = env / "fpga-4.json"
    with FakeBoard(repl=FakeRepl(run=_run_with(answer))) as fake:
        fake.env(monkeypatch)
        assert main(["board", "census", "fpga-4", "--out", str(out)]) == 1
        assert fake.repl.resets == 1
    assert not out.exists()
    assert capsys.readouterr().err.strip()
    rec = next((env / "work" / "board").glob("fpga-4-census-*.json"))
    assert "error" in json.loads(rec.read_text())


def test_cli_board_script_error_is_reported(env, monkeypatch, capsys):
    with FakeBoard(repl=FakeRepl()) as fake:  # the default fake raises on scripts containing `raise`
        fake.env(monkeypatch)
        assert (
            main(["board", "census", "fpga-4", "--out", str(env / "p.json")]) == 1
        )  # prints 'ran', not JSON
    assert capsys.readouterr().err.strip()


def test_cli_refuses_a_board_that_is_not_ours(env, monkeypatch, capsys):
    assert main(["board", "census", "fpga-1", "--out", str(env / "p.json")]) == 1
    assert "fpga-1" in capsys.readouterr().err


def test_info_session_is_read_only_and_writes_a_record(env, monkeypatch, capsys):
    info = {"implementation": ["micropython", [1, 29, 0]], "sdk_version": "3.1.0", "pins": {}}
    out = env / "info.json"
    with FakeBoard(repl=FakeRepl(run=lambda s: (json.dumps(info).encode() + b"\r\n", b""))) as fake:
        fake.env(monkeypatch)
        assert main(["board", "census", "fpga-4", "--info", "--out", str(out)]) == 0
        assert fake.repl.resets == 0 and fake.repl.raw_resets == 0
        assert len(fake.repl.scripts) == 2  # the probe and the pin look
    rec = json.loads(out.read_text())
    assert rec["info"]["sdk_version"] == "3.1.0" and rec["session"]["outcome"] == "ok"
