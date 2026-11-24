# SPDX-License-Identifier: Apache-2.0
"""TEMPORARY (24 Nov 2026): a pinned exception for an expired server certificate. Unset the variable once the
certificate of tinytapeout.fpgas.online is renewed; with a valid certificate it has no effect at all.

The status reads (`status.py`) and the serial WebSocket (`link.py`) both connect through this module, so there
is one rule. Full verification is the default and always tried first. Only when the environment variable
`TTHFV_BOARD_TLS_EXPIRED_PIN` holds a SHA-256 hex fingerprint (colons and capitals allowed) of the server's
certificate in DER form, AND the verification failure is specifically "certificate has expired", is the
connection made again with chain and hostname checking off, and then ONLY if the peer's certificate hashes to
exactly the pinned value; nothing is sent before that check (an HTTP request, the WebSocket upgrade). Every
other failure (wrong host, self-signed, unknown issuer) fails as before, whatever the pin.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import re
import socket
import ssl
import threading

from tthfv.board import BoardError

PIN_ENV = "TTHFV_BOARD_TLS_EXPIRED_PIN"
WARNING = (
    f"tthfv: WARNING: expired certificate accepted by pin ({PIN_ENV}, temporary exception of 24 Nov 2026): "
    f"{{what}}; unset {PIN_ENV} once the certificate is renewed"
)

_lock = threading.Lock()
_events: list[dict] = []


def configured_pin() -> str | None:
    """The pinned fingerprint (lower-case hex, 64 digits), or None when the variable is unset or empty."""
    value = os.environ.get(PIN_ENV, "").strip()
    if not value:
        return None
    pin = value.replace(":", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", pin):
        raise BoardError(
            f"{PIN_ENV} must be a SHA-256 fingerprint: 64 hex digits (colons allowed), got {value!r}"
        )
    return pin


def verified_context() -> ssl.SSLContext:
    """The ordinary, fully verifying context (tests replace it to trust a throwaway certificate)."""
    return ssl.create_default_context()


def pinned_context() -> ssl.SSLContext:
    """No chain and no hostname checking: ONLY for a connection whose peer certificate is then pinned."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def is_expired(e: BaseException) -> bool:
    return isinstance(e, ssl.SSLCertVerificationError) and (
        e.verify_code == 10 or "certificate has expired" in str(e)  # X509_V_ERR_CERT_HAS_EXPIRED
    )


def pin_if_expired(e: BaseException) -> str | None:
    """The pin to fall back on for this verification failure, or None (the caller re-raises `e`)."""
    return configured_pin() if is_expired(e) else None


def pinned_socket(host: str, port: int, timeout: float, pin: str) -> ssl.SSLSocket:
    """A TLS socket to `host` whose peer certificate has been checked against `pin`; closed and BoardError
    otherwise. The use is recorded."""
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        sock = pinned_context().wrap_socket(raw, server_hostname=host)
        der = sock.getpeercert(binary_form=True) or b""
        fingerprint = hashlib.sha256(der).hexdigest()
        if fingerprint != pin:
            raise BoardError(
                f"{host}: the certificate has expired and its SHA-256 fingerprint {fingerprint} is not the "
                f"pinned {pin} ({PIN_ENV}); nothing was sent"
            )
    except BaseException:
        raw.close()
        raise
    with _lock:
        _events.append({"host": host, "fingerprint": fingerprint})
    return sock


def connect_https(host: str, port: int | None, timeout: float) -> http.client.HTTPSConnection:
    """A connected HTTPS connection: verified, or (expired certificate, pin set) pinned."""
    conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=verified_context())
    try:
        conn.connect()
        return conn
    except ssl.SSLCertVerificationError as e:
        conn.close()
        pin = pin_if_expired(e)
        if pin is None:
            raise
    fallback = http.client.HTTPSConnection(host, port, timeout=timeout, context=pinned_context())
    fallback.sock = pinned_socket(host, port or 443, timeout, pin)
    return fallback


def uses() -> int:
    """How many times the exception has been used in this process."""
    with _lock:
        return len(_events)


def events_since(n: int) -> list[dict]:
    with _lock:
        return list(_events[n:])
