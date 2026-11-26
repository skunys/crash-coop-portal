#!/usr/bin/env python3
"""
Crash: Mind over Mutant co-op portal
====================================

One friend, one browser URL.  This host app does three things:

  1. capture + encode the game with ffmpeg (NVENC) and publish it to a local
     MediaMTX instance, which re-serves it to the browser over WebRTC (WHEP);
  2. serve the small web page (with a token) and proxy the WHEP signalling;
  3. take the friend's keys over a WebSocket and drive a **virtual Xbox 360
     pad** through ViGEmBus -- that pad is player 3 in the game, next to your
     keyboard/mouse (player 1) and your local pad (player 2).

Only the standard library is used, plus `vgamepad` (ViGEmBus binding).
Run:  python portal.py            (add --no-capture to test the page alone)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
PUBLIC = HERE / "public"
LOGDIR = HERE / "logs"
LOGDIR.mkdir(exist_ok=True)

STAMP = datetime.now().strftime("%Y-%m-%d_%H%M%S")
LOG_FILE = LOGDIR / f"portal-{STAMP}.log"
_log_lock = threading.Lock()


def log(*parts: object) -> None:
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), " ".join(str(p) for p in parts))
    with _log_lock:
        print(line, flush=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# ---------------------------------------------------------------------------
# Virtual pad (player 3)
# ---------------------------------------------------------------------------

class Pad:
    """One ViGEm virtual Xbox 360 pad, updated from the friend's keys."""

    # The game's own keyboard layout (user/controls.toml) -> pad, so the friend
    # plays exactly like you would on the keyboard.
    KEY_TO_BUTTON = {
        "space": "A",      # jump / double jump
        "j": "X",          # light attack
        "k": "Y",          # heavy attack
        "e": "B",          # jack a titan / save totem
        "f": "LT",         # titan special
        "r": "RB",         # pocket a titan
        "tab": "BACK",     # map
        "enter": "START",  # join / pause menu
        "escape": "START",
    }

    def __init__(self, slot: int) -> None:
        self.slot = slot
        self.lock = threading.Lock()
        self.held: set[str] = set()
        self.stick = (0.0, 0.0)
        # Analog values from the friend's own controller (Gamepad API in the
        # browser); None means "use the keyboard directions" instead.
        self.stick_override: tuple[float, float] | None = None
        self.walk = False
        self.spin_until = 0.0
        self.spin_angle = 0.0
        self.connected_clients = 0
        # The game only reads the pad while it owns the foreground; these tell
        # the focus keeper when input is actually being used.
        self.last_key_at = 0.0
        self.focus_wanted = threading.Event()
        self.pad = None
        self.error: str | None = None
        self._open()

    def _open(self) -> None:
        try:
            import vgamepad as vg  # noqa: PLC0415  (import cost is the driver probe)

            self.vg = vg
            self.pad = vg.VX360Gamepad()
            self.pad.reset()
            self.pad.update()
            log(f"pad: virtual Xbox 360 pad created for player {self.slot}")
        except Exception as exc:  # driver missing / vgamepad broken
            self.error = str(exc)
            log("pad: FAILED to create the virtual pad:", exc)
            log("pad: install ViGEmBus (portal/bin/ViGEmBus_setup.exe) and reboot, then restart.")

    # --- input -------------------------------------------------------------
    def key(self, name: str, down: bool) -> None:
        name = name.lower()
        with self.lock:
            if down:
                self.held.add(name)
                self.last_key_at = time.monotonic()
                self.focus_wanted.set()
            else:
                self.held.discard(name)
            if down and name == "q":          # spin: a stick circle, not a button
                self.spin_until = time.monotonic() + 0.35

    def set_stick(self, x: float, y: float) -> None:
        """Left stick from her real pad: -1..1 each, y positive = up."""
        with self.lock:
            self.stick_override = (max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y)))
            self.last_key_at = time.monotonic()
            self.focus_wanted.set()

    def client_joined(self) -> None:
        with self.lock:
            self.connected_clients += 1
            self.held.clear()
            self.spin_until = 0.0
            self.stick_override = None
            self.last_key_at = time.monotonic()
            self.focus_wanted.set()

    def client_left(self) -> None:
        with self.lock:
            self.connected_clients = max(0, self.connected_clients - 1)
            self.held.clear()
            self.spin_until = 0.0
            self.stick_override = None
            self._apply(0.0, 0.0, set(), None)

    # --- state machine -----------------------------------------------------
    def _axis(self) -> tuple[float, float]:
        if self.stick_override is not None:
            x, y = self.stick_override
            if self.walk:
                x *= 0.75
                y *= 0.75
            return x, y
        held = self.held
        left = "a" in held or "left" in held
        right = "d" in held or "right" in held
        up = "w" in held or "up" in held
        down = "s" in held or "down" in held
        x = (1.0 if right else 0.0) - (1.0 if left else 0.0)
        y = (1.0 if up else 0.0) - (1.0 if down else 0.0)
        if x and y:                      # diagonal: same length as a stick push
            x *= 0.7071
            y *= 0.7071
        if self.walk:
            x *= 0.75
            y *= 0.75
        return x, y

    def _apply(self, x: float, y: float, pressed: set[str], spin_angle: float | None) -> None:
        if not self.pad:
            return
        vg = self.vg
        b = vg.XUSB_BUTTON
        mapping = {
            "A": b.XUSB_GAMEPAD_A,
            "B": b.XUSB_GAMEPAD_B,
            "X": b.XUSB_GAMEPAD_X,
            "Y": b.XUSB_GAMEPAD_Y,
            "BACK": b.XUSB_GAMEPAD_BACK,
            "START": b.XUSB_GAMEPAD_START,
        }
        for name, code in mapping.items():
            if name in pressed:
                self.pad.press_button(button=code)
            else:
                self.pad.release_button(button=code)
        self.pad.right_trigger(value=255 if "RT" in pressed else 0)
        self.pad.left_trigger(value=255 if "LT" in pressed else 0)
        self.pad.right_joystick(x_value=0, y_value=0)
        if spin_angle is not None:
            self.pad.left_joystick(x_value=int(32767 * __import__("math").cos(spin_angle)),
                                   y_value=int(32767 * __import__("math").sin(spin_angle)))
        else:
            self.pad.left_joystick(x_value=int(x * 32767), y_value=int(y * 32767))
        self.pad.update()

    def tick(self) -> None:
        if not self.pad:
            return
        import math

        with self.lock:
            if self.connected_clients <= 0:
                return
            pressed: set[str] = set()
            for key in self.held:
                if key in self.KEY_TO_BUTTON:
                    pressed.add(self.KEY_TO_BUTTON[key])
            if "shift" in self.held:
                pressed.add("RT")
            x, y = self._axis()
            now = time.monotonic()
            spin = None
            if now < self.spin_until or "q" in self.held:
                self.spin_angle = (self.spin_angle - 2 * math.pi * 3.0 / 60.0) % (2 * math.pi)
                spin = self.spin_angle
            self._apply(x, y, pressed, spin)


