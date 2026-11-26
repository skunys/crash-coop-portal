#!/usr/bin/env python3
"""
Keyboard + gamepad capture for Player 2, feeding p2_client.send_input().

Windows only, standard library only (ctypes). Two sources are merged into one
state snapshot:

  keyboard  GetAsyncKeyState -- reads the real key state, so it keeps working
            when the game window is in the background
  gamepad   XInput (xinput1_4.dll) -- Xbox-style pads, including DS4/DS5 and
            Switch Pro controllers in XInput mode

The snapshot uses the same key names and the same button indexes as
public/index.html, so the host can drive the virtual pad identically whichever
route her input took:

  keys          held key names, exactly the page's KEYS set
  pad.buttons   pressed indexes: 0=A 1=B 2=X 3=Y 4=LB 5=RB 6=LT 7=RT
                8=Back 9=Start 10=L3 11=R3 12..15 = d-pad up/down/left/right
  pad.triggers  [LT, RT] analog 0..1
  pad.axes      [left x, left y, right x, right y], -1..1, y positive = up

A snapshot is sent the moment it changes, plus a heartbeat every 200 ms so the
host can tell a quiet Player 2 from a dead link.

Try it without the relay:

    python p2_input.py          # prints 5 s of snapshots; hold keys / press pad
"""
from __future__ import annotations

import ctypes
import os
import time

HEARTBEAT_SECONDS = 0.2
SAMPLE_SECONDS = 1 / 60.0

# The page's KEYS set -> Windows virtual-key codes.
VK = {
    "w": 0x57, "a": 0x41, "s": 0x53, "d": 0x44,
    "j": 0x4A, "k": 0x4B, "e": 0x45, "f": 0x46, "r": 0x52, "q": 0x51,
    "space": 0x20, "shift": 0x10, "tab": 0x09, "enter": 0x0D, "escape": 0x1B,
    "arrowup": 0x26, "arrowdown": 0x28, "arrowleft": 0x25, "arrowright": 0x27,
}

# XInput button bit -> the page's standard-mapping button index.
XINPUT_BUTTONS = {
    0x0001: 12,  # d-pad up
    0x0002: 13,  # d-pad down
    0x0004: 14,  # d-pad left
    0x0008: 15,  # d-pad right
    0x0010: 9,   # Start
    0x0020: 8,   # Back
    0x0040: 10,  # L3
    0x0080: 11,  # R3
    0x0100: 4,   # LB
    0x0200: 5,   # RB
    0x1000: 0,   # A
    0x2000: 1,   # B
    0x4000: 2,   # X
    0x8000: 3,   # Y
}
TRIGGER_PRESS = 30   # of 255 -- analogue triggers reported as pressed buttons
STICK_DEAD = 0.16    # same deadzone as the page: worn sticks never centre


class _XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class _XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_ulong), ("Gamepad", _XINPUT_GAMEPAD)]


def _stick(rx: int, ry: int) -> tuple[float, float]:
    """Raw XInput thumb reading (-32768..32767) -> -1..1 with the page's deadzone."""
    x = max(-1.0, min(1.0, rx / 32768.0))
    y = max(-1.0, min(1.0, ry / 32768.0))
    length = (x * x + y * y) ** 0.5
    if length < STICK_DEAD:
        return (0.0, 0.0)
    scaled = min(1.0, (length - STICK_DEAD) / (1.0 - STICK_DEAD))
    return (round(x / length * scaled, 3), round(y / length * scaled, 3))


class Keyboard:
    def __init__(self) -> None:
        self.api = None
        if os.name == "nt":
            try:
                self.api = ctypes.WinDLL("user32", use_last_error=True)
            except OSError as exc:
                print(f"[P2] keyboard capture unavailable: {exc}", flush=True)

    def held(self) -> list[str]:
        if not self.api:
            return []
        api = self.api
        return [name for name, vk in VK.items() if api.GetAsyncKeyState(vk) & 0x8000]


class Gamepad:
    def __init__(self) -> None:
        self.api = None
        self.driver = ""
        self.index: int | None = None
        if os.name != "nt":
            return
        for dll in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
            try:
                self.api = ctypes.WinDLL(dll)
                self.driver = dll
                break
            except OSError:
                continue

    def state(self) -> dict | None:
        """Current pad snapshot, or None when no pad is plugged in."""
        if not self.api:
            return None
        state = _XINPUT_STATE()
        order = [self.index] if self.index is not None else []
        order += [i for i in range(4) if i != self.index]
        for i in order:
            try:
                if self.api.XInputGetState(i, ctypes.byref(state)) == 0:
                    if self.index != i:
                        self.index = i
                        print(f"[P2] gamepad hooked up ({self.driver}, slot {i})", flush=True)
                    return self._parse(state)
            except OSError:
                break
        if self.index is not None:
            print("[P2] gamepad unplugged", flush=True)
            self.index = None
        return None

    def _parse(self, state: _XINPUT_STATE) -> dict:
        g = state.Gamepad
        buttons = [idx for bit, idx in XINPUT_BUTTONS.items() if g.wButtons & bit]
        if g.bLeftTrigger >= TRIGGER_PRESS:
            buttons.append(6)
        if g.bRightTrigger >= TRIGGER_PRESS:
            buttons.append(7)
        lx, ly = _stick(g.sThumbLX, g.sThumbLY)
        rx, ry = _stick(g.sThumbRX, g.sThumbRY)
        return {
            "slot": self.index,
            "buttons": sorted(buttons),
            "triggers": [round(g.bLeftTrigger / 255.0, 3), round(g.bRightTrigger / 255.0, 3)],
            "axes": [lx, ly, rx, ry],
        }


class Sampler:
    def __init__(self) -> None:
        self.keyboard = Keyboard()
        self.gamepad = Gamepad()

    def snapshot(self) -> dict:
        return {"keys": self.keyboard.held(), "pad": self.gamepad.state()}


def run(client, heartbeat: float = HEARTBEAT_SECONDS, sample: float = SAMPLE_SECONDS) -> None:
    """Sample forever and push changes to the host as {t: 'pad_input'}."""
    if os.name != "nt":
        print("[P2] input capture is Windows-only; nothing to sample here", flush=True)
        return
    sampler = Sampler()
    seq = 0
    last_state = None
    last_sent = 0.0
    while True:
        time.sleep(sample)
        state = sampler.snapshot()
        now = time.time()
        if state == last_state and (now - last_sent) < heartbeat:
            continue
        last_state = state
        if not getattr(client, "connected", False):
            continue
        seq += 1
        client.send_input(
            {"t": "pad_input", "data": {**state, "seq": seq, "ts": int(now * 1000)}}
        )
        last_sent = now


if __name__ == "__main__":
    probe = Sampler()
    print("sampling for 5 s -- hold keys, press pad buttons (Ctrl+C to stop)")
    end = time.time() + 5
    try:
        while time.time() < end:
            print(probe.snapshot(), flush=True)
            time.sleep(0.4)
    except KeyboardInterrupt:
        pass
