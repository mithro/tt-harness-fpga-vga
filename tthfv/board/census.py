# SPDX-License-Identifier: Apache-2.0
"""The pin census of a Tiny Tapeout FPGA board, and the board profile it produces.

The census script (`mp/census.py`) runs on the board's MicroPython, inside a session that is allowed to change
the board (it moves pin pulls): for every Tiny Tapeout net it reads the pin under the MCU's pull-down and under
its pull-up (three reads each), and for ALL eight candidate tie pairs `ui_in[k]`/`uio[k]` (k = 0..7) it sets a
pull-up on one and a pull-down on the other, both ways round. It never drives a pin. Pairs that nobody expects
to be connected are the negative control: they must read [1, 0] then [0, 1]. Everything below this docstring
is host-side and pure: parsing that fails loudly, classification, ties, `mcu_may_drive`, `stimulus` and
`effective_ui_in_mask`.

The profile file (`boards/<name>.json`) is data other tools read. One representation per thing: integers for
levels, masks and GPIO numbers; fixed sets of strings (`CLASSES`, `BASES`, `VERDICTS`); no optional shorthand.

    usb_serial, date                       the board and the UTC day of the census
    nets[name]                             gpio, read_pulldown, read_pullup (0/1 or null), class, basis
    ties                                   [[a, b], ...] the pairs whose tie_tests verdict is "tied"
    tie_tests                              one per pair k = 0..7: a, b, a_up_b_down [ra, rb], a_down_b_up [ra, rb]
                                           ([] if not tested), verdict, reason
    drive_conflicts                        [{"ui_in": k, "uio": k}, ...] one per tie (see below)
    mcu_may_drive                          ui_in bit numbers the MCU may drive
    stimulus                               ui_in, uio_in, ui_in_mask, uio_in_uncertain,
                                           floating_assumed_low {"ui_in", "uio"} (all masks, 0..255)

Classes. With only the MCU's own weak pulls and no driving, two readings per pin can say three things; `basis`
says how much the class is a measurement:

- `floating` (basis `measured`): reads 0 under the pull-down and 1 under the pull-up. Nothing stronger than
  the MCU's pull holds the net.
- `driven-high` / `driven-low` (basis `indistinguishable`): reads the same under both pulls. Something stronger
  than the MCU's pull (about 50 kOhm) holds it; a driver (the FPGA design, a Pi GPIO) cannot be told from a
  stiff external resistor without driving the pin. The class names are kept; the basis says what they mean.
- `mcu-configured` (basis `not-tested`): the pin was an MCU output or a peripheral function when the census
  began (the project clock is the PWM on GPIO16), so it was not touched; its readings are null.

A pin that reads 1 under the pull-down and 0 under the pull-up (against both) is an error, as is a pin whose
three reads under one pull disagree. `tied` means equal readings both ways round: consistent with a connection
(a resistive link would read the same), not proof. `independent` means [1, 0] then [0, 1].

Two outputs fighting. `drive_conflicts` lists every tie between a `ui_in` bit and a `uio` bit. If the MCU
drives `ui_in[k]` while a loaded design enables `uio[k]` as an output (its `uio_oe` bit k), two outputs are
connected together. `effective_ui_in_mask(profile, uio_oe_possible)` is the rule: the profile's `ui_in_mask`
minus every `ui_in` bit with a conflict whose `uio` bit is set in `uio_oe_possible`, the mask of uio bits the
design can ever enable as outputs (0xFF when unknown: on fpga-4 bits 1..3 are then dropped).

`stimulus`: `ui_in_mask` has a 1 for every bit in `mcu_may_drive`; `ui_in` is the level of the other bits
(`driven-high` is 1, `driven-low` is 0) with the bits the MCU may drive at 0; `uio_in` is 1 for every `uio`
net that is `driven-high`. `uio_in_uncertain` is the mask of `uio` bits that are `driven-*`: their value may
come from the design that was loaded during the census rather than from the board. `floating_assumed_low` is
the masks of `ui_in` and `uio` bits taken as 0 only because the SDK leaves its own pull-down on them (and the
Pi's pulls may sit on the same nets): not measured at the FPGA.
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
PAIR_BITS = tuple(range(8))
BASES = ("measured", "indistinguishable", "not-tested")
VERDICTS = ("tied", "independent", "undetermined")
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
    _keys(
        raw,
        {"v", "nets", "ties", "pwm_before", "pwm_after", "sdk_map", "restore_failed", "mem_free", "retries"},
        "the answer",
    )
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
    failed = raw["restore_failed"]
    if not isinstance(failed, list) or not all(isinstance(x, str) for x in failed):
        raise CensusError(f"census answer: restore_failed must be a list of net names, got {failed!r}")
    if failed:
        raise CensusError(f"census answer: restore failed for {failed}: those pins may not be as found")
    heap = raw["mem_free"]
    if not isinstance(heap, list) or len(heap) != 2 or not all(_is_int(x) and x >= 0 for x in heap):
        raise CensusError(f"census answer: mem_free must be [before, after] integers, got {heap!r}")
    if not _is_int(raw["retries"]) or raw["retries"] < 0:
        raise CensusError(f"census answer: retries must be a count, got {raw['retries']!r}")
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


def _judge_tie(a: str, b: str, rec: dict, classes: dict[str, str]) -> tuple[str, str]:
    """(verdict, reason) for one pair; a verdict is one of VERDICTS."""
    for n in (a, b):
        if classes[n] != "floating":
            return (
                "undetermined",
                f"{n} is {classes[n]}: both readings follow what holds it, so a tie cannot be judged",
            )
    if not rec["tested"]:
        return "undetermined", "pair not tested"
    c1, c2 = rec["a_up_b_down"], rec["a_down_b_up"]
    if c1[0] == c1[1] and c2[0] == c2[1]:
        return "tied", (
            "equal readings under opposed pulls, both ways round (consistent with a connection; a resistive "
            "link would read the same)"
        )
    if c1 == [1, 0] and c2 == [0, 1]:
        return "independent", "each net follows its own pull"
    return (
        "undetermined",
        f"mixed readings (a up/b down {c1}, a down/b up {c2}): neither the same net nor independent",
    )


def mcu_may_drive(classes: dict[str, str]) -> list[int]:
    """ui_in bits with nothing on them but the pulls."""
    return [i for i in range(8) if classes[f"ui_in[{i}]"] == "floating"]


def stimulus(classes: dict[str, str], may_drive: list[int]) -> dict:
    ui_in = uio_in = uio_uncertain = ui_floating = uio_floating = 0
    for i in range(8):
        for net in (f"ui_in[{i}]", f"uio[{i}]"):
            if classes[net] == "mcu-configured":
                raise CensusError(f"{net} was configured by the MCU: the stimulus cannot be derived")
        if i not in may_drive and classes[f"ui_in[{i}]"] == "driven-high":
            ui_in |= 1 << i
        if classes[f"ui_in[{i}]"] == "floating":
            ui_floating |= 1 << i
        if classes[f"uio[{i}]"] == "driven-high":
            uio_in |= 1 << i
        if classes[f"uio[{i}]"].startswith("driven"):
            uio_uncertain |= 1 << i
        if classes[f"uio[{i}]"] == "floating":
            uio_floating |= 1 << i
    mask = sum(1 << i for i in may_drive)
    return {
        "ui_in": ui_in,
        "uio_in": uio_in,
        "ui_in_mask": mask,
        "uio_in_uncertain": uio_uncertain,
        "floating_assumed_low": {"ui_in": ui_floating, "uio": uio_floating},
    }


BASIS_OF = {"floating": "measured", "driven-high": "indistinguishable", "driven-low": "indistinguishable",
            "mcu-configured": "not-tested"}  # fmt: skip


def _drive_conflicts(ties: list) -> list[dict]:
    out = []
    for a, b in ties:
        ma, mb = re.fullmatch(r"ui_in\[(\d)\]", a), re.fullmatch(r"uio\[(\d)\]", b)
        if ma and mb:
            out.append({"ui_in": int(ma.group(1)), "uio": int(mb.group(1))})
    return out


def effective_ui_in_mask(profile: dict, uio_oe_possible: int) -> int:
    """The `ui_in` bits the MCU may drive for a design that can enable the `uio` bits in `uio_oe_possible` as
    outputs (0xFF when unknown): the profile's `ui_in_mask` minus every `ui_in` bit whose tied `uio` bit is in
    that mask, because driving both ends of a tie makes two outputs fight."""
    if not _is_int(uio_oe_possible) or not 0 <= uio_oe_possible <= 255:
        raise CensusError(f"uio_oe_possible must be an integer 0..255, got {uio_oe_possible!r}")
    mask = profile["stimulus"]["ui_in_mask"]
    for c in profile["drive_conflicts"]:
        if uio_oe_possible >> c["uio"] & 1:
            mask &= ~(1 << c["ui_in"])
    return mask


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
        else:
            cls, pd, pu = "mcu-configured", None, None
        nets[name] = {
            "gpio": gpio,
            "read_pulldown": pd,
            "read_pullup": pu,
            "class": cls,
            "basis": BASIS_OF[cls],
        }
        classes[name] = cls
    ties, tests = [], []
    for rec in raw["ties"]:
        a, b = rec["a"], rec["b"]
        verdict, why = _judge_tie(a, b, rec, classes)
        if verdict == "tied":
            ties.append([a, b])
        tests.append(
            {
                "a": a,
                "b": b,
                "a_up_b_down": rec["a_up_b_down"],
                "a_down_b_up": rec["a_down_b_up"],
                "verdict": verdict,
                "reason": why,
            }
        )
    may = mcu_may_drive(classes)
    return {
        "usb_serial": usb_serial,
        "date": date,
        "nets": nets,
        "ties": ties,
        "tie_tests": tests,
        "drive_conflicts": _drive_conflicts(ties),
        "mcu_may_drive": may,
        "stimulus": stimulus(classes, may),
    }


def _byte(v) -> bool:
    return _is_int(v) and 0 <= v <= 255


def validate_profile(p: dict) -> None:
    """Strict check of a profile (as written, or as read back by another tool)."""
    _keys(
        p,
        {
            "usb_serial",
            "date",
            "nets",
            "ties",
            "tie_tests",
            "drive_conflicts",
            "mcu_may_drive",
            "stimulus",
        },
        "profile",
    )
    if not isinstance(p["usb_serial"], str) or not re.fullmatch(r"\d{4}-\d\d-\d\d", str(p["date"])):
        raise CensusError("profile: usb_serial must be a string and date an ISO day")
    if not isinstance(p["nets"], dict) or set(p["nets"]) != set(NETS):
        raise CensusError("profile: nets must be exactly the 26 Tiny Tapeout nets")
    reads_of = {
        "floating": (0, 1),
        "driven-high": (1, 1),
        "driven-low": (0, 0),
        "mcu-configured": (None, None),
    }
    for name, rec in p["nets"].items():
        _keys(rec, {"gpio", "read_pulldown", "read_pullup", "class", "basis"}, f"profile net {name}")
        if rec["class"] not in CLASSES:
            raise CensusError(f"profile net {name}: class {rec['class']!r} is not one of {CLASSES}")
        if rec["gpio"] != NETS[name]:
            raise CensusError(f"profile net {name}: gpio {rec['gpio']!r}, expected {NETS[name]}")
        if (rec["read_pulldown"], rec["read_pullup"]) != reads_of[rec["class"]]:
            raise CensusError(
                f"profile net {name}: reads ({rec['read_pulldown']!r}, {rec['read_pullup']!r}) do not agree "
                f"with class {rec['class']}"
            )
        if rec["basis"] not in BASES or rec["basis"] != BASIS_OF[rec["class"]]:
            raise CensusError(f"profile net {name}: basis {rec['basis']!r} is wrong for class {rec['class']}")
    tests = p["tie_tests"]
    if not isinstance(tests, list) or len(tests) != len(PAIR_BITS):
        raise CensusError(f"profile: tie_tests must hold the {len(PAIR_BITS)} pairs ui_in[k]/uio[k]")
    for k, t in zip(PAIR_BITS, tests, strict=True):
        _keys(t, {"a", "b", "a_up_b_down", "a_down_b_up", "verdict", "reason"}, f"profile tie_tests[{k}]")
        if (t["a"], t["b"]) != (f"ui_in[{k}]", f"uio[{k}]"):
            raise CensusError(f"profile tie_tests[{k}] is for {t['a']}/{t['b']}")
        if t["verdict"] not in VERDICTS:
            raise CensusError(f"profile tie_tests[{k}]: verdict {t['verdict']!r} is not one of {VERDICTS}")
        for key in ("a_up_b_down", "a_down_b_up"):
            if t[key] != [] and not (
                isinstance(t[key], list)
                and len(t[key]) == 2
                and all(_is_int(x) and x in (0, 1) for x in t[key])
            ):
                raise CensusError(f"profile tie_tests[{k}].{key} must be [] or two values 0/1")
        if not isinstance(t["reason"], str) or not t["reason"]:
            raise CensusError(f"profile tie_tests[{k}]: reason must be a non-empty string")
    tied = [[t["a"], t["b"]] for t in tests if t["verdict"] == "tied"]
    if p["ties"] != tied:
        raise CensusError(
            f"profile: ties {p['ties']} are not the pairs whose tie_tests verdict is tied: {tied}"
        )
    if p["drive_conflicts"] != _drive_conflicts(tied):
        raise CensusError(f"profile: drive_conflicts {p['drive_conflicts']} do not follow from ties {tied}")
    st = p["stimulus"]
    _keys(
        st, {"ui_in", "uio_in", "ui_in_mask", "uio_in_uncertain", "floating_assumed_low"}, "profile stimulus"
    )
    if not all(_byte(st[k]) for k in ("ui_in", "uio_in", "ui_in_mask", "uio_in_uncertain")):
        raise CensusError(f"profile stimulus values must be integers 0..255: {st}")
    fl = st["floating_assumed_low"]
    if not isinstance(fl, dict) or set(fl) != {"ui_in", "uio"} or not all(_byte(v) for v in fl.values()):
        raise CensusError(
            f"profile stimulus floating_assumed_low must be {{ui_in, uio}} integers 0..255: {fl!r}"
        )
    if not isinstance(p["mcu_may_drive"], list) or not all(
        _is_int(i) and 0 <= i < 8 for i in p["mcu_may_drive"]
    ):
        raise CensusError("profile mcu_may_drive must be a list of ui_in bit numbers")
    _check_derived(p)


def _check_derived(p: dict) -> None:
    """The fields other tools trust are re-derived with the functions that built them and must be equal."""
    classes = {name: rec["class"] for name, rec in p["nets"].items()}
    for k, t in zip(PAIR_BITS, p["tie_tests"], strict=True):
        if bool(t["a_up_b_down"]) != bool(t["a_down_b_up"]):
            raise CensusError(f"profile tie_tests[{k}]: only one of the two readings is present")
        rec = {
            "tested": 1 if t["a_up_b_down"] else 0,
            "a_up_b_down": t["a_up_b_down"],
            "a_down_b_up": t["a_down_b_up"],
        }
        verdict, _ = _judge_tie(t["a"], t["b"], rec, classes)
        if verdict != t["verdict"]:
            raise CensusError(
                f"profile tie_tests[{k}]: verdict {t['verdict']!r} does not follow from its readings "
                f"{t['a_up_b_down']}/{t['a_down_b_up']} and the nets' classes (they give {verdict!r})"
            )
    may = mcu_may_drive(classes)
    if p["mcu_may_drive"] != may:
        raise CensusError(f"profile mcu_may_drive {p['mcu_may_drive']} does not follow from the nets: {may}")
    try:
        want = stimulus(classes, may)
    except CensusError as e:
        raise CensusError(f"profile stimulus cannot be derived from the nets: {e}") from None
    if p["stimulus"] != want:
        raise CensusError(f"profile stimulus {p['stimulus']} does not follow from the nets: {want}")


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
