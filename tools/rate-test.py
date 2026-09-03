#!/usr/bin/env python3
"""Find the safe update rate for host-driven animations on 1770:ff00.

Steps a zone through the palette at a series of target frame rates, holding each
for a few seconds. Reports the rate actually achieved and any errors; you watch
for flicker, tearing between zones, or the firmware lagging behind.

    python3 tools/rate-test.py [--seconds N] [--rates 2,5,10,15,20,30,45]
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from msikey.device import find_hidraw  # noqa: E402

PALETTE = [1, 2, 3, 4, 5, 6, 7, 8]  # red..white
INT_HIGH = 0


def _ioc_sfeature(length: int) -> int:
    return 0xC0000000 | (length << 16) | (ord("H") << 8) | 0x06


def _feature(fd: int, pkt: bytes) -> None:
    fcntl.ioctl(fd, _ioc_sfeature(len(pkt)), pkt)


def region_pkt(region: int, color: int) -> bytes:
    return bytes([0x01, 0x02, 0x42, region, color, INT_HIGH, 0x00, 0x00])


def mode_pkt(mode: int = 1) -> bytes:
    return bytes([0x01, 0x02, 0x41, mode, 0, 0, 0, 0])


def _try(fd: int, pkt: bytes, retries: int, errno_hist: dict) -> bool:
    for attempt in range(retries + 1):
        try:
            _feature(fd, pkt)
            return True
        except OSError as e:
            errno_hist[e.errno] = errno_hist.get(e.errno, 0) + 1
            if attempt < retries:
                time.sleep(0.001)
    return False


def run_rate(fd: int, hz: float, seconds: float, full_frame: bool,
             retries: int, errno_hist: dict) -> dict:
    period = 1.0 / hz
    frames = bad = 0
    worst_lag = 0.0
    t_end = time.perf_counter() + seconds
    next_t = time.perf_counter()
    i = 0
    while time.perf_counter() < t_end:
        ok = True
        if full_frame:
            for r in (1, 2, 3):
                ok &= _try(fd, region_pkt(r, PALETTE[(i + r) % len(PALETTE)]),
                           retries, errno_hist)
        else:
            ok &= _try(fd, region_pkt(1, PALETTE[i % len(PALETTE)]), retries, errno_hist)
        ok &= _try(fd, mode_pkt(), retries, errno_hist)
        frames += 1
        bad += (not ok)
        i += 1
        next_t += period
        lag = time.perf_counter() - next_t
        worst_lag = max(worst_lag, lag)
        if lag < 0:
            time.sleep(-lag)
        else:
            next_t = time.perf_counter()
    return {"target": hz, "actual": round(frames / seconds, 1), "frames": frames,
            "bad_frames": bad, "worst_lag_ms": round(worst_lag * 1000)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--rates", default="5,10,15,20,30")
    ap.add_argument("--full-frame", action="store_true",
                    help="rewrite all 3 zones per frame (default: 1 zone + latch)")
    ap.add_argument("--retries", type=int, default=0,
                    help="resend a packet up to N times on error")
    args = ap.parse_args()
    rates = [float(x) for x in args.rates.split(",")]

    path = find_hidraw()
    if not path:
        print("No MSI keyboard found.", file=sys.stderr)
        return 1
    fd = os.open(path, os.O_RDWR)

    mode = "3 zones + latch" if args.full_frame else "1 zone + latch"
    print(f"{path} - {args.seconds}s per rate, {mode} per frame, retries={args.retries}.")
    print("Watch for: flicker, zones changing out of sync, visible lag.\n")
    print(f"{'target Hz':>9} {'actual Hz':>10} {'frames':>7} {'bad frm':>8} {'worst lag':>11}")
    errno_hist: dict = {}
    try:
        for hz in rates:
            time.sleep(0.3)
            s = run_rate(fd, hz, args.seconds, args.full_frame, args.retries, errno_hist)
            print(f"{s['target']:>9} {s['actual']:>10} {s['frames']:>7} "
                  f"{s['bad_frames']:>8} {s['worst_lag_ms']:>8} ms", flush=True)
    finally:
        # leave zones static
        for r in (1, 2, 3):
            try:
                _feature(fd, region_pkt(r, 0))
            except OSError:
                pass
        try:
            _feature(fd, mode_pkt())
        except OSError:
            pass
        os.close(fd)
    import errno as _e
    if errno_hist:
        names = {v: k for k, v in vars(_e).items() if isinstance(v, int)}
        breakdown = ", ".join(f"{names.get(k, k)}({k})={n}" for k, n in sorted(errno_hist.items()))
        print(f"\nerror breakdown: {breakdown}")
    else:
        print("\nno errors")
    print("Pick the highest rate that still looked smooth with 0 bad frames.")
    print("Restore with:  python3 -m msikey on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
