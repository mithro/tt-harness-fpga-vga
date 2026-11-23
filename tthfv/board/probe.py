# SPDX-License-Identifier: Apache-2.0
"""A read-only probe of a board: one raw-REPL script that changes nothing.

It configures no pin, touches no PWM, writes no file, loads no design and creates no SDK object: it reads the
already-existing global `tt` if the SDK left one (never constructs a DemoBoard), and reads a few registers with
`machine.mem32`. Register addresses are from pico-sdk 2.1.1, `src/rp2350/hardware_regs/include/hardware/regs/`
(`addressmap.h`, `sysinfo.h`, `io_bank0.h`, `resets.h`, `pwm.h`); GPIO16 is PWM slice 0 channel A
(`PWM_GPIO_SLICE_NUM` in `hardware_pwm/include/hardware/pwm.h`, `IO_BANK0_GPIO16_CTRL_FUNCSEL_VALUE_PWM_A_0`).
The registers are read only on an RP2350 (by `os.uname().machine`), and the PWM block only once RESETS says it
is out of reset, since a peripheral held in reset must not be accessed.
"""

from __future__ import annotations

# Register addresses (RP2350).
SYSINFO_CHIP_ID = 0x40000000  # SYSINFO_BASE + SYSINFO_CHIP_ID_OFFSET (0x0)
RESETS_RESET_DONE = 0x40020008  # RESETS_BASE + RESETS_RESET_DONE_OFFSET (0x8)
RESET_DONE_PWM = 0x00010000  # RESETS_RESET_DONE_PWM_BITS
IO_BANK0_GPIO16_STATUS = 0x40028080  # IO_BANK0_BASE + IO_BANK0_GPIO16_STATUS_OFFSET (0x80)
IO_BANK0_GPIO16_CTRL = 0x40028084  # + IO_BANK0_GPIO16_CTRL_OFFSET (0x84)
PWM_CH0_CSR = 0x400A8000  # PWM_BASE + PWM_CH0_CSR_OFFSET (0x0)
PWM_CH0_DIV = 0x400A8004  # + PWM_CH0_DIV_OFFSET (0x4)
PWM_CH0_TOP = 0x400A8010  # + PWM_CH0_TOP_OFFSET (0x10)
PWM_EN = 0x400A80F0  # + PWM_EN_OFFSET (0xf0); bit 0 = slice 0

FUNCSEL = {0x00: "hstx", 0x01: "spi", 0x02: "uart", 0x03: "i2c", 0x04: "pwm", 0x05: "sio", 0x06: "pio0",
           0x07: "pio1", 0x08: "pio2", 0x0A: "usb", 0x1F: "null"}  # fmt: skip

PROBE_SOURCE = f"""\
def _tthfv_probe():
    import sys, os, gc, machine, json
    r = {{}}
    def put(k, f):
        try:
            r[k] = f()
        except Exception as e:
            r[k] = 'error: ' + repr(e)
    put('implementation', lambda: [sys.implementation.name, list(sys.implementation.version),
                                   getattr(sys.implementation, '_machine', None)])
    put('version', lambda: sys.version)
    put('uname', lambda: list(os.uname()))
    put('freq_hz', machine.freq)
    put('mem_free_before_collect', gc.mem_free)
    gc.collect()
    put('mem_free', gc.mem_free)
    put('mem_alloc', gc.mem_alloc)
    g = globals()
    r['sdk_present'] = 'tt' in g
    if 'tt' in g:
        t = g['tt']
        put('sdk_version', lambda: t.version)
        put('sdk_revision', lambda: t.revision)
        put('shuttle_run', lambda: t.shuttle.run)
        put('enabled', lambda: t.shuttle.enabled.name if t.shuttle.enabled else None)
        put('mode', lambda: t.mode_str)
        put('is_auto_clocking', lambda: bool(t.is_auto_clocking))
        put('auto_clocking_freq', lambda: t.auto_clocking_freq)
    if 'RP2350' in os.uname().machine:
        m = machine.mem32
        regs = {{}}
        regs['SYSINFO_CHIP_ID'] = m[{SYSINFO_CHIP_ID:#x}]
        regs['IO_BANK0_GPIO16_STATUS'] = m[{IO_BANK0_GPIO16_STATUS:#x}]
        regs['IO_BANK0_GPIO16_CTRL'] = m[{IO_BANK0_GPIO16_CTRL:#x}]
        done = m[{RESETS_RESET_DONE:#x}]
        regs['RESETS_RESET_DONE'] = done
        if done & {RESET_DONE_PWM:#x}:
            regs['PWM_CH0_CSR'] = m[{PWM_CH0_CSR:#x}]
            regs['PWM_CH0_DIV'] = m[{PWM_CH0_DIV:#x}]
            regs['PWM_CH0_TOP'] = m[{PWM_CH0_TOP:#x}]
            regs['PWM_EN'] = m[{PWM_EN:#x}]
        r['regs'] = regs
    print(json.dumps(r))
_tthfv_probe()
del _tthfv_probe
"""


def decode_registers(regs: dict) -> dict:
    """Readable fields of the raw register values the probe returned (missing ones are left out)."""
    u = {k: v & 0xFFFFFFFF for k, v in regs.items() if isinstance(v, int)}
    out: dict = {}
    if "SYSINFO_CHIP_ID" in u:
        cid = u["SYSINFO_CHIP_ID"]
        out["chip_revision"] = cid >> 28
        out["chip_part"] = (cid >> 12) & 0xFFFF
        out["chip_manufacturer"] = (cid >> 1) & 0x7FF
    if "IO_BANK0_GPIO16_CTRL" in u:
        f = u["IO_BANK0_GPIO16_CTRL"] & 0x1F
        out["gpio16_funcsel"] = FUNCSEL.get(f, f"{f:#x}")
    if "IO_BANK0_GPIO16_STATUS" in u:
        st = u["IO_BANK0_GPIO16_STATUS"]
        out["gpio16_out_to_pad"] = bool(st & 0x200)
        out["gpio16_oe_to_pad"] = bool(st & 0x2000)
        out["gpio16_in_from_pad"] = bool(st & 0x20000)
    if "RESETS_RESET_DONE" in u:
        out["pwm_out_of_reset"] = bool(u["RESETS_RESET_DONE"] & RESET_DONE_PWM)
    if "PWM_EN" in u:
        out["pwm0_enabled"] = bool(u["PWM_EN"] & 1)
    if "PWM_CH0_CSR" in u:
        out["pwm0_csr_en"] = bool(u["PWM_CH0_CSR"] & 1)
    if "PWM_CH0_DIV" in u:
        d = u["PWM_CH0_DIV"]
        out["pwm0_div"] = ((d >> 4) & 0xFF) + (d & 0xF) / 16
    if "PWM_CH0_TOP" in u:
        out["pwm0_top"] = u["PWM_CH0_TOP"] & 0xFFFF
    return out
