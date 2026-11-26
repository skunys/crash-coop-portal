#!/usr/bin/env python3
"""Bring the game window to the foreground (the game ignores input otherwise).

    python focus-game.py            # steal focus, print the result
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
PREFIX = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8")).get(
    "window_title", "crash_mom")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def find_hwnd() -> int | None:
    hits: list[int] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.lower().startswith(PREFIX.lower()):
                hits.append(int(hwnd))
                return False
        return True

    user32.EnumWindows(proc(visit), 0)
    return hits[0] if hits else None


def foreground(hwnd: int) -> bool:
    """SetForegroundWindow only works for the thread that owns the foreground
    window; attach to that thread's input queue first, which is allowed."""
    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return True
    this_thread = kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    if fg_thread and fg_thread != this_thread:
        attached = bool(user32.AttachThreadInput(fg_thread, this_thread, True))
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
        user32.SetActiveWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(fg_thread, this_thread, False)
    time.sleep(0.3)
    return user32.GetForegroundWindow() == hwnd


def main() -> int:
    hwnd = find_hwnd()
    if not hwnd:
        print("game window not found")
        return 1
    ok = foreground(hwnd)
    print(f"hwnd={hwnd} foreground={user32.GetForegroundWindow()} ok={ok} "
          f"active={user32.GetActiveWindow() == hwnd} "
          f"minimized={bool(user32.IsIconic(hwnd))}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
