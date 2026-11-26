#!/usr/bin/env python3
"""Keep the game out of its "window lost focus" pause.

Crash: Mind over Mutant pauses the whole game (and therefore ignores the
co-op portal's virtual pad) as soon as its window stops being the foreground
window.  With this running, the game is nudged into believing it is always
active, so a friend can keep playing while you are in the browser, Discord,
anywhere.  The host app starts this itself; run it by hand only if the portal
was started without it.

    python keep-active.py            # runs until Ctrl+C
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

WM_ACTIVATE, WM_ACTIVATEAPP, WM_SETFOCUS = 0x0006, 0x001C, 0x0007


def find_game_hwnd(prefix: str = "crash_mom") -> int | None:
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
            if buf.value.lower().startswith(prefix):
                hits.append(int(hwnd))
                return False
        return True

    user32.EnumWindows(proc(visit), 0)
    return hits[0] if hits else None


def main() -> int:
    user32 = ctypes.windll.user32
    hwnd = find_game_hwnd()
    if not hwnd:
        print("game window not found -- is Crash running?")
        return 1
    print(f"keeping hwnd 0x{hwnd:x} active; Ctrl+C to stop")
    try:
        while True:
            if not user32.IsWindow(hwnd):
                hwnd = find_game_hwnd()
                if not hwnd:
                    time.sleep(1.0)
                    continue
            if user32.GetForegroundWindow() != hwnd:
                user32.PostMessageW(hwnd, WM_ACTIVATE, 1, 0)
                user32.PostMessageW(hwnd, WM_ACTIVATEAPP, 1, 0)
                user32.PostMessageW(hwnd, WM_SETFOCUS, 0, 0)
            time.sleep(0.4)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