# ---------------------------------------------------------------------------
# ffmpeg + MediaMTX children
# ---------------------------------------------------------------------------

def find_game_hwnd(prefix: str = "crash_mom") -> int | None:
    """The game's own window handle, found by title prefix (gdigrab wants it)."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hits: list[int] = []
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value.lower().startswith(prefix.lower()):
                    hits.append(int(hwnd))
                    return False
            return True

        user32.EnumWindows(proc(visit), 0)
        return hits[0] if hits else None
    except Exception as exc:
        log("capture: window lookup failed:", exc)
        return None


# Streamed width: 1280 keeps a 16:9 game at 720p60, which is far kinder to a
# transatlantic link than 1080p60 (and slightly lowers the HLS delay).
WIDTH = int(CONFIG.get("width", 1280))


AUDIO_RATE = 48000
AUDIO_BLOCK = 1024          # ~21 ms of stereo s16


class AudioLoop:
    """Game sound -> raw s16le stereo on ffmpeg's stdin.

    This PC's audio driver exposes no "Stereo Mix" capture endpoint, so dshow
    cannot hear the speakers.  WASAPI loopback (the `soundcard` package) records
    whatever the default output device is playing, which is exactly the game.

    The loopback endpoint delivers nothing at all while the PC is silent (a
    paused game, a quiet menu), so the reader thread only fills a queue and a
    paced writer keeps feeding ffmpeg, padding with silence when the queue runs
    dry.  Without that padding the AAC track starves, MediaMTX writes empty
    audio fragments, and the browser's player dies on them (fatal mediaError).
    """

    def __init__(self) -> None:
        self.sc = None
        self.available = False
        self.stop_flag = threading.Event()
        self.thread: threading.Thread | None = None
        self.blocks: queue.Queue[bytes] = queue.Queue(maxsize=16)
        try:
            import soundcard  # noqa: PLC0415  (optional dependency)
            self.sc = soundcard
            self.available = True
        except Exception as exc:
            log("audio: unavailable, streaming video only:", exc)
            log("audio: enable it with  pip install soundcard numpy")

    def start(self, sink) -> None:
        if not self.available:
            return
        self.stop_flag.clear()
        self.blocks = queue.Queue(maxsize=16)
        self.thread = threading.Thread(target=self._pump, args=(sink,), daemon=True)
        self.thread.start()

    def _read(self, mic) -> None:
        """WASAPI reader: real audio into the queue, nothing at all when idle."""
        try:
            with mic.recorder(samplerate=AUDIO_RATE, channels=2) as rec:
                while not self.stop_flag.is_set():
                    data = rec.record(numframes=AUDIO_BLOCK)
                    pcm = (data * 32767.0).clip(-32768, 32767).astype("<i2").tobytes()
                    if self.blocks.full():      # encoder is behind: drop the oldest
                        try:
                            self.blocks.get_nowait()
                        except queue.Empty:
                            pass
                    try:
                        self.blocks.put_nowait(pcm)
                    except queue.Full:
                        pass
        except Exception as exc:
            if not self.stop_flag.is_set():
                log("audio: reader stopped:", exc)

    def _pump(self, sink) -> None:
        sc = self.sc
        want = str(CONFIG.get("audio_device", "")).lower()
        silence = bytes(AUDIO_BLOCK * 2 * 2)      # stereo s16
        period = AUDIO_BLOCK / AUDIO_RATE
        while not self.stop_flag.is_set():
            try:
                if want:
                    # config.json can name the output (e.g. "hyperx") -- Windows
                    # mixes each device separately, so the loopback has to be the
                    # one the game actually plays through.
                    matches = [s for s in sc.all_speakers() if want in s.name.lower()]
                    speaker = matches[0] if matches else sc.default_speaker()
                else:
                    speaker = sc.default_speaker()
                mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
                log("audio: loopback of", speaker.name)
                self.blocks = queue.Queue(maxsize=16)
                threading.Thread(target=self._read, args=(mic,), daemon=True).start()
                next_at = time.monotonic()
                while not self.stop_flag.is_set():
                    wait = next_at - time.monotonic()
                    if wait > 0:
                        time.sleep(min(wait, period))
                    try:
                        pcm = self.blocks.get_nowait()
                    except queue.Empty:
                        pcm = silence               # keep the track alive
                    sink.write(pcm)
                    sink.flush()
                    next_at += period
                    if time.monotonic() - next_at > 0.5:   # resync after a stall
                        next_at = time.monotonic()
            except (BrokenPipeError, ValueError, OSError) as exc:
                if self.stop_flag.is_set():
                    return
                log("audio: capture hiccup, retrying:", exc)
                time.sleep(0.5)
            except Exception as exc:
                log("audio: stopped:", exc)
                return

    def stop(self) -> None:
        self.stop_flag.set()


class Capture:
    def __init__(self) -> None:
        # Resolve the ffmpeg path: honour config.json, but fall back to common
        # install locations so the portal works on a fresh cloud PC without
        # editing the config.
        ffmpeg = CONFIG["ffmpeg"]
        if not Path(ffmpeg).exists():
            for cand in (r"C:\ffmpeg\bin\ffmpeg.exe",
                         r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
                         "ffmpeg"):
                if cand == "ffmpeg" or Path(cand).exists():
                    ffmpeg = cand
                    break
        self.ffmpeg = ffmpeg
        self.fps = CONFIG.get("fps", 60)
        self.bitrate = CONFIG.get("bitrate", "12M")
        self.audio = AudioLoop() if CONFIG.get("audio", True) else None
        # HLS parts must start on a keyframe, otherwise the player can be handed
        # half a GOP and fails to decode it. One keyframe per part duration keeps
        # every part independently decodable.
        part_ms = int(CONFIG.get("part_ms", 200))
        self.gop = max(1, round(self.fps * part_ms / 1000.0))
        self.mode_index = 0
        self.last_prefer = 0.0
        self.proc: subprocess.Popen | None = None
        self.started = 0.0
        self.log_path = LOGDIR / f"capture-{STAMP}.log"
        self.fh = self.log_path.open("w", encoding="utf-8")

    def video_modes(self) -> list[tuple[list[str], list[str]]]:
        """(input args, video filter args) candidates, best first."""
        fps = str(self.fps)
        modes: list[tuple[list[str], list[str]]] = []
        hwnd = find_game_hwnd(CONFIG.get("window_title", "crash_mom"))
        if hwnd:
            # 1. the game's own window only (so the friend never sees your
            #    desktop -- browsers, chat, another game).
            log(f"capture: game window hwnd {hwnd}")
            modes.append((["-f", "gdigrab", "-framerate", fps, "-i", f"hwnd={hwnd}"],
                          ["-vf", f"scale={WIDTH}:-2"]))
        else:
            log("capture: the game window was not found -- capturing the desktop instead")
        modes += [
            # 2. GPU desktop duplication, downloaded to system memory for NVENC.
            #    (Verified on this PC: NVENC refuses ddagrab's d3d11 hw frames
            #    directly -- "Impossible to convert between the formats" -- so
            #    this is the reliable low-latency mode.)
            (["-f", "lavfi", "-i", f"ddagrab=output_idx=0:framerate={fps}"],
             ["-vf", f"hwdownload,format=bgra,scale={WIDTH}:-2,format=yuv420p"]),
            # 3. Plain GDI capture of the desktop (works everywhere, uses more CPU).
            (["-f", "gdigrab", "-framerate", fps, "-i", "desktop"],
             ["-vf", f"scale={WIDTH}:-2"]),
        ]
        return modes

    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        modes = self.video_modes()
        self.mode_index %= len(modes)
        video_in, vf = modes[self.mode_index]
        audio = self.audio is not None and self.audio.available
        # every input first (-vf/-map are output options), then the filters
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "warning", *video_in]
        if audio:
            # raw PCM pushed by AudioLoop on stdin; AAC is what HLS wants
            cmd += ["-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", "2", "-i", "pipe:0"]
        else:
            cmd += ["-nostdin"]
        cmd += [*vf]
        if audio:
            cmd += ["-map", "0:v:0", "-map", "1:a:0",
                    "-c:a", "aac", "-b:a", CONFIG.get("audio_bitrate", "128k"),
                    "-ar", str(AUDIO_RATE), "-af", "aresample=async=1"]
        else:
            cmd += ["-map", "0:v:0"]
        cmd += [
            "-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull",
            "-rc", "cbr", "-b:v", self.bitrate, "-maxrate", self.bitrate,
            "-bufsize", "6M", "-g", str(self.gop), "-bf", "0",
            "-pix_fmt", "yuv420p",
            "-f", "rtsp", "-rtsp_transport", "tcp", CONFIG["rtsp_publish"],
        ]
        log("capture: mode", self.mode_index + 1, "of", len(modes), "| audio:", audio)
        log("capture:", " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd, stdout=self.fh, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if audio else subprocess.DEVNULL)
        self.started = time.monotonic()
        if audio:
            self.audio.start(self.proc.stdin)

    def check(self) -> None:
        """Restart ffmpeg when it dies, trying the next capture mode."""
        if not self.proc:
            return
        code = self.proc.poll()
        if code is None:
            # Started before the game window existed? Then this is desktop
            # capture, which would stream your whole desktop to the friend.
            # Move back to the game's own window as soon as it shows up.
            if (self.mode_index != 0 and time.monotonic() - self.last_prefer > 30
                    and find_game_hwnd(CONFIG.get("window_title", "crash_mom"))):
                log("capture: the game window is here now; capturing it instead")
                self.last_prefer = time.monotonic()
                self.mode_index = 0
                self.stop()
                time.sleep(1.0)
                self.start()
            return
        log(f"capture: ffmpeg exited with {code}; trying the next mode")
        self.mode_index += 1
        time.sleep(1.0)
        self.start()

    def stop(self) -> None:
        if self.audio:
            self.audio.stop()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class Tunnel:
    """Public https link with zero setup on the friend's side (cloudflared
    quick tunnel).  The page and the key WebSocket ride it directly; the video
    goes through it as low-latency HLS, because WebRTC needs UDP that a plain
    HTTPS tunnel cannot carry."""

    def __init__(self) -> None:
        cf = CONFIG.get("cloudflared", "")
        if not cf or not Path(cf).exists():
            # Try common install locations for a fresh cloud PC.
            for cand in (r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
                         r"C:\Program Files\cloudflared\cloudflared.exe",
                         "cloudflared"):
                if cand == "cloudflared" or Path(cand).exists():
                    cf = cand
                    break
        self.exe = cf
        self.url: str | None = None
        self.proc: subprocess.Popen | None = None
        self.port = int(CONFIG.get("http_port", 8080))
        self.log_path = LOGDIR / f"tunnel-{STAMP}.log"
        self.fh = self.log_path.open("w", encoding="utf-8")
        self.stopping = False
        self.restarts = 0

    def start(self) -> None:
        if not Path(self.exe).exists():
            log("tunnel: cloudflared not found at", self.exe)
            log("tunnel: install it with  winget install Cloudflare.cloudflared")
            return
        self._spawn()

    def _spawn(self) -> None:
        self.url = None
        log("tunnel: starting cloudflared quick tunnel")
        self.proc = subprocess.Popen(
            [self.exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, text=True, bufsize=1)
        threading.Thread(target=self._watch, args=(self.proc,), daemon=True).start()

    def _watch(self, proc: subprocess.Popen) -> None:
        assert proc.stderr
        for line in proc.stderr:
            self.fh.write(line)
            self.fh.flush()
            if self.url:
                continue
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m:
                self.url = m.group(0)
                link = f"{self.url}/?k={TOKEN}"
                log("tunnel: PUBLIC", link)
                log("tunnel: send that link -- nothing to install on her side")
                try:
                    LINK_FILE.write_text(link + "\n", encoding="utf-8")
                except OSError:
                    pass
        code = proc.poll()
        if self.stopping:
            return
        # A quick tunnel drops out now and then (Cloudflare side, network
        # blips). Left alone it stays down and the friend's link is dead, so
        # bring it straight back.
        self.restarts += 1
        log(f"tunnel: cloudflared exited ({code}); restarting in 5 s")
        time.sleep(5)
        if not self.stopping:
            self._spawn()

    def stop(self) -> None:
        self.stopping = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class MediaMTX:
    def __init__(self) -> None:
        # Resolve relative paths against the portal folder, so the config
        # works on any machine (home PC or cloud PC) without editing.
        self.exe = str((HERE / CONFIG["mediamtx"]).resolve()) if not Path(CONFIG["mediamtx"]).is_absolute() else CONFIG["mediamtx"]
        self.cfg = str((HERE / CONFIG["mediamtx_config"]).resolve()) if not Path(CONFIG["mediamtx_config"]).is_absolute() else CONFIG["mediamtx_config"]
        self.proc: subprocess.Popen | None = None
        self.log_path = LOGDIR / f"mediamtx-{STAMP}.log"
        self.fh = self.log_path.open("w", encoding="utf-8")

    def start(self) -> None:
        log("mediamtx: starting")
        self.proc = subprocess.Popen([self.exe, self.cfg],
                                     cwd=str(Path(self.exe).parent),
                                     stdout=self.fh, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL)
        # wait for the RTSP port, so ffmpeg's first attempt doesn't fail
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", 8554), timeout=0.2):
                    log("mediamtx: RTSP ready on 127.0.0.1:8554")
                    return
            except OSError:
                time.sleep(0.2)
        log("mediamtx: RTSP did not come up; check", self.log_path)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# ---------------------------------------------------------------------------
# Minimal WebSocket (server side) -- keys only, a few bytes per message
# ---------------------------------------------------------------------------

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_handshake(handler: BaseHTTPRequestHandler) -> socket.socket | None:
    key = handler.headers.get("Sec-WebSocket-Key")
    if not key:
        handler.send_error(400, "missing Sec-WebSocket-Key")
        return None
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept)
    handler.end_headers()
    handler.wfile.flush()
    return handler.connection


def ws_read(conn: socket.socket) -> dict | None:
    """Read one client frame; returns the decoded JSON message or None."""
    head = conn.recv(2)
    if len(head) < 2:
        return None
    opcode = head[0] & 0x0F
    masked = head[1] & 0x80
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", conn.recv(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", conn.recv(8))[0]
    mask = conn.recv(4) if masked else b""
    payload = b""
    while len(payload) < length:
        chunk = conn.recv(length - len(payload))
        if not chunk:
            break
        payload += chunk
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    if opcode == 8:
        return None
    if opcode == 9:      # ping -> pong
        ws_send(conn, payload, opcode=10)
        return {}
    if opcode != 1:
        return {}
    try:
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return {}


def ws_send(conn: socket.socket, data: bytes, opcode: int = 1) -> None:
    header = bytes([0x80 | opcode])
    n = len(data)
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += bytes([126]) + struct.pack(">H", n)
    else:
        header += bytes([127]) + struct.pack(">Q", n)
    try:
        conn.sendall(header + data)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# HTTP: page, WHEP proxy, WebSocket input
# ---------------------------------------------------------------------------

TOKEN = CONFIG["token"]
REQUIRE_TOKEN = bool(CONFIG.get("require_token", False))
LINK_FILE = HERE / "current-link.txt"

# A link that lost its ?k= part (chat apps and messengers strip query strings
# surprisingly often) used to land on a bare "403 bad token" page with nothing
# to click, which looks exactly like a broken page. Say what is wrong instead.
DENIED_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crash co-op - link incomplete</title>
<style>
  html,body{margin:0;height:100%;background:#07090d;color:#e8eef7;
    font:16px/1.5 system-ui,"Segoe UI",sans-serif;display:grid;place-items:center;padding:24px}
  h1{font-size:22px;margin:0 0 10px} p{color:#9fb0c7;max-width:48ch;margin:8px 0}
  code{background:#16233a;border-radius:5px;padding:2px 6px}
  button{margin-top:20px;padding:12px 26px;font-size:16px;font-weight:600;border:0;
    border-radius:999px;cursor:pointer;color:#08131f;background:linear-gradient(180deg,#7ee0ff,#35b9e8)}
</style></head><body><div>
<h1>This link is incomplete</h1>
<p>The access key is missing. The link has to end with <code>?k=...</code> — ask for
   the whole link again and open all of it, including everything after the <code>?</code>.</p>
<p>The key is long, so a chat app may have cut the end off. Copying it straight
   into the address bar works best.</p>
<button onclick="location.reload()">Try again</button>
</div></body></html>
"""
# A browser that closes the tab or skips ahead aborts its downloads; that must
# never take the host app down with it (it used to: the tunnel link then went
# dead for the friend).
GONE = (ConnectionError, TimeoutError)


