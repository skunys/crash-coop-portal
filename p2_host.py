#!/usr/bin/env python3
"""
P2 host (Player 2 host) -- run on YOUR PC.

Listens on 127.0.0.1:9099 for her modified game client (p2_client.py).
On a successful handshake it prints "[P2_HOST] Player 2 connected
from <ip>" and stays open so her client's input can be sent to the game.

This file is intentionally dependency-free (only the Python standard library).
It is meant to be started by play-coop.cmd, NOT as a standalone service.
"""
from __future__ import annotations

import socket
import threading
import sys
import time

HOST = "0.0.0.0"   # <-- 127.0.0.1 only reaches this PC; 0.0.0.0 reaches the whole LAN
PORT = 9099
READY = b'{"t":"p2_ready"}\n'
READY_RESPONSE = b'{"t":"ready"}\n'

_stop = threading.Event()


def parse_flags(argv: list[str]) -> None:
    global _stop
    for a in argv:
        if a in ("--stop", "-q"):
            _stop.set()


def handle(conn: socket.socket, addr: tuple[str, int]) -> None:
    try:
        conn.settimeout(10)
        data = b""
        while b"\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        if data.strip() != READY.strip():
            print(f"[P2_HOST] INVALID handshake from {addr}: {data!r}", flush=True)
            conn.close()
            return
        conn.sendall(READY_RESPONSE)
        print(f"[P2_HOST] Player 2 connected from {addr[0]}:{addr[1]}", flush=True)
        try:
            while not _stop.is_set():
                chunk = conn.recv(4096)
                if not chunk:
                    break
                try:
                    import json as _json
                    msg = _json.loads(chunk.decode("utf-8"))
                    t = msg.get("t")
                    if t == "pad_input":
                        print(f"[P2_HOST] input: {msg.get('data')}", flush=True)
                    elif t == "disconnect":
                        print(f"[P2_HOST] {addr[0]} sent disconnect", flush=True)
                        break
                except Exception:
                    pass
        finally:
            print(f"[P2_HOST] Player 2 {addr[0]} disconnected", flush=True)
            conn.close()
    except Exception as exc:
        print(f"[P2_HOST] error with {addr}: {exc}", flush=True)


def serve_forever() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((HOST, PORT))
    except OSError as exc:
        print(f"[P2_HOST] cannot bind {HOST}:{PORT}: {exc}", flush=True)
        print("[P2_HOST] is another P2 host already running?", flush=True)
        sys.exit(1)
    srv.listen(4)
    srv.settimeout(1.0)
    print(f"[P2_HOST] listening on {HOST}:{PORT}", flush=True)
    while not _stop.is_set():
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        except Exception:
            break
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    parse_flags(sys.argv[1:])
    try:
        serve_forever()
    finally:
        _stop.set()
