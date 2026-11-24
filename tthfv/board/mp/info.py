# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: BLE001, UP031
# Read-only look at a Tiny Tapeout board before the census: runtime, SDK, chip, and what each project pin is
# set to right now. It configures no pin (Pin(n) with no other argument only wraps the pin), writes no
# register, creates no SDK object and prints ONE JSON line. The host replaces the PINS line with a literal
# list of GPIO numbers.
PINS = []  # tthfv:pins


def _tthfv_info():
    import gc
    import json
    import os
    import sys

    import machine

    r = {}

    def put(k, f):
        try:
            r[k] = f()
        except Exception as e:
            r[k] = "error: " + repr(e)

    put("implementation", lambda: [sys.implementation.name, list(sys.implementation.version)])
    put("machine", lambda: os.uname().machine)
    put("version", lambda: sys.version)
    put("freq_hz", machine.freq)
    put("unique_id", lambda: "".join("%02x" % b for b in machine.unique_id()))
    gc.collect()
    put("mem_free", gc.mem_free)
    m = machine.mem32
    put("chip_id_reg", lambda: m[0x40000000] & 0xFFFFFFFF)
    g = globals()
    r["sdk_present"] = "tt" in g
    if "tt" in g:
        t = g["tt"]
        put("sdk_version", lambda: t.version)
        put("sdk_revision", lambda: t.revision)
        put("enabled", lambda: t.shuttle.enabled.name if t.shuttle.enabled else None)
        put("mode", lambda: t.mode_str)
        put("pins_attrs", lambda: [a for a in dir(t.pins) if not a.startswith("_")])
    mod = sys.modules.get("ttboard.pins.gpio_map")
    r["gpio_map_module"] = mod is not None
    cls = getattr(mod, "GPIOMapTTDBv3", None) if mod else None
    if cls is not None:
        r["gpio_map"] = {
            k: getattr(cls, k) for k in dir(cls) if isinstance(getattr(cls, k), int) and k[0] != "_"
        }
    pins = {}
    for n in PINS:
        st = m[0x40028000 + 8 * n]
        ctrl = m[0x40028000 + 8 * n + 4]
        pad = m[0x40038000 + 4 + 4 * n]
        rec = {
            "funcsel": ctrl & 0x1F,
            "oe": (st >> 13) & 1,
            "level": (st >> 17) & 1,
            "pue": (pad >> 3) & 1,
            "pde": (pad >> 2) & 1,
        }
        put("_p", lambda n=n: repr(machine.Pin(n)))
        rec["repr"] = r.pop("_p")
        pins[str(n)] = rec
    r["pins"] = pins
    print(json.dumps(r))


_tthfv_info()
del _tthfv_info
