#!/usr/bin/env python3
"""
P2 client (Player 2 client) -- run on HER machine.

Modified game copy that connects as Player 2, sends her real keyboard + gamepad
input (p2_input.py), and keeps working wherever she is:

  LAN first   your PC on the same network, port 9099
  relay next  the Node relay (relay/server.js) you deploy, reached over the
              internet. The relay holds the room and your seat, so the address
              she was given never changes.

Both sides send the shared room key (config.json -> relay.key, or P2_KEY), so
knowing the relay URL is not enough to take a seat in the room.

Overrides (all optional):

    set P2_HOST=192.168.100.2
    set P2_RELAY=wss://<your-relay-host>
    set P2_ROOM=crash
    set P2_KEY=<the shared room key>
    set P2_FORCE_RELAY=1     # skip the LAN attempt (useful for testing)
    set P2_INPUT=0           # link only, no keyboard/pad capture

Dependency-free: only the Python standard library.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time

from p2_input import run as run_input
from p2_relay import RelayLink, relay_url

HERE = os.path.dirname(os.path.abspath(__file__))


def _relay_config() -> dict:
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as fh:
            return json.load(fh).get("relay") or {}
    except Exception:
        return {}


_CFG = _relay_config()

HOST = os.environ.get("P2_HOST", "192.168.100.2")
PORT = int(os.environ.get("P2_PORT", "9099"))
RELAY = os.environ.get("P2_RELAY") or _CFG.get("base") or "wss://dryadvsskunycrash.com"
ROOM = os.environ.get("P2_ROOM") or _CFG.get("room") or "crash"
KEY = os.environ.get("P2_KEY") or _CFG.get("key") or ""
FORCE_RELAY = os.environ.get("P2_FORCE_RELAY", "").lower() not in ("", "0", "false", "no")
WANT_INPUT = os.environ.get("P2_INPUT", "1").lower() not in ("0", "false", "no")

READY = b'{"t":"p2_ready"}\n'


class P2Client:
    def __init__(
        self,
        host: str = HOST,
        port: int = PORT,
        relay: str = RELAY,
        room: str = ROOM,
        key: str = KEY,
    ):
        self.host = host
        self.port = port
        self.relay = relay
        self.room = room
        self.key = key
        self.sock: socket.socket | None = None
        self.link: RelayLink | None = None
        self.route = ""
        self.connected = False
        self._shown_rtt: int | None = None
        self._peer: bool | None = None

    # -- connecting --------------------------------------------------------
    def connect(self) -> bool:
        self.close()
        if not FORCE_RELAY and self._connect_lan():
            self.route = "lan"
            return True
        print(
            "[P2] LAN unreachable, falling back to the relay ..."
            if not FORCE_RELAY
            else "[P2] relay forced by P2_FORCE_RELAY"
        )
        return self._connect_relay()

    def _connect_lan(self) -> bool:
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=2)
            self.sock.settimeout(3)
            self.sock.sendall(READY)
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise OSError("host closed during handshake")
                text = chunk.decode(errors="ignore").strip()
                print("[P2] server:", text)
                if '"ready"' in text:
                    self.connected = True
                    print(f"[P2] connected to host {self.host}:{self.port} as Player 2")
                    return True
        except OSError as exc:
            print(f"[P2] LAN connect failed: {exc}")
            self.close()
            return False

    def _connect_relay(self) -> bool:
        url = relay_url(self.relay, self.room, "client", self.key)
        self.link = RelayLink(url)
        try:
            self.link.connect()
        except OSError as exc:
            print(f"[P2] relay connect failed: {exc}")
            if not self.key:
                print("[P2] no room key set -- the relay needs the shared key (P2_KEY)")
            self.link = None
            return False

        self.route = "relay"
        self.connected = True
        print(f"[P2] at the relay in room '{self.room}'")
        self.link.send({"t": "p2_ready"})

        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                msg = self.link.poll(0.5)
            except OSError as exc:
                print(f"[P2] relay: {exc}")
                self.connected = False
                return False
            t = msg.get("t")
            if t == "ready":
                print("[P2] handshake OK over the relay -- ready to send input")
                return True
            if t == "relay_ready":
                print("[P2] relay ready:", msg.get("location") or "location unset")
            elif t == "peer_absent":
                print("[P2] Player 1 is not at the relay yet -- your seat is held, keep this open")
            elif t == "peer_present":
                print("[P2] Player 1 is at the relay")
        print("[P2] relay accepted, no 'ready' yet (Player 1 may still be starting)")
        return True

    # -- traffic -----------------------------------------------------------
    def pump(self) -> bool:
        """Drain relay control traffic. False once the relay link is gone."""
        if self.link is None:
            return self.connected
        try:
            msg = self.link.poll(0.5)
        except OSError as exc:
            print(f"[P2] relay: {exc}")
            self.connected = False
            return False
        t = msg.get("t")
        if t == "link_stats":
            rtt = msg.get("rtt_ms")
            peer = bool(msg.get("peer"))
            if peer != self._peer:
                self._peer = peer
                print("[P2] Player 1 is in the room" if peer else "[P2] Player 1 is not in the room", flush=True)
            if isinstance(rtt, int) and (self._shown_rtt is None or abs(rtt - self._shown_rtt) >= 10):
                self._shown_rtt = rtt
                print(f"[P2] relay ping {rtt} ms", flush=True)
        elif t == "peer_joined":
            print("[P2] Player 1 joined the relay room", flush=True)
        elif t == "peer_left":
            print("[P2] Player 1 left the relay room", flush=True)
        elif t == "replaced":
            print("[P2] another Player 2 client took this room", flush=True)
            self.connected = False
            return False
        return True

    def send_input(self, payload: dict) -> None:
        if not self.connected:
            return
        if self.link is not None:
            if not self.link.send(payload):
                self.connected = False
            return
        try:
            self.sock.sendall(json.dumps(payload).encode() + b"\n")
        except OSError:
            self.connected = False
            print("[P2] input send failed, disconnecting")

    def close(self) -> None:
        if self.link is not None:
            try:
                self.link.send({"t": "disconnect"})
            except OSError:
                pass
            self.link.close()
            self.link = None
        if self.sock is not None:
            try:
                self.sock.sendall(json.dumps({"t": "disconnect"}).encode() + b"\n")
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self.connected = False


def main() -> int:
    print("[P2] Player 2 client starting...")
    print(f"[P2] LAN {HOST}:{PORT}")
    print(f"[P2] relay {relay_url(RELAY, ROOM, 'client')}")
    print(f"[P2] room key {'set' if KEY else 'MISSING -- the relay will refuse this client'}")
    client = P2Client()
    if WANT_INPUT:
        threading.Thread(target=run_input, args=(client,), name="p2-input", daemon=True).start()
    else:
        print("[P2] input capture off (P2_INPUT=0)")
    try:
        while True:
            if not client.connect():
                print("[P2] retrying in 5 s ...", flush=True)
                time.sleep(5)
                continue
            while client.pump():
                time.sleep(0.25)
            print("[P2] connection lost -- reconnecting ...", flush=True)
            client.close()
            time.sleep(3)
    except KeyboardInterrupt:
        print("\n[P2] stopping")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
