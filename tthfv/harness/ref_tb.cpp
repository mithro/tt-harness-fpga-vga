// SPDX-License-Identifier: Apache-2.0
//
// Reference testbench: clock a Tiny Tapeout design from reset and dump its output pins.
//
// Usage: ref_tb --clocks N --ui-in V --uio-in V --out DIR
//               [--ui-drive MASK] [--ui-undriven V] [--ties MASK] [--seed S]
//
// Writes DIR/uo_out.bin (one byte per clock: uo_out), DIR/uo_out_low.bin (one byte per clock: uo_out in the
// low phase), DIR/uio.bin (two bytes per clock: uio_out, uio_oe), DIR/uio_oe_ever.bin (one byte: the OR of
// uio_oe over every sample from clock 10 on) and DIR/two_driver_clocks.bin (eight bytes, little endian: the
// number of clocks at which the MCU drove a tied ui_in bit while the design enabled the same uio bit).
// Clock i is the i-th rising edge from the start; the value recorded in uo_out.bin is the one after that
// rising edge. rst_n is low for clocks 0..9 and high from clock 10.
//
// Low phase of clock i: uo_out after the falling edge that follows the rising edge of clock i has been
// evaluated and the pads settled, before the rising edge of clock i+1. The falling edge is the evaluation with
// clk = 0 at the start of the next loop turn (which is also where rst_n goes high, for clock 9); the last
// clock gets one more evaluation after the loop.
//
// Pins. Per ui_in bit k: --ui-drive says whether the MCU drives it (default: all), --ui-in is the level it
// drives, --ui-undriven the level the pin sits at when nobody drives it (default: --ui-in). Per uio bit,
// --uio-in is the level the net sits at when nobody drives it. --ties MASK lists the bits k where ui_in[k]
// and uio[k] are one net; ui_in[k] and uio_in[k] both read it:
//     net = uio_oe[k] ? uio_out[k] : (MCU drives ui_in[k] ? its level : uio_in[k] undriven level)
// If the MCU drives a tied bit and the design enables that uio bit, two outputs are on one net. The design's
// value wins (a real fight is undefined) and the clock is counted in two_driver_clocks.bin.
// Untied: uio_in[k] = uio_oe[k] ? uio_out[k] : undriven level; ui_in[k] = driven ? level : undriven level.
// The value the design sees during clock i is derived from the uio_out and uio_oe it drove after the rising
// edge of clock i-1 (before clock 0, from its state after the initial evaluation). After each evaluation
// that can change them (the initial one, each rising edge, each falling edge) the pads are settled, as
// combinational pads would be: if the derived values differ from the current ui_in/uio_in they are applied
// and the model evaluated again, until they are stable. If they are still changing after MAX_SETTLE
// evaluations the net does not settle; that is counted and the first occurrence recorded. The run goes on to
// the end, the dumps are written, "ref_tb: the pad did not settle: ..." goes to stderr and the exit code is 4.
// If the design calls $finish or $fatal, the run stops, what was recorded so far is written, and
// "ref_tb: design finished at clock N" goes to stderr with exit code 3 (N is the clock being run).
// No sync detection, no frame logic, no memory model.
//
// Build: verilator --cc --exe --build --prefix Vtop --top-module <top> ref_tb.cpp <sources>

#include "Vtop.h"
#include "verilated.h"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

static constexpr int RESET_CLOCKS = 10;
static constexpr int EXIT_FINISHED = 3;   // the design called $finish or $fatal
static constexpr int EXIT_UNSETTLED = 4;  // a pad did not settle
static constexpr int MAX_SETTLE = 8;      // evaluations to settle the pads (more means a loop through the pad)

struct Pins {
    uint8_t ui_level = 0;    // what the MCU drives on the bits it drives
    uint8_t ui_drive = 0xff; // ui_in bits the MCU drives
    uint8_t ui_undriven = 0; // level of an undriven ui_in bit
    uint8_t uio_undriven = 0;
    uint8_t ties = 0;
};

