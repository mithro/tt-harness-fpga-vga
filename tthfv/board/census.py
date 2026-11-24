# SPDX-License-Identifier: Apache-2.0
"""The pin census of a Tiny Tapeout FPGA board, and the board profile it produces.

The census script (`mp/census.py`) runs on the board's MicroPython, inside a session that is allowed to change
the board (it moves pin pulls): for every Tiny Tapeout net it reads the pin under the MCU's pull-down and under
its pull-up (three reads each), and for the candidate tie pairs `ui_in[k]`/`uio[k]` (k = 1..3) it sets a pull-up
on one and a pull-down on the other, both ways round. It never drives a pin. Everything below this docstring
is host-side and pure: parsing that fails loudly, classification, ties, `mcu_may_drive` and `stimulus`.

The profile file (`boards/<name>.json`) is data other tools read. One representation per thing: integers for
levels, masks and GPIO numbers; a fixed set of class strings (`CLASSES`); no optional shorthand.

    usb_serial, date                       the board and the UTC day of the census
    nets[name]                             gpio, read_pulldown, read_pullup (0/1), class
    ties                                   [[a, b], ...] pairs the evidence says are one net
    ties_undetermined                      [{"nets": [a, b], "reason": str}, ...] pairs it could not judge
    tie_evidence                           [{a, b, a_up_b_down: [ra, rb], a_down_b_up: [ra, rb]}, ...]
    mcu_may_drive                          ui_in bit numbers the MCU may drive
    stimulus                               {"ui_in", "uio_in", "ui_in_mask"} the board presents

Classes. With only the MCU's own weak pulls and no driving, two readings per pin can say three things:

- `floating`: reads 0 under the pull-down and 1 under the pull-up. Nothing holds the net.
- `driven-high` / `driven-low`: reads the same under both pulls. Something stronger than the MCU's pull
  (about 50 kOhm) holds it; whether that is a driver (the FPGA, a Pi GPIO) or a stiff external resistor cannot
  be told apart without driving the pin, so the name says what the reading shows, not what holds it.
- `mcu-configured`: the pin was an MCU output or a peripheral function when the census began (the project
  clock is the PWM on GPIO16), so it was not touched; its readings are null. It is not a class of the brief;
  it exists because testing such a pin would change what the board is doing.

A pin that reads 1 under the pull-down and 0 under the pull-up (against both) is an error, as is a pin whose
three reads under one pull disagree.

`stimulus`: `ui_in_mask` has a 1 for every bit in `mcu_may_drive`; `ui_in` is the level of the other bits
(`driven-high` is 1, `driven-low` is 0) with the bits the MCU may drive at 0; `uio_in` is 1 for every `uio`
net that is `driven-high`. A `floating` net is taken as 0: its true level depends on what the design's own
pad does, which the census cannot measure.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from tthfv.board import BoardError

CLASSES = ("floating", "driven-high", "driven-low", "mcu-configured")
MP_DIR = Path(__file__).parent / "mp"


class CensusError(BoardError):
    """The census answer or a profile is not what it must be."""


def _nets() -> dict[str, int]:
    nets = {f"ui_in[{i}]": 17 + i for i in range(8)}
    nets.update({f"uio[{i}]": 25 + i for i in range(8)})
    nets.update({f"uo_out[{i}]": 33 + i for i in range(8)})
    nets.update({"clk": 16, "rst_n": 14})
    return nets


# GPIOMapTTDBv3 of SDK 3.1.0 (clk 16, rst_n 14, ui_in 17..24, uio 25..32, uo_out 33..40). The census run
# compares it with the constants the SDK itself holds on the board and records any difference.
NETS: dict[str, int] = _nets()
# The constant of the SDK's GPIOMapTTDBv3 that names each net's GPIO (read from the board by the census script).
SDK_NAMES: dict[str, str] = {
    **{f"ui_in[{i}]": f"UI_IN{i}" for i in range(8)},
    **{f"uio[{i}]": f"UIO{i}" for i in range(8)},
    **{f"uo_out[{i}]": f"UO_OUT{i}" for i in range(8)},
    "clk": "RP_PROJCLK",
    "rst_n": "PROJECT_nRST",
}
PAIR_BITS = (1, 2, 3)
STATE_KEYS = {"funcsel", "oe", "level", "pue", "pde"}


def census_source() -> str:
    """The script text sent to the board: `mp/census.py` with its NETS line replaced by the literal."""
    text = (MP_DIR / "census.py").read_text()
    out, n = re.subn(
        r"^NETS = \{\}  # tthfv:nets$", f"NETS = {json.dumps(NETS)}  # tthfv:nets", text, flags=re.MULTILINE
    )
    if n != 1:
        raise CensusError("mp/census.py has no single `NETS = {}  # tthfv:nets` line")
    return out


def info_source() -> str:
    text = (MP_DIR / "info.py").read_text()
    pins = json.dumps(sorted(NETS.values()))
    out, n = re.subn(r"^PINS = \[\]  # tthfv:pins$", f"PINS = {pins}  # tthfv:pins", text, flags=re.MULTILINE)
    if n != 1:
        raise CensusError("mp/info.py has no single `PINS = []  # tthfv:pins` line")
    return out


# -- strict parsing of the board's answer --


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _keys(obj, want: set[str], what: str) -> None:
    if not isinstance(obj, dict) or set(obj) != want:
        got = sorted(obj) if isinstance(obj, dict) else type(obj).__name__
        raise CensusError(f"census answer: {what} has keys {got}, expected {sorted(want)}")


def _bits(v, n: int, what: str) -> list[int]:
    if not isinstance(v, list) or len(v) != n or not all(_is_int(x) and x in (0, 1) for x in v):
        raise CensusError(f"census answer: {what} must be a list of {n} values 0/1, got {v!r}")
    return v


def parse_output(text: str) -> dict:
    """The validated raw answer from the board's stdout (its last non-empty line must be the JSON)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise CensusError("census answer: the board printed nothing")
    try:
        raw = json.loads(lines[-1])
    except ValueError as e:
        raise CensusError(f"census answer: last line is not JSON: {lines[-1][:200]!r}") from e
    _keys(raw, {"v", "nets", "ties", "pwm_before", "pwm_after", "sdk_map"}, "the answer")
    if raw["v"] != 1:
        raise CensusError(f"census answer: version {raw['v']!r}, expected 1")
    nets = raw["nets"]
    if not isinstance(nets, dict) or set(nets) != set(NETS):
        raise CensusError(
            f"census answer: nets differ from the expected {len(NETS)}: got {sorted(nets) if isinstance(nets, dict) else nets!r}"
        )
    for name, rec in nets.items():
        _keys(rec, {"gpio", "found", "tested", "pd", "pu", "restored"}, f"net {name}")
        if rec["gpio"] != NETS[name]:
            raise CensusError(f"census answer: net {name} is GPIO {rec['gpio']!r}, expected {NETS[name]}")
        _keys(rec["found"], STATE_KEYS, f"net {name} found")
        if not all(_is_int(v) for v in rec["found"].values()):
            raise CensusError(f"census answer: net {name} found has non-integer values: {rec['found']}")
        if rec["tested"] not in (0, 1) or not _is_int(rec["tested"]):
            raise CensusError(f"census answer: net {name} tested must be 0 or 1, got {rec['tested']!r}")
        n = 3 if rec["tested"] else 0
        _bits(rec["pd"], n, f"net {name} pd")
        _bits(rec["pu"], n, f"net {name} pu")
        if rec["restored"] != 1 or not _is_int(rec["restored"]):
            raise CensusError(
                f"census answer: net {name} was NOT restored to its original mode and pull (restored={rec['restored']!r})"
            )
    ties = raw["ties"]
    want = [(f"ui_in[{k}]", f"uio[{k}]") for k in PAIR_BITS]
    if not isinstance(ties, list) or len(ties) != len(want):
        raise CensusError(f"census answer: ties must be a list of {len(want)} pair records, got {ties!r}")
    for rec, (a, b) in zip(ties, want, strict=True):
        _keys(rec, {"a", "b", "tested", "a_up_b_down", "a_down_b_up"}, f"ties record {a}/{b}")
        if (rec["a"], rec["b"]) != (a, b):
            raise CensusError(f"census answer: ties record is for {rec['a']}/{rec['b']}, expected {a}/{b}")
        if rec["tested"] not in (0, 1) or not _is_int(rec["tested"]):
            raise CensusError(f"census answer: ties record {a}/{b} tested must be 0 or 1")
        n = 2 if rec["tested"] else 0
        _bits(rec["a_up_b_down"], n, f"ties record {a}/{b} a_up_b_down")
        _bits(rec["a_down_b_up"], n, f"ties record {a}/{b} a_down_b_up")
        if rec["tested"] and not (nets[a]["tested"] and nets[b]["tested"]):
            raise CensusError(
                f"census answer: ties record {a}/{b} claims a test of a net that was not tested"
            )
    for key in ("pwm_before", "pwm_after"):
        v = raw[key]
        if not isinstance(v, list) or not all(_is_int(x) for x in v) or len(v) not in (0, 4):
            raise CensusError(f"census answer: {key} must be [] or 4 integers, got {v!r}")
    if raw["pwm_before"] != raw["pwm_after"]:
        raise CensusError(
            f"census answer: the clock PWM registers changed: {raw['pwm_before']} -> {raw['pwm_after']}"
        )
    if not isinstance(raw["sdk_map"], dict) or not all(_is_int(v) for v in raw["sdk_map"].values()):
        raise CensusError("census answer: sdk_map must be an object of integers")
    mismatches = sdk_map_mismatches(raw["sdk_map"])
    if mismatches:
        raise CensusError(
            "census answer: our GPIO map differs from the SDK's on the board: " + "; ".join(mismatches)
        )
    return raw


