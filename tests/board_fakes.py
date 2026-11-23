# SPDX-License-Identifier: Apache-2.0
"""An in-process stand-in for one board behind the fpgas.online daemon: a status endpoint (http.server) and a
serial WebSocket (websockets' sync server) in front of a byte-level MicroPython REPL emulation. No board."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

RAW_BANNER = b"\r\nraw REPL; CTRL-B to exit\r\n>"
FRIENDLY_BANNER = b'\r\nMicroPython fake on 2026-11-23; fake board with RP2350\r\nType "help()" for more information.\r\n>>> '
TRACEBACK = (
    b'Traceback (most recent call last):\r\n  File "<stdin>", line 1, in <module>\r\nValueError: boom\r\n'
)


def default_run(source: str) -> tuple[bytes, bytes]:
    """What the fake board 'runs': a script containing `raise` fails with TRACEBACK, anything else prints."""
    if "raise" in source:
        return b"", TRACEBACK
    return b"ran\r\n", b""


class FakeRepl:
    """MicroPython's friendly and raw REPL, byte by byte, as far as the link needs it."""

    def __init__(self, run: Callable[[str], tuple[bytes, bytes]] = default_run, silent: bool = False):
        self.run = run
        self.silent = silent
        self.mode = "friendly"
        self.sdk = True  # the SDK's `tt` exists (main.py ran)
        self.buf = bytearray()
        self.received = bytearray()
        self.resets = 0  # soft resets from the friendly REPL (main.py runs)
        self.raw_resets = 0  # soft resets from the raw REPL (main.py does not run)
        self.scripts: list[str] = []
        self.lock = threading.Lock()

    def feed(self, data: bytes) -> bytes:
        with self.lock:
            self.received += data
            if self.silent:
                return b""
            out = bytearray()
            for b in data:
                out += self._byte(b)
            return bytes(out)

    def _byte(self, b: int) -> bytes:
        if self.mode == "friendly":
            if b == 0x01:
                self.mode = "raw"
                self.buf.clear()
                return RAW_BANNER
            if b == 0x03:
                self.buf.clear()
                return b"\r\n>>> "
            if b == 0x04:
                self.resets += 1
                self.sdk = True
                return b"MPY: soft reboot\r\nBOOT: fake SDK started\r\n>>> "
            if b == 0x02:
                return FRIENDLY_BANNER
            if b == 0x0D:
                self.buf.clear()
                return b"\r\n>>> "
            self.buf.append(b)
            return bytes([b])
        # raw
        if b == 0x02:
            self.mode = "friendly"
            self.buf.clear()
            return FRIENDLY_BANNER
        if b == 0x03:
            self.buf.clear()
            return b""
        if b == 0x01:
            self.buf.clear()
            return RAW_BANNER
        if b == 0x04:
            if not self.buf:
                self.raw_resets += 1
                self.sdk = False
                return b"OK\r\nMPY: soft reboot\r\nraw REPL; CTRL-B to exit\r\n>"
            source = self.buf.decode("utf-8", "replace")
            self.buf.clear()
            self.scripts.append(source)
            stdout, stderr = self.run(source)
            return b"OK" + stdout + b"\x04" + stderr + b"\x04>"
        self.buf.append(b)
        return b""


