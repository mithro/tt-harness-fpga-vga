# SPDX-License-Identifier: Apache-2.0
"""The temporary, pinned exception for an expired certificate (24 Nov 2026): one place (`tls.py`) serves the
status reads and the serial WebSocket. Local TLS servers with throwaway certificates; no network."""

import hashlib
import shutil
import ssl
import subprocess

import pytest
from board_fakes import FakeBoard

from tthfv.board import BoardError, session, status, tls
from tthfv.board.link import Link

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl command")

PAST = ["-not_before", "20200101000000Z", "-not_after", "20200102000000Z"]


def _make_cert(directory, name, host="localhost", extra=()):
    cert, key = directory / f"{name}.pem", directory / f"{name}.key"
    cmd = ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]
    cmd += ["-keyout", str(key), "-out", str(cert), "-subj", f"/CN={host}", *extra]
    cmd += ["-addext", f"subjectAltName=DNS:{host}"]
    subprocess.run(cmd, check=True, capture_output=True)
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    return str(cert), str(key), hashlib.sha256(der).hexdigest()


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    d = tmp_path_factory.mktemp("tls")
    return {
        "expired": _make_cert(d, "expired", extra=PAST),
        "valid": _make_cert(d, "valid"),
        "other_host": _make_cert(d, "other_host", host="other.example"),
        "untrusted": _make_cert(d, "untrusted"),
        "expired_other_host": _make_cert(d, "expired_other_host", host="other.example", extra=PAST),
    }


