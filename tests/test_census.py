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


def _raw(**overrides):
    """A raw board answer for a board with ui_in[0] held high, ui_in[1..3] tied to uio[1..3], the rest floating,
    uo_out driven low, clk MCU-driven."""
    nets = {}
    for name, gpio in census.NETS.items():
        found = {"funcsel": 5, "oe": 0, "level": 0, "pue": 0, "pde": 0}
        pd, pu, tested = [0, 0, 0], [1, 1, 1], 1
        if name == "ui_in[0]":
            pd, pu = [1, 1, 1], [1, 1, 1]
        if name.startswith("uo_out"):
            pd, pu = [0, 0, 0], [0, 0, 0]
        if name == "clk":
            found = {"funcsel": 4, "oe": 1, "level": 1, "pue": 0, "pde": 0}
            pd, pu, tested = [], [], 0
        nets[name] = {"gpio": gpio, "found": found, "tested": tested, "pd": pd, "pu": pu, "restored": 1}
    ties = [
        {"a": f"ui_in[{k}]", "b": f"uio[{k}]", "tested": 1, "a_up_b_down": [0, 0], "a_down_b_up": [1, 1]}
        for k in (1, 2, 3)
    ]
    out = {
        "v": 1,
        "nets": nets,
        "ties": ties,
        "pwm_before": [1, 2, 3, 1],
        "pwm_after": [1, 2, 3, 1],
        "sdk_map": {},
    }
    out.update(overrides)
    return out


def _parse(raw):
    return census.parse_output(json.dumps(raw))


def test_parse_accepts_a_good_answer_and_takes_the_last_line():
    raw = _parse(_raw())
    assert raw["nets"]["ui_in[0]"]["pd"] == [1, 1, 1]
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


def test_profile_shape_and_classes():
    p = _profile()
    assert list(p) == [
        "usb_serial",
        "date",
        "nets",
        "ties",
        "ties_undetermined",
        "tie_evidence",
        "mcu_may_drive",
        "stimulus",
    ]
    assert p["usb_serial"] == "a2961e5cac65b25f" and p["date"] == "2026-11-24"
    assert len(p["nets"]) == 26
    assert p["nets"]["ui_in[0]"] == {
        "gpio": 17,
        "read_pulldown": 1,
        "read_pullup": 1,
        "class": "driven-high",
    }
    assert p["nets"]["ui_in[4]"]["class"] == "floating"
    assert p["nets"]["uo_out[0]"]["class"] == "driven-low"
    assert p["nets"]["clk"] == {
        "gpio": 16,
        "read_pulldown": None,
        "read_pullup": None,
        "class": "mcu-configured",
    }
    assert {c["class"] for c in p["nets"].values()} <= set(census.CLASSES)


def test_ties_found_when_both_nets_are_floating_and_read_equal_both_ways():
    p = _profile()
    assert p["ties"] == [["ui_in[1]", "uio[1]"], ["ui_in[2]", "uio[2]"], ["ui_in[3]", "uio[3]"]]
    assert p["ties_undetermined"] == []
    assert p["tie_evidence"][0] == {
        "a": "ui_in[1]",
        "b": "uio[1]",
        "a_up_b_down": [0, 0],
        "a_down_b_up": [1, 1],
    }


def test_independent_nets_are_not_tied():
    ties = [
        {"a": f"ui_in[{k}]", "b": f"uio[{k}]", "tested": 1, "a_up_b_down": [1, 0], "a_down_b_up": [0, 1]}
        for k in (1, 2, 3)
    ]
    p = _profile(ties=ties)
    assert p["ties"] == [] and p["ties_undetermined"] == []
    assert len(p["tie_evidence"]) == 3


def test_mixed_readings_are_undetermined_with_a_reason():
    ties = _raw()["ties"]
    ties[0]["a_down_b_up"] = [1, 0]
    p = _profile(ties=ties)
    assert [1, 2] == [len(p["ties"]), len(p["ties_undetermined"])] or p["ties_undetermined"]
    und = p["ties_undetermined"][0]
    assert und["nets"] == ["ui_in[1]", "uio[1]"] and "mixed" in und["reason"]
    assert ["ui_in[1]", "uio[1]"] not in p["ties"]


def test_a_driven_net_makes_its_tie_undetermined_not_guessed():
    raw = _raw()
    raw["nets"]["uio[2]"]["pd"] = raw["nets"]["uio[2]"]["pu"] = [1, 1, 1]  # driven high by the FPGA
    p = census.build_profile(_parse(raw), usb_serial="s", date="2026-11-24")
    assert ["ui_in[2]", "uio[2]"] not in p["ties"]
    und = {tuple(u["nets"]): u["reason"] for u in p["ties_undetermined"]}
    assert "driven-high" in und[("ui_in[2]", "uio[2]")]
    assert ["ui_in[1]", "uio[1]"] in p["ties"]


def test_untested_pair_is_undetermined():
    raw = _raw()
    raw["ties"][1].update(tested=0, a_up_b_down=[], a_down_b_up=[])
    p = census.build_profile(_parse(raw), usb_serial="s", date="2026-11-24")
    assert ["ui_in[2]", "uio[2]"] not in p["ties"]
    assert any(
        u["nets"] == ["ui_in[2]", "uio[2]"] and "not tested" in u["reason"] for u in p["ties_undetermined"]
    )


def test_mcu_may_drive_is_the_floating_ui_in_bits_only():
    p = _profile()
    assert p["mcu_may_drive"] == [1, 2, 3, 4, 5, 6, 7]  # ui_in[0] is driven high
    raw = _raw()
    raw["nets"]["ui_in[5]"]["pd"] = raw["nets"]["ui_in[5]"]["pu"] = [0, 0, 0]
    p = census.build_profile(_parse(raw), usb_serial="s", date="2026-11-24")
    assert p["mcu_may_drive"] == [1, 2, 3, 4, 6, 7]


def test_stimulus_is_the_level_of_undriven_bits_with_the_mask_of_what_the_mcu_sets():
    p = _profile()
    assert p["stimulus"] == {"ui_in": 0x01, "uio_in": 0x00, "ui_in_mask": 0xFE}
    raw = _raw()
    raw["nets"]["ui_in[5]"]["pd"] = raw["nets"]["ui_in[5]"]["pu"] = [1, 1, 1]  # held high, not drivable
    raw["nets"]["uio[6]"]["pd"] = raw["nets"]["uio[6]"]["pu"] = [1, 1, 1]
    raw["nets"]["uio[0]"]["pd"] = raw["nets"]["uio[0]"]["pu"] = [0, 0, 0]
    p = census.build_profile(_parse(raw), usb_serial="s", date="2026-11-24")
    assert p["stimulus"] == {"ui_in": 0x21, "uio_in": 0x40, "ui_in_mask": 0xDE}


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


def test_profile_round_trips_through_validation():
    p = _profile()
    census.validate_profile(json.loads(json.dumps(p)))
    bad = copy.deepcopy(p)
    bad["nets"]["clk"]["class"] = "shiny"
    with pytest.raises(census.CensusError, match="class"):
        census.validate_profile(bad)
    bad = copy.deepcopy(p)
    bad["stimulus"]["ui_in_mask"] = "0xfe"
    with pytest.raises(census.CensusError, match="stimulus"):
        census.validate_profile(bad)


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
    assert p["stimulus"]["ui_in_mask"] == 0xFE
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
