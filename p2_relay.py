#!/usr/bin/env python3
"""
Tiny WebSocket client for the Crash co-op relay (relay/server.js).

The relay is the fixed meeting point. Both sides dial OUT to it, so nothing has
to be port-forwarded on the gaming PC and Player 2 never needs a link that
changes every time the portal restarts.

The LAN path speaks newline-terminated JSON over TCP. This module gives the
relay path the same shape -- send(dict) and poll() -> dict -- so p2_host.py and
p2_client.py do not care which route won.

Standard library only.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
from urllib.parse import urlparse, urlencode

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x1, 0x2, 0x8, 0x9, 0xA


def relay_url(base: str, room: str = "crash", role: str = "client", key: str = "") -> str:
    """Turn wss://host (or a full /relay?.. URL) into this side's relay address.

    `key` is the shared room key: the relay refuses a connection without it, so a
    public URL is not enough to take a seat in the room.
    """
    if "://" not in base:
        base = "wss://" + base
    u = urlparse(base)
    scheme = u.scheme or "wss"
    netloc = u.netloc or base.split("://", 1)[-1]
    path = (u.path or "").rstrip("/")
    if not path.endswith("/relay"):
        path += "/relay"
    params: dict[str, str] = {}
    for pair in (u.query or "").split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            params[k] = v
    params["room"] = room
    params["role"] = role
    if key:
        params["key"] = key
    return f"{scheme}://{netloc}{path}?{urlencode(params)}"


class RelayLink:
    """One WebSocket connection to the relay. Recreate it to reconnect."""

    def __init__(self, url: str, timeout: float = 6.0):
        self.url = url
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self._buf = b""
        self.connected = False

    # -- connect -----------------------------------------------------------
    def connect(self) -> bool:
        u = urlparse(self.url)
        host = u.hostname or ""
        if not host:
            raise OSError(f"bad relay URL: {self.url!r}")
        secure = u.scheme in ("wss", "https")
        port = u.port or (443 if secure else 80)
        path = (u.path or "/") + (("?" + u.query) if u.query else "")

        raw = socket.create_connection((host, port), timeout=self.timeout)
        if secure:
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        raw.settimeout(self.timeout)

        key = base64.b64encode(os.urandom(16)).decode()
        host_hdr = host if port in (80, 443) else f"{host}:{port}"
        raw.sendall(
            (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host_hdr}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            ).encode()
        )

        head = b""
        while b"\r\n\r\n" not in head:
            chunk = raw.recv(4096)
            if not chunk:
                raw.close()
                raise OSError("relay closed the connection during the handshake")
            head += chunk
        header, _, rest = head.partition(b"\r\n\r\n")
        status = header.split(b"\r\n", 1)[0]
        if b" 101" not in status:
            raw.close()
            raise OSError(f"relay refused: {status.decode(errors='replace')}")
        expect = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest())
        if expect not in header:
            raw.close()
            raise OSError("relay sent a bad Sec-WebSocket-Accept")

        self.sock = raw
        self._buf = rest
        self.connected = True
        return True

    # -- framing -----------------------------------------------------------
    def _recv_exact(self, n: int) -> bytes:
        if not self.sock:
            raise ConnectionError("relay link is closed")
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise ConnectionError("relay closed the connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> bool:
        if not self.sock:
            return False
        mask = os.urandom(4)
        n = len(payload)
        header = bytes([0x80 | opcode])
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        try:
            self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
            return True
        except Exception:
            self.connected = False
            return False

    # -- public ------------------------------------------------------------
    def send(self, msg: dict) -> bool:
        return self._send_frame(OP_TEXT, json.dumps(msg).encode("utf-8"))

    def poll(self, timeout: float = 1.0) -> dict:
        """One message, {} when idle, OSError when the relay link is gone."""
        if not self.sock:
            raise ConnectionError("relay link is closed")
        self.sock.settimeout(timeout)
        try:
            head = self._recv_exact(2)
            opcode = head[0] & 0x0F
            masked = head[1] & 0x80
            length = head[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length) if length else b""
        except socket.timeout:
            return {}
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == OP_CLOSE:
            self.connected = False
            raise ConnectionError("relay closed the connection")
        if opcode == OP_PING:
            self._send_frame(OP_PONG, payload)
            return {}
        if opcode in (OP_PONG, OP_BIN):
            return {}
        if opcode != OP_TEXT:
            return {}
        try:
            msg = json.loads(payload.decode("utf-8"))
        except Exception:
            return {}
        return msg if isinstance(msg, dict) else {}

    def ping(self) -> None:
        """Application-level probe: the relay answers {t: 'relay_pong', ts}."""
        self.send({"t": "ping", "ts": int(__import__("time").time() * 1000)})

    def close(self) -> None:
        self.connected = False
        if not self.sock:
            return
        try:
            self._send_frame(OP_CLOSE, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.sock = None
