#!/usr/bin/env python3
"""Send keys to a running portal, exactly like the friend's browser does.

Handy for checking the pad without a browser:

    python send-key.py space          # tap A (jump / "any button")
    python send-key.py d 600          # hold D (walk right) for 0.6 s
    python send-key.py enter          # tap Start

It speaks the same tiny WebSocket protocol as public/index.html.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))["token"]
PORT = int(json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))["http_port"])
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def connect() -> socket.socket:
    conn = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        "GET /ws HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{PORT}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Cookie: portal_token={TOKEN}\r\n"
        "\r\n"
    )
    conn.sendall(req.encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = conn.recv(4096)
        if not chunk:
            raise SystemExit("handshake failed")
        head += chunk
    expect = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    if expect.encode() not in head:
        raise SystemExit("handshake rejected:\n" + head.decode(errors="replace"))
    return conn


def send(conn: socket.socket, msg: dict) -> None:
    data = json.dumps(msg).encode()
    mask = os.urandom(4)
    header = bytes([0x81])
    n = len(data)
    if n < 126:
        header += bytes([0x80 | n])
    elif n < 65536:
        header += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        header += bytes([0x80 | 127]) + struct.pack(">Q", n)
    conn.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    key = sys.argv[1]
    hold = float(sys.argv[2]) if len(sys.argv) > 2 else 0.15
    conn = connect()
    send(conn, {"t": "k", "c": key, "d": 1})
    time.sleep(hold)
    send(conn, {"t": "k", "c": key, "d": 0})
    time.sleep(0.3)
    conn.close()
    print(f"sent {key} down for {hold}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
