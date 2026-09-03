#!/usr/bin/env python3
"""Probe an MSI SteelSeries keyboard (1770:ff00) for addressable lighting zones.

The 3-zone protocol also defines region codes for a logo / front lightbar /
mouse on some models. This lights each candidate region on its own so you can
see which ones your hardware actually has.

Run it, watch the keyboard and laptop body, and note what lights up for each
"REGION n" prompt. Nothing here can harm the device - it only sends the same
colour packets the normal app uses.

    python3 tools/probe-zones.py [--hold SECONDS] [--max-region N]
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from msikey.device import find_hidraw  # noqa: E402

# region code -> what the original msi-keyboard project called it
KNOWN = {1: "left (keys)", 2: "middle (keys)", 3: "right (keys)",
         4: "logo?", 5: "front-left?", 6: "front-right?", 7: "mouse?"}

COLOR_WHITE, COLOR_OFF = 8, 0
INT_HIGH = 0


def _ioc_sfeature(length: int) -> int:
    return 0xC0000000 | (length << 16) | (ord("H") << 8) | 0x06


class Proto(OSError):
    """The device NAKed a packet (EPROTO). It also stalls every following
    write until the hidraw fd is reopened."""


def _feature(fd: int, pkt: bytes) -> None:
    try:
        fcntl.ioctl(fd, _ioc_sfeature(len(pkt)), pkt)
    except OSError as e:
        if e.errno == 71:  # EPROTO
            raise Proto(e.errno, "device rejected packet") from None
        raise


def set_region(fd: int, region: int, color: int, intensity: int = INT_HIGH) -> None:
    _feature(fd, bytes([0x01, 0x02, 0x42, region, color, intensity, 0x00, 0x00]))


def set_mode(fd: int, mode: int) -> None:
    _feature(fd, bytes([0x01, 0x02, 0x41, mode, 0, 0, 0, 0]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", type=float, default=4.0)
    ap.add_argument("--max-region", type=int, default=7)
    args = ap.parse_args()

    path = find_hidraw()
    if not path:
        print("No MSI keyboard found (1770:ff00).", file=sys.stderr)
        return 1

    def fresh():
        try:
            return os.open(path, os.O_RDWR)
        except PermissionError:
            print(f"No permission for {path}. Run: msikey-gui setup", file=sys.stderr)
            raise SystemExit(1)

    def paint(fd, lit):
        """Set every real zone off except `lit` (white), then latch."""
        for x in (1, 2, 3):
            set_region(fd, x, COLOR_WHITE if x == lit else COLOR_OFF)
        if lit and lit not in (1, 2, 3):
            set_region(fd, lit, COLOR_WHITE)
        set_mode(fd, 1)

    regions = list(range(1, args.max_region + 1))
    print(f"Probing {path}: regions {regions[0]}-{regions[-1]}, {args.hold}s each.\n"
          "Watch the KEYS, the MSI logo on the lid, and the FRONT EDGE light bar.\n")
    result: dict[int, str] = {}
    for r in regions:
        label = KNOWN.get(r, "unknown")
        fd = fresh()                       # every region starts on a clean fd
        try:
            paint(fd, r)
        except Proto:
            result[r] = "rejected (firmware NAK - not a real zone)"
            print(f"  REGION {r:<2} ({label}): {result[r]}", flush=True)
            os.close(fd)
            continue
        result[r] = "accepted - watch"
        print(f"  REGION {r:<2} ({label}): accepted - LOOK NOW ({args.hold}s)", flush=True)
        time.sleep(args.hold)
        os.close(fd)

    fd = fresh()
    try:
        paint(fd, None)
    except Proto:
        pass
    os.close(fd)

    print("\nSummary:")
    for r in regions:
        print(f"  region {r}: {result.get(r, 'not tested')}")
    print("\nAn 'accepted' region is one the firmware didn't reject - it may "
          "still light nothing. Trust your eyes over this list.")
    print("Restore your lighting with:  python3 -m msikey on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
