#!/usr/bin/env python3
"""
P2 host (Player 2 host) -- run on YOUR PC.

Two ways in, both feeding the same handlers:

  LAN    listens on 0.0.0.0:9099 for her client (p2_client.py) on your network
  relay  dials OUT to the Node relay (relay/server.js) as role=host, so she can
         reach you from another country with no port forwarding, no public IP,
         and no link that changes whenever the portal restarts

The relay is the fixed meeting point: it keeps the room and your seat, so you
send her one address once. Point it at the relay you deploy with config.json
("relay": {"base": ..., "room": ...}) or override it per run:

    set P2_RELAY=wss://relay.example.com
    set P2_ROOM=crash
    python p2_host.py            # LAN listener + relay seat
    python p2_host.py --no-relay # LAN listener only

This file is intentionally dependency-free (only the Python standard library).
It is meant to be started by play-coop.cmd, NOT as a standalone service.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import sys
import time

from p2_relay import RelayLink, relay_url

HOST = "0.0.0.0"   # 0.0.0.0 reaches the whole LAN; 127.0.0.1 only this PC
PORT = 9099
READY = b'{"t":"p2_ready"}'
READY_RESPONSE = b'{"t":"ready"}\n'
RETRY_SECONDS = 5.0

HERE = os.path.dirname(os.path.abspath(__file__))

_stop = threading.Event()


def _config() -> dict:
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def relay_settings() -> tuple[str, str, str]:
    """Relay base URL, room and shared key: env wins, then config.json."""
    cfg = _config().get("relay") or {}
    base = os.environ.get("P2_RELAY") or cfg.get("base") or ""
    room = os.environ.get("P2_ROOM") or cfg.get("room") or "crash"
    key = os.environ.get("P2_KEY") or cfg.get("key") or ""
    return base.strip(), room.strip() or "crash", key.strip()


_last_input: str | None = None


def on_message(msg: dict, who: str) -> dict | None:
    """The single place a message from Player 2 lands, whichever route it took."""
    global _last_input
    t = msg.get("t")
    if t == "p2_ready":
        print(f"[P2_HOST] Player 2 handshake from {who} -> ready", flush=True)
        return {"t": "ready"}
    if t == "pad_input":
        # One line per actual change: the 200 ms heartbeat keeps the link warm
        # without turning the console into a wall of identical snapshots.
        data = msg.get("data") or {}
        state = {k: v for k, v in data.items() if k not in ("seq", "ts")}
        blob = json.dumps(state, sort_keys=True)
        if blob != _last_input:
            _last_input = blob
            print(f"[P2_HOST] input from {who}: {blob}", flush=True)
    elif t == "disconnect":
        print(f"[P2_HOST] {who} sent disconnect", flush=True)
    return None


# ---------------------------------------------------------------------------
# LAN listener (her client on the same network)
# ---------------------------------------------------------------------------

def handle(conn: socket.socket, addr: tuple[str, int]) -> None:
    who = f"{addr[0]}:{addr[1]}"
    try:
        conn.settimeout(10)
        data = b""
        while b"\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        if data.split(b"\n")[0].strip() != READY:
            print(f"[P2_HOST] INVALID handshake from {who}: {data!r}", flush=True)
            return
        conn.sendall(READY_RESPONSE)
        print(f"[P2_HOST] Player 2 connected from {who}", flush=True)
        try:
            while not _stop.is_set():
                chunk = conn.recv(4096)
                if not chunk:
                    break
                for line in chunk.split(b"\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except Exception:
                        continue
                    reply = on_message(msg, who)
                    if reply:
                        conn.sendall(json.dumps(reply).encode() + b"\n")
                    if msg.get("t") == "disconnect":
                        return
        finally:
            print(f"[P2_HOST] Player 2 {who} disconnected", flush=True)
    except Exception as exc:
        print(f"[P2_HOST] error with {who}: {exc}", flush=True)
    finally:
        try:
            conn.close()
        except OSError:
            pass


def serve_forever() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((HOST, PORT))
    except OSError as exc:
        print(f"[P2_HOST] cannot bind {HOST}:{PORT}: {exc}", flush=True)
        print("[P2_HOST] LAN path off; the relay seat still runs.", flush=True)
        return
    srv.listen(4)
    srv.settimeout(1.0)
    print(f"[P2_HOST] listening on {HOST}:{PORT} for a LAN Player 2", flush=True)
    while not _stop.is_set():
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        except Exception:
            break
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()


# ---------------------------------------------------------------------------
# Relay seat (her client from anywhere else)
# ---------------------------------------------------------------------------

def relay_loop(base: str, room: str, key: str) -> None:
    url = relay_url(base, room, "host", key)
    # Never print the key: this console gets screenshotted into chat.
    print(f"[P2_HOST] relay seat: {relay_url(base, room, 'host')}", flush=True)
    print(f"[P2_HOST] room key {'set' if key else 'MISSING -- the relay will refuse this host'}", flush=True)
    while not _stop.is_set():
        link = RelayLink(url)
        try:
            link.connect()
            print(f"[P2_HOST] at the relay -- waiting for Player 2 in room '{room}'", flush=True)
            shown_rtt: int | None = None
            shown_peer: bool | None = None
            while not _stop.is_set():
                msg = link.poll(1.0)
                if not msg:
                    continue
                t = msg.get("t")
                if t == "relay_ready":
                    where = msg.get("location") or "unknown"
                    print(f"[P2_HOST] relay ready ({where})", flush=True)
                elif t == "peer_joined":
                    print("[P2_HOST] Player 2 joined the relay room", flush=True)
                elif t == "peer_left":
                    print("[P2_HOST] Player 2 left the relay room", flush=True)
                elif t == "link_stats":
                    peer = bool(msg.get("peer"))
                    if peer != shown_peer:
                        shown_peer = peer
                        print(
                            "[P2_HOST] Player 2 is in the room" if peer else "[P2_HOST] waiting for Player 2 ...",
                            flush=True,
                        )
                    rtt = msg.get("rtt_ms")
                    if isinstance(rtt, int) and (shown_rtt is None or abs(rtt - shown_rtt) >= 10):
                        shown_rtt = rtt
                        print(f"[P2_HOST] relay ping {rtt} ms", flush=True)
                elif t == "replaced":
                    print("[P2_HOST] another host seat took this room; reconnecting", flush=True)
                    break
                else:
                    reply = on_message(msg, f"relay/{room}")
                    if reply:
                        link.send(reply)
        except (OSError, ConnectionError) as exc:
            print(f"[P2_HOST] relay: {exc}", flush=True)
        except Exception as exc:  # never let the relay thread kill the seat
            print(f"[P2_HOST] relay error: {exc}", flush=True)
        finally:
            link.close()
        if _stop.wait(RETRY_SECONDS):
            return


# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    base, room, key = relay_settings()
    use_relay = "--no-relay" not in argv and bool(base)
    if "--no-relay" not in argv and not base:
        print("[P2_HOST] no relay configured (config.json relay.base / P2_RELAY)", flush=True)

    threading.Thread(target=serve_forever, daemon=True).start()
    if use_relay:
        threading.Thread(target=relay_loop, args=(base, room, key), daemon=True).start()

    try:
        while not _stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        print("\n[P2_HOST] stopping", flush=True)
    finally:
        _stop.set()
    return 0


if __name__ == "__main__":
    if any(a in ("--stop", "-q") for a in sys.argv[1:]):
        sys.exit(0)
    sys.exit(main(sys.argv[1:]))