# -- classification and derivation --


def classify(pd: int, pu: int) -> str:
    if (pd, pu) == (0, 1):
        return "floating"
    if (pd, pu) == (1, 1):
        return "driven-high"
    if (pd, pu) == (0, 0):
        return "driven-low"
    raise CensusError("reads 1 under the pull-down and 0 under the pull-up: against both pulls")


def _reduce(name: str, reads: list[int], which: str) -> int:
    if len(set(reads)) != 1:
        raise CensusError(f"net {name}: the three reads under the {which} are unstable: {reads}")
    return reads[0]


def _judge_tie(a: str, b: str, rec: dict, classes: dict[str, str]) -> tuple[bool | None, str]:
    """(True, why) tied, (False, why) not tied, (None, reason) cannot be judged."""
    for n in (a, b):
        if classes[n] != "floating":
            return None, f"{n} is {classes[n]}: both readings follow what holds it, so a tie cannot be judged"
    if not rec["tested"]:
        return None, "pair not tested"
    c1, c2 = rec["a_up_b_down"], rec["a_down_b_up"]
    if c1[0] == c1[1] and c2[0] == c2[1]:
        return True, "equal readings under opposed pulls, both ways round"
    if c1 == [1, 0] and c2 == [0, 1]:
        return False, "each follows its own pull"
    return None, f"mixed readings (a up/b down {c1}, a down/b up {c2}): neither the same net nor independent"


