#!/usr/bin/env python3
"""Save one frame of the game window (the same window the stream captures).

    python shot.py logs/now.png
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
PREFIX = CFG.get("window_title", "crash_mom")


def find_hwnd() -> int | None:
    user32 = ctypes.windll.user32
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


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "logs", "shot.png")
    hwnd = find_hwnd()
    if not hwnd:
        print("game window not found")
        return 1
    user32 = ctypes.windll.user32
    fg = user32.GetForegroundWindow()
    print(f"hwnd={hwnd} foreground={fg} is_foreground={fg == hwnd} "
          f"minimized={bool(user32.IsIconic(hwnd))}")
    subprocess.run([CFG["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "gdigrab", "-framerate", "2", "-i", f"hwnd={hwnd}",
                    "-frames:v", "1", out], check=True)
    print("saved", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
