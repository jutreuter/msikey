#!/usr/bin/env python3
"""Work out what the four 'intensity' codes actually do, and how latching works.

Sends a fixed colour to the LEFT zone at each intensity code, holding each so you
can describe it. Then probes whether the mode packet is needed on every change
or only once.

    python3 tools/intensity-test.py [--color blue] [--hold 3]
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from msikey.device import COLOR_CODES, find_hidraw  # noqa: E402


def _sf(n):  # HIDIOCSFEATURE
    return 0xC0000000 | (n << 16) | (ord("H") << 8) | 0x06


def region(fd, r, color, inten):
    try:
        fcntl.ioctl(fd, _sf(8), bytes([1, 2, 0x42, r, color, inten, 0, 0]))
        return True
    except OSError as e:
        return f"errno {e.errno}"


def mode(fd, m=1):
    try:
        fcntl.ioctl(fd, _sf(8), bytes([1, 2, 0x41, m, 0, 0, 0, 0]))
        return True
    except OSError as e:
        return f"errno {e.errno}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--color", default="blue")
    ap.add_argument("--hold", type=float, default=3.0)
    args = ap.parse_args()
    col = COLOR_CODES.get(args.color, 6)

    path = find_hidraw()
    if not path:
        print("no keyboard", file=sys.stderr)
        return 1
    fd = os.open(path, os.O_RDWR)

    try:
        print(f"Colour: {args.color}. Watch the LEFT zone.\n")

        print("== A. each intensity code, latched once with mode=normal ==")
        for code in (0, 1, 2, 3):
            region(fd, 2, 0, 0)
            region(fd, 3, 0, 0)
            region(fd, 1, col, code)
            mode(fd, 1)
            print(f"  intensity code {code}: describe it (bright? dim? white/pale?)",
                  flush=True)
            time.sleep(args.hold)

        print("\n== B. does a colour change need a mode packet each time? ==")
        region(fd, 1, col, 0)
        mode(fd, 1)
        print("  set code 0, latched. now changing to code 2 WITHOUT a mode packet...",
              flush=True)
        time.sleep(args.hold)
        r = region(fd, 1, col, 2)
        print(f"    sent (result: {r}). did the LEFT zone change? (3s)", flush=True)
        time.sleep(args.hold)
        print("  now sending the mode packet...", flush=True)
        mode(fd, 1)
        print("    did it change now? (3s)", flush=True)
        time.sleep(args.hold)

        print("\n== C. latch with mode=gaming(2) instead of normal(1) ==")
        for code in (0, 2):
            region(fd, 1, col, code)
            m = mode(fd, 2)
            print(f"  code {code} + mode=gaming (result {m}): flash? (3s)", flush=True)
            time.sleep(args.hold)

        print("\n== D. re-latch the SAME state repeatedly (does mode=1 itself flash?) ==")
        region(fd, 1, col, 0)
        mode(fd, 1)
        time.sleep(0.5)
        print("  sending mode=normal 10x over 3s, no colour change...", flush=True)
        for _ in range(10):
            mode(fd, 1)
            time.sleep(0.3)
        print("    did the LEFT zone flash/pulse each time?", flush=True)
    finally:
        for r in (1, 2, 3):
            region(fd, r, 0, 0)
        mode(fd, 1)
        os.close(fd)
    print("\nRestore with:  python3 -m msikey on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
