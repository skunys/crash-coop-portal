#!/usr/bin/env python3
"""
P2 client (Player 2 client) -- run on HER machine.

Modified game copy that connects to YOUR PC (port 9099) as Player 2,
sends her keyboard + gamepad input, and falls back to the VPS relay
(cloudflared tunnel link) if LAN is unreachable.

Point HOST at YOUR PC's LAN IP (find it with `ipconfig` on your PC):
    ipconfig  ->  IPv4 Address (e.g. 192.168.1.20)
"""
from __future__ import annotations

import json
import socket
import sys
import time
import webbrowser

# <-- EDIT THIS to your PC's actual LAN IP (from ipconfig -> IPv4 Address)
HOST = "192.168.100.2"

PORT = 9099
RELAY_URL = "https://your-tunnel-url.example.com/"  # <- the ?k= tunnel link you send her

READY = b'{"t":"p2_ready"}\n'
READY_RESPONSE = b'{"t":"ready"}\n'


class P2Client:
    def __init__(self, host=HOST, port=PORT, relay=RELAY_URL):
        self.host = host
        self.port = port
        self.relay = relay
        self.sock = None
        self.connected = False

    def connect(self, timeout=5.0):
        """Try LAN first, then fall back to the relay."""
        if self._connect_lan():
            return True
        print("[P2] LAN unreachable, falling back to remote relay...")
        time.sleep(2)
        return self._connect_relay()

    def _connect_lan(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=2)
            self.sock.settimeout(2)
            self.sock.sendall(READY)
            self._read_ready()
            self.connected = True
            print("[P2] connected to host at", self.host, "as Player 2")
            return True
        except OSError as exc:
            print(f"[P2] LAN connect failed: {exc}")
            return False

    def _connect_relay(self):
        try:
            webbrowser.open(self.relay)
            print("[P2] opened relay link in browser")
            return True
        except Exception as exc:
            print(f"[P2] relay fallback failed: {exc}")
            return False

    def _read_ready(self):
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                print("[P2] server:", chunk.decode(errors="ignore").strip())
                if chunk.strip() == b'{"t":"ready"}':
                    self._on_ready()
        except socket.timeout:
            pass
        except OSError:
            self.connected = False

    def _on_ready(self):
        print("[P2] handshake OK -- ready to send input")

    def send_input(self, payload):
        if not self.connected:
            return
        try:
            self.sock.sendall(json.dumps(payload).encode() + b"\n")
        except OSError:
            self.connected = False
            print("[P2] input send failed, disconnecting")

    def close(self):
        try:
            self.sock.sendall(json.dumps({"t": "disconnect"}).encode() + b"\n")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def main() -> int:
    print("[P2] Player 2 client starting...")
    print("[P2] HOST =", HOST, "PORT =", PORT)
    if not P2Client().connect():
        return 1
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[P2] stopping")
    finally:
        P2Client().close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