def write_out(wfile, data: bytes) -> None:
    try:
        wfile.write(data)
        wfile.flush()
    except GONE:
        pass          # the browser hung up; nothing to report


MXTX = CONFIG["mediamtx_http"].rstrip("/")
HLS = CONFIG.get("hls_http", "http://127.0.0.1:8888").rstrip("/")
TUNNEL = None  # type: Tunnel | None
PATH_NAME = CONFIG.get("path_name", "game")
PAD = None  # type: Pad | None


def authed(handler: BaseHTTPRequestHandler) -> bool:
    if handler.path.split("?")[0] == "/healthz":
        return True
    # The key is optional by default: a link that lost its ?k= part (chat apps
    # cut query strings) used to dead-end on an error page, which looks like a
    # broken site. With "require_token": true in config.json only the full
    # ?k= link works -- the trade-off is that the random tunnel address is then
    # the only secret, so anyone who has it can play.
    if not REQUIRE_TOKEN:
        return True
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(handler.path).query)
    if q.get("k", [""])[0] == TOKEN:
        return True
    cookie = handler.headers.get("Cookie", "")
    return f"portal_token={TOKEN}" in cookie


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter console
        return

    # -- helpers ----------------------------------------------------------
    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _proxy(self, method: str, target: str, body: bytes = b"", ctype: str | None = None):
        req = urllib.request.Request(target, data=body if body else None, method=method)
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
                status = resp.status
                headers = dict(resp.headers)
        except urllib.error.HTTPError as err:
            data = err.read()
            status = err.code
            headers = dict(err.headers)
        except Exception as exc:
            log("whep proxy error:", exc)
            self.send_error(502, "mediamtx unreachable")
            return
        self.send_response(status)
        loc = headers.get("Location")
        if loc:
            # Keep the browser on our origin, whatever mediamtx says.
            m = re.search(rf"/{PATH_NAME}/whep/([^/]+)$", loc)
            self.send_header("Location", f"/whep/{m.group(1)}" if m else loc)
        for h in ("Content-Type", "Access-Control-Allow-Origin"):
            if headers.get(h):
                self.send_header(h, headers[h])
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            write_out(self.wfile, data)

    def _proxy_get(self, url: str) -> None:
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type", "application/octet-stream")
                status = resp.status
        except urllib.error.HTTPError as err:
            self.send_error(err.code, "hls upstream error")
            return
        except Exception as exc:
            log("hls proxy error:", exc)
            self.send_error(502, "hls unreachable")
            return
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if data:
            write_out(self.wfile, data)

    # -- routes -----------------------------------------------------------
    def _denied(self) -> None:
        page = DENIED_PAGE.encode()
        try:
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            write_out(self.wfile, page)
        except GONE:
            pass

    def do_GET(self):
        if not authed(self):
            self._denied()
            return
        path = self.path.split("?")[0]
        if path == "/healthz":
            body = json.dumps({
                "ok": True,
                "pad": bool(PAD and PAD.pad),
                "pad_error": PAD.error if PAD else "none",
                "clients": PAD.connected_clients if PAD else 0,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            write_out(self.wfile, body)
            return
        if path == "/ws":
            self._websocket()
            return
        if path.startswith("/hls/"):
            # LL-HLS for the public tunnel link (and anything else https).
            tail = self.path[len("/hls/"):]
            self._proxy_get(f"{HLS}/{tail}")
            return
        if path in ("/", "/index.html"):
            page = (PUBLIC / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            # The page is edited constantly while fixing things; without this a
            # browser happily keeps serving an old copy, so fixes appear to do
            # nothing on the friend's side.
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Set-Cookie", f"portal_token={TOKEN}; Path=/; HttpOnly; SameSite=Lax")
            self.end_headers()
            write_out(self.wfile, page)
            return
        self.send_error(404)

    def do_POST(self):
        if not authed(self):
            self.send_error(403, "bad token")
            return
        body = self._body()
        if self.path.startswith("/whep"):
            self._proxy("POST", f"{MXTX}/{PATH_NAME}/whep", body,
                        self.headers.get("Content-Type"))
            return
        self.send_error(404)

    def do_PATCH(self):
        if not authed(self):
            self.send_error(403, "bad token")
            return
        body = self._body()
        if self.path.startswith("/whep/"):
            rid = self.path.split("/whep/", 1)[1]
            self._proxy("PATCH", f"{MXTX}/{PATH_NAME}/whep/{rid}", body,
                        self.headers.get("Content-Type"))
            return
        self.send_error(404)

    def do_DELETE(self):
        if not authed(self):
            self.send_error(403, "bad token")
            return
        if self.path.startswith("/whep/"):
            rid = self.path.split("/whep/", 1)[1]
            self._proxy("DELETE", f"{MXTX}/{PATH_NAME}/whep/{rid}")
            return
        self.send_error(404)

    # -- websocket input ---------------------------------------------------
    def _websocket(self):
        conn = ws_handshake(self)
        if conn is None or PAD is None:
            return
        peer = self.client_address[0]
        PAD.client_joined()
        stick_logged = False
        log(f"ws: friend connected from {peer} ({PAD.connected_clients} active)")
        try:
            while True:
                msg = ws_read(conn)
                if msg is None:
                    break
                if not msg:
                    continue
                kind = msg.get("t")
                if kind == "k" and "c" in msg:
                    PAD.key(str(msg["c"]), bool(msg.get("d")))
                    if msg.get("d"):
                        # "pad button" = came from her controller, "key" = her
                        # keyboard; the log then says which path is alive.
                        log("ws: pad button down" if msg.get("src") == "pad" else "ws: key down",
                            msg["c"])
                elif kind == "hello":
                    # Which copy of the page is she actually running?
                    log("ws: page version", msg.get("ver"))
                elif kind == "padinfo":
                    # What her browser can see, reported by the page itself.
                    if not msg.get("id"):
                        log("ws: her browser sees NO controller")
                    else:
                        log(f"ws: her browser sees pad '{msg.get('id')}' "
                            f"(mapping {msg.get('mapping') or 'none'}, "
                            f"{msg.get('axes')} axes, {msg.get('buttons')} buttons)")
                elif kind == "stick":
                    try:
                        PAD.set_stick(float(msg.get("x", 0.0)), float(msg.get("y", 0.0)))
                        if not stick_logged:
                            stick_logged = True
                            log("ws: her pad stick is active")
                    except (TypeError, ValueError):
                        pass
                elif kind == "walk":
                    with PAD.lock:
                        PAD.walk = bool(msg.get("d"))
                elif kind == "ping":
                    ws_send(conn, json.dumps({"t": "pong", "ms": msg.get("ms")}).encode())
        except OSError:
            pass
        finally:
            PAD.client_left()
            log(f"ws: friend disconnected ({PAD.connected_clients} active)")
            try:
                conn.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def pad_loop(pad: Pad, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            pad.tick()
        except Exception:
            log("pad tick error:", traceback.format_exc(limit=1))
        time.sleep(1 / 60)


def keep_active(pad: Pad, stop: threading.Event) -> None:
    """The game only reads the pad while its own window is the **foreground**
    window, and it pauses itself when it loses focus. Posting WM_ACTIVATE is
    not enough (the window never really becomes active, so the pad is ignored
    and prompts like "press any button to resume" never clear). While the
    friend is playing, actually take the foreground with AttachThreadInput.
    """
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
    except Exception:
        return

    def steal(hwnd: int) -> bool:
        fg = user32.GetForegroundWindow()
        if fg == hwnd:
            return True
        this_thread = kernel32.GetCurrentThreadId()
        fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = False
        try:
            if fg_thread and fg_thread != this_thread:
                attached = bool(user32.AttachThreadInput(fg_thread, this_thread, True))
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)          # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
            user32.SetActiveWindow(hwnd)
        except Exception:
            return False
        finally:
            if attached:
                user32.AttachThreadInput(fg_thread, this_thread, False)
        return user32.GetForegroundWindow() == hwnd

    hwnd = None
    last_steal = 0.0
    logged = False
    while not stop.is_set():
        try:
            now = time.monotonic()
            active = pad.connected_clients > 0 and now - pad.last_key_at < 10.0
            if active:
                pad.focus_wanted.clear()
                if not hwnd or not user32.IsWindow(hwnd):
                    hwnd = find_game_hwnd()
                # Take focus the moment a key arrives, then re-check every 2 s
                # (a page reload or an alt-tab otherwise ends the session).
                if hwnd and now - last_steal > 2.0 and user32.GetForegroundWindow() != hwnd:
                    if steal(hwnd):
                        last_steal = now
                        if not logged:
                            log("focus: keeping the game in the foreground for player 2")
                            logged = True
        except Exception:
            pass
        time.sleep(0.4)


def main() -> int:
    global PAD, TUNNEL
    no_capture = "--no-capture" in sys.argv
    log("portal: Crash: Mind over Mutant co-op portal")
    log("portal: token =", TOKEN,
        "(required)" if REQUIRE_TOKEN else "(optional -- the address alone is enough)")

    # One portal only: a second copy would fight over the RTSP path and the pad.
    port = int(CONFIG.get("http_port", 8080))
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("0.0.0.0", port))
    except OSError:
        log(f"portal: port {port} is already in use -- a portal is already running.")
        log("portal: close that window first (it is the one with the game link).")
        return 1
    finally:
        probe.close()

    PAD = Pad(CONFIG.get("pad_slot", 3))

    mxtx = None
    cap = None
    if not no_capture:
        mxtx = MediaMTX()
        mxtx.start()
        cap = Capture()
        cap.start()

    stop = threading.Event()
    threading.Thread(target=pad_loop, args=(PAD, stop), daemon=True).start()
    threading.Thread(target=keep_active, args=(PAD, stop), daemon=True).start()

    if cap:
        def watchdog():
            while not stop.is_set():
                cap.check()
                time.sleep(2)
        threading.Thread(target=watchdog, daemon=True).start()

    host, port = "0.0.0.0", int(CONFIG.get("http_port", 8080))
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print_links(port)
    if CONFIG.get("tunnel", True) and not no_capture:
        TUNNEL = Tunnel()
        TUNNEL.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log("portal: shutting down")
        stop.set()
        if cap:
            cap.stop()
        if mxtx:
            mxtx.stop()
        if PAD.pad:
            try:
                PAD.pad.reset()
                PAD.pad.update()
            except Exception:
                pass
        if TUNNEL:
            TUNNEL.stop()
    return 0


def public_ip() -> str | None:
    """This machine's public IPv4, for the link the friend opens."""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=6) as resp:
                ip = resp.read().decode().strip()
            if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
                return ip
        except Exception:
            continue
    return None


def print_links(port: int) -> None:
    log(f"portal: page at   http://127.0.0.1:{port}/?k={TOKEN}")
    for ip in _local_ips():
        if ip.startswith("169.254."):
            continue
        log(f"portal: LAN         http://{ip}:{port}/?k={TOKEN}")
    ip = public_ip()
    if ip:
        log(f"portal: PUBLIC      http://{ip}:{port}/?k={TOKEN}")
        log("portal:            (works from outside only with the router forwarding"
            " TCP %d and UDP 8189 to this PC)" % port)
    log("portal:            no router work? use the tunnel link printed below")


def _local_ips() -> list[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ips.discard("127.0.0.1")
    return sorted(ips)


if __name__ == "__main__":
    sys.exit(main())