class FakeBoard:
    """One board: status endpoint plus serial WebSocket. Use as a context manager; it sets the env vars.

    `lag_s` makes the status report `clients` as it was that long ago (the real site lags 5-10 s).
    `status_garbage` ("badstatus" or "incomplete") makes the status endpoint answer with a malformed HTTP
    response. `delay_before_reply` maps a byte value to seconds the board waits before answering a frame
    containing it. `drop_all()` closes every serial connection from the server side."""

    def __init__(
        self,
        slug: str = "fpga-4",
        usb_serial: str = "a2961e5cac65b25f",
        repl: FakeRepl | None = None,
        attach_delay: float = 0.0,
        chunk: int | None = None,
        spew: bool = False,
        quiet_reads: int = 2,
        lag_s: float = 0.0,
    ):
        self.slug = slug
        self.usb_serial = usb_serial
        self.repl = repl or FakeRepl()
        self.attach_delay = attach_delay
        self.chunk = chunk
        self.spew = spew
        self.quiet_reads = quiet_reads  # status reads after the last client leaves before "loaded"
        self._since_quiet = quiet_reads
        self.lag_s = lag_s
        self._ws_clients = 0
        self._visitors = 0
        self._history: list[tuple[float, int]] = [(0.0, 0)]
        self.state_override: str | None = None
        self.status_garbage: str | None = None
        self.delay_before_reply: dict[int, float] = {}
        self.status_reads = 0
        self.status_user_agents: list[str | None] = []
        self.ws_user_agents: list[str | None] = []
        self.ws_connections = 0
        self._conns: set = set()
        self.lock = threading.RLock()
        self.hook: Callable[[FakeBoard], None] | None = (
            None  # called (under the lock) before each status read
        )

    # -- clients, with history for the lag --
    def _record(self) -> None:
        self._history.append((time.monotonic(), self._ws_clients + self._visitors))

    @property
    def ws_clients(self) -> int:
        return self._ws_clients

    @property
    def visitors(self) -> int:
        return self._visitors

    @visitors.setter
    def visitors(self, n: int) -> None:
        with self.lock:
            self._visitors = n
            self._record()

    def _clients_seen(self) -> int:
        if not self.lag_s:
            return self._ws_clients + self._visitors
        when = time.monotonic() - self.lag_s
        seen = 0
        for t, n in self._history:
            if t <= when:
                seen = n
        return seen

    # -- status --
    def status(self) -> dict:
        with self.lock:
            self.status_reads += 1
            if self.hook is not None:
                self.hook(self)
            clients = self._clients_seen()
            if self.state_override is not None:
                state = self.state_override
            elif clients:
                state = "in use"
                self._since_quiet = 0
            elif not self.repl.sdk:
                state = "left: the SDK is not running"
            elif self._since_quiet < self.quiet_reads:
                self._since_quiet += 1
                state = "waiting"
            else:
                state = "loaded"
        return {
            "board": {
                "present": True,
                "device": "/dev/ttboard",
                "vid_pid": "2e8a:0005",
                "usb_serial": self.usb_serial,
                "chip": "fpga",
            },
            "kind": "fpga",
            "clients": clients,
            "idle_display": {"design": "idle.bin", "state": state},
            "safe_start": {"state": "not a chip board"},
            "uptime_s": 1,
            "version": "0.1.0",
            "reachable": True,
        }

    # -- serial --
    def _send(self, conn, data: bytes) -> None:
        if not data:
            return
        step = self.chunk or len(data)
        for i in range(0, len(data), step):
            conn.send(data[i : i + step])

    def _handler(self, conn) -> None:
        if conn.request.path != f"/ws/board/{self.slug}/serial":
            conn.close(code=1008)
            return
        self.ws_user_agents.append(conn.request.headers.get("User-Agent"))
        with self.lock:
            self._ws_clients += 1
            self.ws_connections += 1
            self._conns.add(conn)
            self._record()
        stop = threading.Event()
        try:
            if self.attach_delay:
                time.sleep(self.attach_delay)
            conn.send(json.dumps({"event": "board", "present": True, "device": "/dev/ttboard"}))
            if self.spew:
                threading.Thread(target=self._spew, args=(conn, stop), daemon=True).start()
            for msg in conn:
                data = msg if isinstance(msg, bytes) else msg.encode()
                delay = max((self.delay_before_reply.get(b, 0.0) for b in data), default=0.0)
                if delay:
                    time.sleep(delay)
                self._send(conn, self.repl.feed(data))
        except ConnectionClosed:  # the client went away mid-send
            return
        finally:
            stop.set()
            with self.lock:
                self._ws_clients -= 1
                self._conns.discard(conn)
                self._record()

    def drop_all(self) -> None:
        with self.lock:
            conns = list(self._conns)
        for c in conns:
            c.close(code=1011, reason="board disconnected")

    @staticmethod
    def _spew(conn, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                conn.send(b"spam spam spam\r\n")
            except Exception:  # noqa: BLE001
                return
            time.sleep(0.01)

    # -- lifecycle --
    def __enter__(self) -> Self:
        board = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != f"/board/{board.slug}/status.json":
                    self.send_error(404)
                    return
                board.status_user_agents.append(self.headers.get("User-Agent"))
                if board.status_garbage == "badstatus":
                    self.wfile.write(b"this is not HTTP\r\n\r\n")
                    self.close_connection = True
                    return
                body = json.dumps(board.status()).encode()
                if board.status_garbage == "incomplete":
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n" + body[:10])
                    self.close_connection = True
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._http.serve_forever, daemon=True).start()
        self._ws = serve(self._handler, "127.0.0.1", 0)
        threading.Thread(target=self._ws.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self._http.server_address[1]}"
        self.ws_url = f"ws://127.0.0.1:{self._ws.socket.getsockname()[1]}"
        return self

    def __exit__(self, *exc) -> None:
        self._ws.shutdown()
        self._http.shutdown()
        self._http.server_close()

    def env(self, monkeypatch) -> None:
        monkeypatch.setenv("TTHFV_BOARD_BASE_URL", self.base_url)
        monkeypatch.setenv("TTHFV_BOARD_WS_URL", self.ws_url)
