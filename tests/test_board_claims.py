# SPDX-License-Identifier: Apache-2.0
import datetime as dt
import json
import os
import time

import pytest

from tthfv.board import ClaimHeld, claims

SERIAL = "a2961e5cac65b25f"


@pytest.fixture
def cdir(tmp_path, monkeypatch):
    d = tmp_path / "board-claims"
    monkeypatch.setenv("TTHFV_BOARD_CLAIMS", str(d))
    return d


def _iso(delta_s: float) -> str:
    return (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=delta_s)).isoformat(timespec="seconds")


def test_take_writes_the_claim_file(cdir):
    c = claims.take("fpga-4", SERIAL, "probe", 10)
    path = cdir / f"fpga-4-{SERIAL}.json"
    assert c.path == path
    data = json.loads(path.read_text())
    assert set(data) == {"board", "usb_serial", "owner", "purpose", "created", "expires"}
    assert data["board"] == "fpga-4"
    assert data["usb_serial"] == SERIAL
    assert data["owner"] == "tt-harness-fpga-vga"
    assert data["purpose"] == "probe"
    created = dt.datetime.fromisoformat(data["created"])
    expires = dt.datetime.fromisoformat(data["expires"])
    assert created.tzinfo is not None
    assert 599 <= (expires - created).total_seconds() <= 601


def test_taken_twice_the_second_fails(cdir):
    claims.take("fpga-4", SERIAL, "first", 10)
    with pytest.raises(ClaimHeld, match="first"):
        claims.take("fpga-4", SERIAL, "second", 10)


def test_release_then_take_again(cdir):
    c = claims.take("fpga-4", SERIAL, "first", 10)
    claims.release(c)
    assert not c.path.exists()
    assert claims.current("fpga-4") is None
    claims.take("fpga-4", SERIAL, "second", 10)


def test_release_twice_is_harmless(cdir):
    c = claims.take("fpga-4", SERIAL, "first", 10)
    claims.release(c)
    claims.release(c)


def test_release_leaves_a_claim_that_replaced_ours(cdir):
    c = claims.take("fpga-4", SERIAL, "first", 10)
    other = {**json.loads(c.path.read_text()), "purpose": "someone else", "created": _iso(1)}
    c.path.write_text(json.dumps(other))
    claims.release(c)
    assert c.path.exists()


def test_expired_claim_is_none_and_is_replaced(cdir):
    cdir.mkdir()
    path = cdir / f"fpga-4-{SERIAL}.json"
    path.write_text(
        json.dumps(
            {
                "board": "fpga-4",
                "usb_serial": SERIAL,
                "owner": "tt-harness-fpga-vga",
                "purpose": "old",
                "created": _iso(-3600),
                "expires": _iso(-60),
            }
        )
    )
    assert claims.current("fpga-4") is None
    c = claims.take("fpga-4", SERIAL, "new", 5)
    assert json.loads(path.read_text())["purpose"] == "new"
    assert c.purpose == "new"


def test_another_sessions_file_for_the_board_is_a_claim(cdir):
    cdir.mkdir()
    (cdir / "fpga-4-p35.json").write_text(json.dumps({"owner": "other", "expires": _iso(600)}))
    held = claims.current("fpga-4")
    assert held is not None and held.owner == "other"
    with pytest.raises(ClaimHeld, match="fpga-4-p35.json"):
        claims.take("fpga-4", SERIAL, "probe", 10)
    assert not (cdir / f"fpga-4-{SERIAL}.json").exists()


def test_another_sessions_expired_file_is_ignored(cdir):
    cdir.mkdir()
    (cdir / "fpga-4-p35.json").write_text(json.dumps({"owner": "other", "expires": _iso(-1)}))
    assert claims.current("fpga-4") is None
    claims.take("fpga-4", SERIAL, "probe", 10)


def test_file_without_expiry_is_live_while_young(cdir):
    cdir.mkdir()
    f = cdir / "fpga-4-p35.json"
    f.write_text("not json at all")
    assert claims.current("fpga-4") is not None
    old = time.time() - 31 * 60
    os.utime(f, (old, old))
    assert claims.current("fpga-4") is None


def test_other_boards_claims_do_not_count(cdir):
    cdir.mkdir()
    (cdir / "fpga-3-8c46329b33590ecb.json").write_text(json.dumps({"expires": _iso(600)}))
    (cdir / "fpga-40-x.json").write_text(json.dumps({"expires": _iso(600)}))
    (cdir / "notes.txt").write_text("x")
    assert claims.current("fpga-4") is None


def test_minutes_are_capped_at_30(cdir):
    with pytest.raises(ValueError):
        claims.take("fpga-4", SERIAL, "probe", 31)
    with pytest.raises(ValueError):
        claims.take("fpga-4", SERIAL, "probe", 0)


def test_default_directory_is_under_the_work_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("TTHFV_BOARD_CLAIMS", raising=False)
    monkeypatch.setenv("TTHFV_WORK", str(tmp_path / "w"))
    assert claims.claims_dir() == tmp_path / "w" / "board-claims"
