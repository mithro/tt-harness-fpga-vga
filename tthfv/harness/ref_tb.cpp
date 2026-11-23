// SPDX-License-Identifier: Apache-2.0
//
// Reference testbench: clock a Tiny Tapeout design from reset and dump its output pins.
//
// Usage: ref_tb --clocks N --ui-in V --uio-in V --out DIR
//
// Writes DIR/uo_out.bin (one byte per clock: uo_out) and DIR/uio.bin (two bytes per clock:
// uio_out, uio_oe). Clock i is the i-th rising edge from the start; the value recorded is the
// one after that rising edge. rst_n is low for clocks 0..9 and high from clock 10.
//
// Pad model: on the board uio[7:0] are bidirectional pads, so a design reads back what it drives.
// The testbench holds the fixed --uio-in on the pins the design does not drive:
//     uio_in = (uio_oe & uio_out) | (~uio_oe & fixed_uio_in)
// The value the design sees during clock i is derived from the uio_out and uio_oe it drove after
// the rising edge of clock i-1 (before clock 0, from its state after the initial evaluation). After
// each rising edge the pad is settled, as a combinational pad would be: if the derived value differs
// from the current uio_in it is applied and the model evaluated again, until it is stable, before the
// outputs are recorded.
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
static constexpr int EXIT_FINISHED = 3;  // the design called $finish or $fatal
static constexpr int MAX_SETTLE = 8;     // evaluations to settle the pad (more means a combinational loop)

// Apply the pad value derived from the design's own uio outputs; evaluate again if it changed.
static void settle_pad(Vtop& top, uint8_t fixed_uio_in) {
    for (int n = 0; n < MAX_SETTLE; n++) {
        const uint8_t oe = top.uio_oe;
        const uint8_t pad = static_cast<uint8_t>((oe & top.uio_out) | (~oe & fixed_uio_in));
        if (pad == top.uio_in) return;
        top.uio_in = pad;
        top.eval();
    }
}

static bool write_all(const std::string& path, const std::vector<uint8_t>& buf) {
    FILE* f = std::fopen(path.c_str(), "wb");
    if (!f) return false;
    bool ok = buf.empty() || std::fwrite(buf.data(), 1, buf.size(), f) == buf.size();
    return std::fclose(f) == 0 && ok;
}

int main(int argc, char** argv) {
    uint64_t clocks = 0;
    unsigned ui_in = 0, uio_in = 0;
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
        else if (a == "--out") out = v;
        else { std::fprintf(stderr, "ref_tb: unknown option %s\n", a.c_str()); return 2; }
    }
    if (!have_clocks || out.empty()) {
        std::fprintf(stderr, "usage: ref_tb --clocks N --ui-in V --uio-in V --out DIR\n");
        return 2;
    }

    Verilated::fatalOnError(false);  // $fatal sets gotFinish instead of abort()
    Vtop top;
    top.ena = 1;
    top.ui_in = ui_in;
    top.uio_in = uio_in;
    top.rst_n = 0;
    top.clk = 0;
    top.eval();
    settle_pad(top, static_cast<uint8_t>(uio_in));

    std::vector<uint8_t> uo, uio;
    uo.reserve(clocks);
    uio.reserve(clocks * 2);
    int64_t finished_at = -1;
    for (uint64_t i = 0; i < clocks; i++) {
        if (i == RESET_CLOCKS) top.rst_n = 1;
        top.clk = 0;
        top.eval();
        top.clk = 1;
        top.eval();
        settle_pad(top, static_cast<uint8_t>(uio_in));
        if (Verilated::gotFinish()) {
            finished_at = static_cast<int64_t>(i);
            break;
        }
        uo.push_back(top.uo_out);
        uio.push_back(top.uio_out);
        uio.push_back(top.uio_oe);
    }
    top.final();

    if (!write_all(out + "/uo_out.bin", uo) || !write_all(out + "/uio.bin", uio)) {
        std::fprintf(stderr, "ref_tb: cannot write to %s\n", out.c_str());
        return 1;
    }
    if (finished_at >= 0) {
        std::fprintf(stderr, "ref_tb: design finished at clock %lld\n", static_cast<long long>(finished_at));
        return EXIT_FINISHED;
    }
    return 0;
}