def mcu_may_drive(classes: dict[str, str]) -> list[int]:
    """ui_in bits with nothing on them but the pulls."""
    return [i for i in range(8) if classes[f"ui_in[{i}]"] == "floating"]


def stimulus(classes: dict[str, str], may_drive: list[int]) -> dict:
    ui_in = uio_in = 0
    for i in range(8):
        if classes[f"ui_in[{i}]"] == "mcu-configured":
            raise CensusError(f"ui_in[{i}] was configured by the MCU: the stimulus cannot be derived")
        if i not in may_drive and classes[f"ui_in[{i}]"] == "driven-high":
            ui_in |= 1 << i
        if classes[f"uio[{i}]"] == "mcu-configured":
            raise CensusError(f"uio[{i}] was configured by the MCU: the stimulus cannot be derived")
        if classes[f"uio[{i}]"] == "driven-high":
            uio_in |= 1 << i
    mask = sum(1 << i for i in may_drive)
    return {"ui_in": ui_in, "uio_in": uio_in, "ui_in_mask": mask}


def build_profile(raw: dict, *, usb_serial: str, date: str) -> dict:
    nets: dict[str, dict] = {}
    classes: dict[str, str] = {}
    for name, gpio in NETS.items():
        rec = raw["nets"][name]
        if rec["tested"]:
            pd = _reduce(name, rec["pd"], "pull-down")
            pu = _reduce(name, rec["pu"], "pull-up")
            try:
                cls = classify(pd, pu)
            except CensusError as e:
                raise CensusError(f"net {name}: {e}") from None
            nets[name] = {"gpio": gpio, "read_pulldown": pd, "read_pullup": pu, "class": cls}
        else:
            cls = "mcu-configured"
            nets[name] = {"gpio": gpio, "read_pulldown": None, "read_pullup": None, "class": cls}
        classes[name] = cls
    ties, undetermined, evidence = [], [], []
    for rec in raw["ties"]:
        a, b = rec["a"], rec["b"]
        verdict, why = _judge_tie(a, b, rec, classes)
        if verdict:
            ties.append([a, b])
        elif verdict is None:
            undetermined.append({"nets": [a, b], "reason": why})
        if rec["tested"]:
            evidence.append(
                {"a": a, "b": b, "a_up_b_down": rec["a_up_b_down"], "a_down_b_up": rec["a_down_b_up"]}
            )
    may = mcu_may_drive(classes)
    return {
        "usb_serial": usb_serial,
        "date": date,
        "nets": nets,
        "ties": ties,
        "ties_undetermined": undetermined,
        "tie_evidence": evidence,
        "mcu_may_drive": may,
        "stimulus": stimulus(classes, may),
    }


