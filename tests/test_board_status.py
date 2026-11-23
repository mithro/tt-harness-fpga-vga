# SPDX-License-Identifier: Apache-2.0
import time

import pytest
from board_fakes import FakeBoard

from tthfv.board import BoardError, status

SAMPLE = {
    "board": {
        "present": True,
        "device": "/dev/ttboard",
        "vid_pid": "2e8a:0005",
        "usb_serial": "a2961e5cac65b25f",
        "chip": "fpga",
    },
    "kind": "fpga",
    "clients": 0,
    "idle_display": {"design": "x.bin", "state": "loaded"},
    "safe_start": {"state": "not a chip board"},
    "uptime_s": 268456,
    "version": "0.1.0",
    "reachable": True,
}


def _status(slug="fpga-4", **changes):
    raw = {**SAMPLE, "board": dict(SAMPLE["board"]), "idle_display": dict(SAMPLE["idle_display"])}
    if "state" in changes:
        raw["idle_display"]["state"] = changes.pop("state")
    if "usb_serial" in changes:
        raw["board"]["usb_serial"] = changes.pop("usb_serial")
    if "present" in changes:
        raw["board"]["present"] = changes.pop("present")
    raw.update(changes)
    return status.parse(slug, raw)


def test_parse_reads_the_fields():
    s = _status()
    assert s.slug == "fpga-4"
    assert s.usb_serial == "a2961e5cac65b25f"
    assert s.clients == 0
    assert s.idle_state == "loaded"
    assert s.reachable is True
    assert s.present is True
    assert s.raw["uptime_s"] == 268456


def test_parse_missing_fields_are_not_usable():
    s = status.parse("fpga-4", {})
    assert s.clients is None and s.idle_state is None and s.usb_serial is None
    assert status.usable(s)[0] is False


def test_loaded_and_no_clients_is_usable():
    assert status.usable(_status()) == (True, "clients 0, idle display loaded")


@pytest.mark.parametrize(
    "state",
    [
        "in use",
        "waiting",
        "left: tt_um_nanosilicon",
        "left: the SDK is not running",
        "failed: timeout",
        "board not present",
    ],
)
def test_any_other_idle_state_is_not_usable(state):
    ok, why = status.usable(_status(state=state))
    assert not ok
    assert state in why


@pytest.mark.parametrize("clients", [1, 2, 5])
def test_any_client_is_not_usable(clients):
    ok, why = status.usable(_status(clients=clients))
    assert not ok
    assert f"clients {clients}" in why


def test_wrong_usb_serial_is_not_usable():
    ok, why = status.usable(_status(usb_serial="8c46329b33590ecb"))
    assert not ok
    assert "8c46329b33590ecb" in why


def test_unreachable_or_absent_is_not_usable():
    assert status.usable(_status(reachable=False))[0] is False
    assert status.usable(_status(present=False))[0] is False


def test_forbidden_board_is_never_usable():
    s = _status(slug="fpga-1", usb_serial="4df39a7a6856f86f")
    ok, why = status.usable(s)
    assert not ok
    assert "fpga-1" in why


def test_unknown_board_is_not_usable():
    ok, why = status.usable(_status(slug="fpga-9"))
    assert not ok
    assert "unknown" in why


def test_read_fetches_from_the_base_url_with_a_generic_user_agent(monkeypatch):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.0)
    with FakeBoard() as fake:
        fake.env(monkeypatch)
        s = status.read("fpga-4")
    assert s.usb_serial == "a2961e5cac65b25f"
    assert s.idle_state == "loaded"
    assert fake.status_user_agents == ["tthfv"]


def test_read_is_rate_limited_per_board(monkeypatch):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.3)
    with FakeBoard() as fake:
        fake.env(monkeypatch)
        t0 = time.monotonic()
        status.read("fpga-4")
        status.read("fpga-4")
        status.read("fpga-4")
        elapsed = time.monotonic() - t0
    assert elapsed >= 0.6


def test_read_error_is_a_board_error(monkeypatch):
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.0)
    with FakeBoard() as fake:
        fake.env(monkeypatch)
        with pytest.raises(BoardError, match="fpga-9"):
            status.read("fpga-9")  # the fake answers 404


def test_base_url_default(monkeypatch):
    monkeypatch.delenv("TTHFV_BOARD_BASE_URL", raising=False)
    monkeypatch.delenv("TTHFV_BOARD_WS_URL", raising=False)
    assert status.status_url("fpga-4") == "https://tinytapeout.fpgas.online/board/fpga-4/status.json"
    assert status.serial_url("fpga-4") == "wss://tinytapeout.fpgas.online/ws/board/fpga-4/serial"


def test_ws_url_follows_base_url(monkeypatch):
    monkeypatch.setenv("TTHFV_BOARD_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("TTHFV_BOARD_WS_URL", raising=False)
    assert status.serial_url("fpga-4") == "ws://127.0.0.1:9/ws/board/fpga-4/serial"
