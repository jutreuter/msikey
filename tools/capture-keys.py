#!/usr/bin/env python3
"""Watch every input + hidraw device (and the LED sysfs) at once, so one run
captures what the Fn brightness keys actually do.

Needs root for /dev/input and /dev/hidraw:

    sudo python3 tools/capture-keys.py

Then press Fn + the keyboard-brightness keys a few times and watch the output.
Ctrl-C to stop.
"""

from __future__ import annotations

import glob
import os
import select
import struct
import sys
import time

EV = struct.Struct("qqHHi")  # input_event on 64-bit: timeval(2q) type code value

EV_TYPES = {0: "SYN", 1: "KEY", 4: "MSC", 17: "LED", 5: "SW"}
KEYS = {
    224: "BRIGHTNESSDOWN", 225: "BRIGHTNESSUP",
    228: "KBDILLUMTOGGLE", 229: "KBDILLUMDOWN", 230: "KBDILLUMUP",
    0xE045: "?", 431: "KBDILLUM_ADJUST?",
}


def dev_name(path: str) -> str:
    try:
        num = path.rsplit("event", 1)[1]
        return open(f"/sys/class/input/event{num}/device/name").read().strip()
    except OSError:
        return "?"


def leds() -> dict[str, str]:
    out = {}
    for d in glob.glob("/sys/class/leds/*"):
        try:
            out[os.path.basename(d)] = open(os.path.join(d, "brightness")).read().strip()
        except OSError:
            pass
    return out


def main() -> int:
    fds: dict[int, str] = {}
    for path in sorted(glob.glob("/dev/input/event*")) + sorted(glob.glob("/dev/hidraw*")):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            print(f"  (skip {path}: {e})")
            continue
        label = path
        if "event" in path:
            label = f"{path} [{dev_name(path)}]"
        fds[fd] = label
    if not fds:
        print("no devices opened - run with sudo", file=sys.stderr)
        return 1

    print(f"watching {len(fds)} devices + {len(leds())} LEDs. press the keys now. Ctrl-C to stop.\n")
    last_leds = leds()
    t0 = time.time()
    try:
        while True:
            r, _, _ = select.select(list(fds), [], [], 0.25)
            for fd in r:
                data = os.read(fd, 4096)
                label = fds[fd]
                if "hidraw" in label:
                    print(f"{time.time()-t0:7.2f} {label}: {data.hex(' ')}")
                    continue
                for i in range(0, len(data) - EV.size + 1, EV.size):
                    _s, _us, etype, code, value = EV.unpack_from(data, i)
                    if etype == 0:  # SYN, skip noise
                        continue
                    tn = EV_TYPES.get(etype, str(etype))
                    kn = KEYS.get(code, "")
                    print(f"{time.time()-t0:7.2f} {label}: {tn} code={code}"
                          f"{' ('+kn+')' if kn else ''} value={value}")
            now = leds()
            for k, v in now.items():
                if last_leds.get(k) != v:
                    print(f"{time.time()-t0:7.2f} LED {k}: {last_leds.get(k)} -> {v}")
            last_leds = now
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        for fd in fds:
            os.close(fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