@pytest.fixture
def trust(monkeypatch, certs):
    """Trust the throwaway certificates (except `untrusted`) the way the system store trusts a real one."""
    cafiles = [certs[k][0] for k in ("expired", "valid", "other_host", "expired_other_host")]

    def context():
        ctx = ssl.create_default_context()
        for f in cafiles:
            ctx.load_verify_locations(cafile=f)
        return ctx

    monkeypatch.setattr(tls, "verified_context", context)
    monkeypatch.setattr(status, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(session, "CONFIRM_AFTER_S", 0.0)
    monkeypatch.delenv(tls.PIN_ENV, raising=False)
    before = tls.uses()
    yield
    assert tls.uses() >= before


def _read(fake, monkeypatch):
    fake.env(monkeypatch)
    return status.read("fpga-4")


def test_without_the_variable_an_expired_certificate_fails_as_before(trust, certs, monkeypatch):
    with FakeBoard(tls=certs["expired"][:2]) as fake:
        with pytest.raises(BoardError, match="certificate has expired"):
            _read(fake, monkeypatch)
        assert fake.status_reads == 0


def test_with_the_right_pin_an_expired_certificate_is_accepted_and_counted(trust, certs, monkeypatch):
    monkeypatch.setenv(tls.PIN_ENV, certs["expired"][2])
    n = tls.uses()
    with FakeBoard(tls=certs["expired"][:2]) as fake:
        assert _read(fake, monkeypatch).clients == 0
    assert tls.uses() == n + 1


def test_the_pin_may_be_written_with_colons_and_capitals(trust, certs, monkeypatch):
    fp = certs["expired"][2].upper()
    monkeypatch.setenv(tls.PIN_ENV, ":".join(fp[i : i + 2] for i in range(0, len(fp), 2)))
    with FakeBoard(tls=certs["expired"][:2]) as fake:
        assert _read(fake, monkeypatch).clients == 0


def test_a_wrong_pin_fails_loudly_naming_both_and_sends_nothing(trust, certs, monkeypatch):
    wrong = certs["valid"][2]
    monkeypatch.setenv(tls.PIN_ENV, wrong)
    with FakeBoard(tls=certs["expired"][:2]) as fake:
        with pytest.raises(BoardError) as e:
            _read(fake, monkeypatch)
        assert fake.status_reads == 0
    assert certs["expired"][2] in str(e.value) and wrong in str(e.value)


def test_a_valid_certificate_takes_the_normal_path_and_the_variable_has_no_effect(trust, certs, monkeypatch):
    monkeypatch.setenv(
        tls.PIN_ENV, certs["expired"][2]
    )  # a pin that would not match: it must not be consulted
    n = tls.uses()
    with FakeBoard(tls=certs["valid"][:2]) as fake:
        assert _read(fake, monkeypatch).clients == 0
    assert tls.uses() == n


@pytest.mark.parametrize(
    ("served", "message"),
    [
        ("untrusted", "self.signed|unable to get local issuer|verify failed"),
        ("other_host", "hostname|doesn't match|verify failed"),
        ("expired_other_host", "hostname|doesn't match|verify failed"),
    ],
)
def test_any_other_verification_failure_still_fails_even_with_the_pin(
    trust, certs, monkeypatch, served, message
):
    monkeypatch.setenv(tls.PIN_ENV, certs[served][2])  # even the pin of the very certificate served
    with FakeBoard(tls=certs[served][:2]) as fake:
        with pytest.raises(BoardError, match=message):
            _read(fake, monkeypatch)
        assert fake.status_reads == 0


@pytest.mark.parametrize("bad", ["abc", "z" * 64, "89b5" * 15, "89b5" * 17])
def test_a_malformed_pin_is_refused(trust, certs, monkeypatch, bad):
    monkeypatch.setenv(tls.PIN_ENV, bad)
    with FakeBoard(tls=certs["expired"][:2]) as fake, pytest.raises(BoardError, match=tls.PIN_ENV):
        _read(fake, monkeypatch)


def test_the_websocket_link_uses_the_same_rule(trust, certs, monkeypatch):
    with FakeBoard(tls=certs["expired"][:2]) as fake:
        fake.env(monkeypatch)
        with pytest.raises(BoardError, match="certificate has expired"):
            Link.open("fpga-4")
        monkeypatch.setenv(tls.PIN_ENV, certs["valid"][2])
        with pytest.raises(BoardError) as e:
            Link.open("fpga-4")
        assert certs["expired"][2] in str(e.value)
        assert fake.ws_connections == 0  # the upgrade request was never sent
        monkeypatch.setenv(tls.PIN_ENV, certs["expired"][2])
        n = tls.uses()
        with Link.open("fpga-4") as link:
            assert link.notices[0]["event"] == "board"
        assert tls.uses() == n + 1


def test_a_session_records_the_exception_in_its_report_outcome_and_stderr(
    trust, certs, monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("TTHFV_BOARD_CLAIMS", str(tmp_path / "claims"))
    monkeypatch.setenv(tls.PIN_ENV, certs["expired"][2])
    report = session.SessionReport()
    with FakeBoard(tls=certs["expired"][:2]) as fake:
        fake.env(monkeypatch)
        with session.board_session(
            "fpga-4", "tls test", poll_s=0.05, restore_timeout_s=3.0, report=report
        ) as link:
            link.friendly_prompt()
    assert "expired certificate accepted by pin" in report.outcome
    assert certs["expired"][2] in report.tls_exception
    err = capsys.readouterr().err
    assert err.count("expired certificate accepted by pin") == 1 and "TTHFV_BOARD_TLS_EXPIRED_PIN" in err


def test_a_session_with_a_valid_certificate_records_nothing(trust, certs, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TTHFV_BOARD_CLAIMS", str(tmp_path / "claims"))
    monkeypatch.setenv(tls.PIN_ENV, certs["expired"][2])
    report = session.SessionReport()
    with FakeBoard(tls=certs["valid"][:2]) as fake:
        fake.env(monkeypatch)
        with session.board_session(
            "fpga-4", "tls test", poll_s=0.05, restore_timeout_s=3.0, report=report
        ) as link:
            link.friendly_prompt()
    assert report.tls_exception == "" and "expired certificate" not in report.outcome
    assert "expired certificate" not in capsys.readouterr().err


def test_a_refused_session_leaves_no_claim(trust, certs, monkeypatch, tmp_path):
    claims = tmp_path / "claims"
    monkeypatch.setenv("TTHFV_BOARD_CLAIMS", str(claims))
    with FakeBoard(tls=certs["expired"][:2]) as fake:
        fake.env(monkeypatch)
        with pytest.raises(BoardError, match="certificate has expired"), session.board_session("fpga-4", "x"):
            pass
    assert not [p for p in claims.glob("*.json")]