def validate_profile(p: dict) -> None:
    """Strict check of a profile (as written, or as read back by another tool)."""
    _keys(
        p,
        {
            "usb_serial",
            "date",
            "nets",
            "ties",
            "ties_undetermined",
            "tie_evidence",
            "mcu_may_drive",
            "stimulus",
        },
        "profile",
    )
    if not isinstance(p["usb_serial"], str) or not re.fullmatch(r"\d{4}-\d\d-\d\d", str(p["date"])):
        raise CensusError("profile: usb_serial must be a string and date an ISO day")
    if not isinstance(p["nets"], dict) or set(p["nets"]) != set(NETS):
        raise CensusError("profile: nets must be exactly the 26 Tiny Tapeout nets")
    for name, rec in p["nets"].items():
        _keys(rec, {"gpio", "read_pulldown", "read_pullup", "class"}, f"profile net {name}")
        if rec["class"] not in CLASSES:
            raise CensusError(f"profile net {name}: class {rec['class']!r} is not one of {CLASSES}")
        want_null = rec["class"] == "mcu-configured"
        for k in ("read_pulldown", "read_pullup"):
            ok = rec[k] is None if want_null else (_is_int(rec[k]) and rec[k] in (0, 1))
            if not ok:
                raise CensusError(f"profile net {name}: {k} is {rec[k]!r} for class {rec['class']}")
    _keys(p["stimulus"], {"ui_in", "uio_in", "ui_in_mask"}, "profile stimulus")
    if not all(_is_int(v) and 0 <= v <= 255 for v in p["stimulus"].values()):
        raise CensusError(f"profile stimulus values must be integers 0..255: {p['stimulus']}")
    if not isinstance(p["mcu_may_drive"], list) or not all(
        _is_int(i) and 0 <= i < 8 for i in p["mcu_may_drive"]
    ):
        raise CensusError("profile mcu_may_drive must be a list of ui_in bit numbers")
    for key in ("ties", "ties_undetermined", "tie_evidence"):
        if not isinstance(p[key], list):
            raise CensusError(f"profile {key} must be a list")


def _comparable(name: str, rec: dict) -> dict:
    """What of a net's record must agree between two runs. A `uo_out` net is an output of whatever design is
    loaded (the idle display animates), so only whether something drives it is compared, not its level."""
    if name.startswith("uo_out["):
        return {"gpio": rec["gpio"], "class": rec["class"].split("-")[0]}
    return rec


def differences(a: dict, b: dict) -> list[str]:
    """What differs between two profiles, ignoring the date and the level of `uo_out` nets; empty if they
    agree."""
    out = []
    for key, value in a.items():
        if key == "date":
            continue
        if key == "nets":
            for name, rec in value.items():
                other = b["nets"].get(name)
                if other is None or _comparable(name, rec) != _comparable(name, other):
                    out.append(f"{name}: {rec} != {other}")
        elif value != b.get(key):
            out.append(f"{key}: {value} != {b.get(key)}")
    return out


def today() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")


def sdk_map_mismatches(sdk_map: dict) -> list[str]:
    """Where the SDK's own GPIO constants (GPIOMapTTDBv3, as the board holds them) differ from NETS."""
    if not sdk_map:
        return ["the SDK's GPIOMapTTDBv3 was not found on the board"]
    out = []
    for net, name in SDK_NAMES.items():
        if sdk_map.get(name) != NETS[net]:
            out.append(f"{net}: SDK {name} is {sdk_map.get(name)!r}, we use GPIO {NETS[net]}")
    return out