// The values ui_in and uio_in take, given what the design drives on uio now.
static void pads_for(uint8_t oe, uint8_t out, const Pins& p, uint8_t& ui, uint8_t& uio) {
    const uint8_t idle_untied = p.uio_undriven;
    const uint8_t idle_tied = static_cast<uint8_t>((p.ui_drive & p.ui_level) | (~p.ui_drive & p.uio_undriven));
    const uint8_t net_untied = static_cast<uint8_t>((oe & out) | (~oe & idle_untied));
    const uint8_t net_tied = static_cast<uint8_t>((oe & out) | (~oe & idle_tied));
    uio = static_cast<uint8_t>((net_untied & ~p.ties) | (net_tied & p.ties));
    const uint8_t ui_untied = static_cast<uint8_t>((p.ui_drive & p.ui_level) | (~p.ui_drive & p.ui_undriven));
    ui = static_cast<uint8_t>((ui_untied & ~p.ties) | (net_tied & p.ties));
}

static void pads(const Vtop& top, const Pins& p, uint8_t& ui, uint8_t& uio) {
    pads_for(top.uio_oe, top.uio_out, p, ui, uio);
}

// Apply the derived pad values; evaluate again if they changed. False if they are still changing.
static bool settle(Vtop& top, const Pins& p, uint8_t& ui_diff, uint8_t& uio_diff) {
    uint8_t ui, uio;
    for (int n = 0; n < MAX_SETTLE; n++) {
        pads(top, p, ui, uio);
        if (ui == top.ui_in && uio == top.uio_in) return true;
        top.uio_in = uio;
        top.ui_in = ui;
        top.eval();
    }
    pads(top, p, ui, uio);
    if (ui == top.ui_in && uio == top.uio_in) return true;
    ui_diff = static_cast<uint8_t>(ui ^ top.ui_in);
    uio_diff = static_cast<uint8_t>(uio ^ top.uio_in);
    return false;
}

static bool write_all(const std::string& path, const std::vector<uint8_t>& buf) {
    FILE* f = std::fopen(path.c_str(), "wb");
    if (!f) return false;
    bool ok = buf.empty() || std::fwrite(buf.data(), 1, buf.size(), f) == buf.size();
    return std::fclose(f) == 0 && ok;
}

