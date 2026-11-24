# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: PLC0206
# Pin census, run on a Tiny Tapeout board's MicroPython (RP2350). The host replaces the NETS line below with a
# literal {net name: GPIO number}; nothing else is substituted. Prints ONE JSON line.
#
# Rules this script keeps (tests/test_census_script.py checks them under CPython with a model of the chip):
# - No pin is ever driven as an output: the only Pin() calls are Pin.IN, with a pull-up, a pull-down or the
#   pull the pin had.
# - A pin the MCU itself drives (output enable set) or that is not a plain SIO input (the project clock is the
#   PWM on GPIO16) is not touched: it is read passively from the pad and reported as not tested.
# - Every pin it touched is put back (Pin.IN with its original pull) in a `finally`, even if a read raises,
#   and the script reports whether the pad registers read back as found.
# - The clock PWM registers are read before and after and reported; the host fails if they differ.
# - It reads nothing from the REPL input and does not touch the keyboard interrupt, so a Ctrl-C always stops it.
NETS = {}  # tthfv:nets
SETTLE_MS = 2
READS = 3
PAIRS = (("ui_in[1]", "uio[1]"), ("ui_in[2]", "uio[2]"), ("ui_in[3]", "uio[3]"))


def _tthfv_census():
    import json
    import sys
    import time

    import machine

    Pin = machine.Pin
    m = machine.mem32
    io = 0x40028000  # IO_BANK0: GPIOn STATUS at 8n, CTRL at 8n+4
    pads = 0x40038000  # PADS_BANK0: GPIOn at 4 + 4n; PDE bit 2, PUE bit 3
    pwm = (0x400A8000, 0x400A8004, 0x400A8010, 0x400A80F0)  # slice 0 CSR, DIV, TOP; EN
    out = {"v": 1, "nets": {}, "ties": []}

    def state(n):
        st = m[io + 8 * n]
        ctrl = m[io + 8 * n + 4]
        pad = m[pads + 4 + 4 * n]
        return {
            "funcsel": ctrl & 0x1F,
            "oe": (st >> 13) & 1,
            "level": (st >> 17) & 1,
            "pue": (pad >> 3) & 1,
            "pde": (pad >> 2) & 1,
        }

    def pwm_regs():
        if not (m[0x40020008] & 0x10000):  # RESETS_RESET_DONE: PWM block out of reset
            return []
        return [m[a] & 0xFFFFFFFF for a in pwm]

    def pull_of(s):
        return Pin.PULL_UP if s["pue"] else (Pin.PULL_DOWN if s["pde"] else None)

    def reads(pin, pull):
        Pin(pin, Pin.IN, pull)
        time.sleep_ms(SETTLE_MS)
        got = []
        for _ in range(READS):
            got.append(Pin(pin).value())
            time.sleep_ms(SETTLE_MS)
        return got

    out["pwm_before"] = pwm_regs()
    found = {}
    touched = []
    try:
        for name in NETS:
            n = NETS[name]
            s = state(n)
            found[name] = s
            testable = s["funcsel"] == 5 and s["oe"] == 0 and not (s["pue"] and s["pde"])
            out["nets"][name] = {
                "gpio": n,
                "found": s,
                "tested": 1 if testable else 0,
                "pd": [],
                "pu": [],
            }
        for name in NETS:
            rec = out["nets"][name]
            if rec["tested"]:
                n = NETS[name]
                touched.append(name)
                rec["pd"] = reads(n, Pin.PULL_DOWN)
                rec["pu"] = reads(n, Pin.PULL_UP)
                Pin(n, Pin.IN, pull_of(found[name]))
        for a, b in PAIRS:
            ra, rb = out["nets"].get(a), out["nets"].get(b)
            tie = {"a": a, "b": b, "tested": 0, "a_up_b_down": [], "a_down_b_up": []}
            if ra and rb and ra["tested"] and rb["tested"]:
                na, nb = NETS[a], NETS[b]
                tie["tested"] = 1
                Pin(na, Pin.IN, Pin.PULL_UP)
                Pin(nb, Pin.IN, Pin.PULL_DOWN)
                time.sleep_ms(SETTLE_MS)
                tie["a_up_b_down"] = [Pin(na).value(), Pin(nb).value()]
                Pin(na, Pin.IN, Pin.PULL_DOWN)
                Pin(nb, Pin.IN, Pin.PULL_UP)
                time.sleep_ms(SETTLE_MS)
                tie["a_down_b_up"] = [Pin(na).value(), Pin(nb).value()]
                Pin(na, Pin.IN, pull_of(found[a]))
                Pin(nb, Pin.IN, pull_of(found[b]))
            out["ties"].append(tie)
    finally:
        for name in touched:
            Pin(NETS[name], Pin.IN, pull_of(found[name]))
    for name in NETS:
        now = state(NETS[name])
        was = found[name]
        keys = ("funcsel", "oe", "pue", "pde")
        out["nets"][name]["restored"] = 1 if all(now[k] == was[k] for k in keys) else 0
    out["pwm_after"] = pwm_regs()
    sdk = {}
    mod = sys.modules.get("ttboard.pins.gpio_map")
    cls = getattr(mod, "GPIOMapTTDBv3", None) if mod else None
    if cls is not None:
        for k in dir(cls):
            v = getattr(cls, k)
            if isinstance(v, int) and not k.startswith("_"):
                sdk[k] = v
    out["sdk_map"] = sdk
    print(json.dumps(out))


_tthfv_census()
del _tthfv_census