int main(int argc, char** argv) {
    uint64_t clocks = 0;
    unsigned ui_in = 0, uio_in = 0, ties = 0, ui_drive = 0xff;
    long ui_undriven = -1;  // -1: the same as --ui-in
    long seed = -1;         // -1: all-zero initial state
    std::string out;
    bool have_clocks = false;
    for (int i = 1; i < argc; i++) {
        const std::string a = argv[i];
        if (i + 1 >= argc) {
            std::fprintf(stderr, "ref_tb: %s needs a value\n", a.c_str());
            return 2;
        }
        const char* v = argv[++i];
        if (a == "--clocks") { clocks = std::strtoull(v, nullptr, 10); have_clocks = true; }
        else if (a == "--ui-in") ui_in = std::strtoul(v, nullptr, 0) & 0xff;
        else if (a == "--uio-in") uio_in = std::strtoul(v, nullptr, 0) & 0xff;
        else if (a == "--ui-drive") ui_drive = std::strtoul(v, nullptr, 0) & 0xff;
        else if (a == "--ui-undriven") ui_undriven = static_cast<long>(std::strtoul(v, nullptr, 0) & 0xff);
        else if (a == "--ties") ties = std::strtoul(v, nullptr, 0) & 0xff;
        else if (a == "--seed") seed = std::strtol(v, nullptr, 10);
        else if (a == "--out") out = v;
        else { std::fprintf(stderr, "ref_tb: unknown option %s\n", a.c_str()); return 2; }
    }
    if (!have_clocks || out.empty()) {
        std::fprintf(stderr,
                     "usage: ref_tb --clocks N --ui-in V --uio-in V --out DIR [--ui-drive MASK] "
                     "[--ui-undriven V] [--ties MASK] [--seed S]\n");
        return 2;
    }
    Pins pins;
    pins.ui_level = static_cast<uint8_t>(ui_in);
    pins.ui_drive = static_cast<uint8_t>(ui_drive);
    pins.ui_undriven = static_cast<uint8_t>(ui_undriven < 0 ? static_cast<long>(ui_in) : ui_undriven);
    pins.uio_undriven = static_cast<uint8_t>(uio_in);
    pins.ties = static_cast<uint8_t>(ties);

    Verilated::fatalOnError(false);  // $fatal sets gotFinish instead of abort()
    if (seed >= 0) {
        Verilated::randReset(2);  // random initial values, from the seed
        Verilated::randSeed(static_cast<int>(seed));
    }
    Vtop top;
    top.ena = 1;
    {  // before anything is evaluated the pins sit at their idle levels: no uio output is enabled yet
        uint8_t ui0, uio0;
        pads_for(0, 0, pins, ui0, uio0);
        top.ui_in = ui0;
        top.uio_in = uio0;
    }
    top.rst_n = 0;
    top.clk = 0;
    top.eval();

    uint64_t unsettled = 0, two_driver = 0;
    int64_t first_unsettled = 0;  // clock of the first occurrence (-1: before clock 0); valid if unsettled > 0
    uint8_t first_ui_diff = 0, first_uio_diff = 0;
    auto settled = [&](int64_t clock) {
        uint8_t ud = 0, od = 0;
        if (settle(top, pins, ud, od)) return;
        if (unsettled++ == 0) {
            first_unsettled = clock;
            first_ui_diff = ud;
            first_uio_diff = od;
        }
    };
    settled(-1);

    std::vector<uint8_t> uo, uo_low, uio;
    uint8_t oe_ever = 0;  // OR of uio_oe over every sample from clock RESET_CLOCKS on
    uo.reserve(clocks);
    uo_low.reserve(clocks);
    uio.reserve(clocks * 2);
    int64_t finished_at = -1;
    for (uint64_t i = 0; i < clocks; i++) {
        if (i == RESET_CLOCKS) top.rst_n = 1;
        top.clk = 0;
        top.eval();
        if (i > 0) {  // the low phase of clock i-1
            settled(static_cast<int64_t>(i) - 1);
            uo_low.push_back(top.uo_out);
            if (i - 1 >= RESET_CLOCKS) oe_ever |= top.uio_oe;
        }
        top.clk = 1;
        top.eval();
        settled(static_cast<int64_t>(i));
        if (Verilated::gotFinish()) {
            finished_at = static_cast<int64_t>(i);
            break;
        }
        uo.push_back(top.uo_out);
        uio.push_back(top.uio_out);
        uio.push_back(top.uio_oe);
        if (i >= RESET_CLOCKS) oe_ever |= top.uio_oe;
        if (top.uio_oe & pins.ui_drive & pins.ties) two_driver++;
    }
    if (finished_at < 0 && clocks > 0) {  // the low phase of the last clock
        top.clk = 0;
        top.eval();
        settled(static_cast<int64_t>(clocks) - 1);
        uo_low.push_back(top.uo_out);
        if (clocks - 1 >= RESET_CLOCKS) oe_ever |= top.uio_oe;
    }
    top.final();

    std::vector<uint8_t> two(8);
    for (int b = 0; b < 8; b++) two[b] = static_cast<uint8_t>(two_driver >> (8 * b));
    if (!write_all(out + "/uo_out.bin", uo) || !write_all(out + "/uo_out_low.bin", uo_low) ||
        !write_all(out + "/uio.bin", uio) || !write_all(out + "/uio_oe_ever.bin", {oe_ever}) ||
        !write_all(out + "/two_driver_clocks.bin", two)) {
        std::fprintf(stderr, "ref_tb: cannot write to %s\n", out.c_str());
        return 1;
    }
    if (finished_at >= 0) {
        std::fprintf(stderr, "ref_tb: design finished at clock %lld\n", static_cast<long long>(finished_at));
        return EXIT_FINISHED;
    }
    if (unsettled > 0) {
        std::fprintf(stderr,
                     "ref_tb: the pad did not settle: first at clock %lld (-1 is before clock 0), %llu times; "
                     "ui_in bits 0x%02x, uio bits 0x%02x were still changing\n",
                     static_cast<long long>(first_unsettled), static_cast<unsigned long long>(unsettled),
                     first_ui_diff, first_uio_diff);
        return EXIT_UNSETTLED;
    }
    return 0;
}
