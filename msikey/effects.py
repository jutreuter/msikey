"""Host-driven animation engine for the 3-zone keyboard.

The firmware only holds a static state, so animations are produced here: a
background thread ticks at a fixed rate, asks the active effect for the frame at
the current time, and sends only the zones that changed (plus a mode packet to
latch them).

Hardware limits found by probing (see HARDWARE.md):

* 3 zones, 8 palette colours, 4 brightness steps - effects work in that space.
* ~15 Hz is the highest rate that still looks smooth on a GT72VR.
* The firmware randomly NAKs (EPROTO) 5-15 % of writes at any rate; a dropped
  frame is skipped, and the fd is reopened only after several in a row.
"""

from __future__ import annotations

import fcntl
import math
import os
import random
import threading
import time
from dataclasses import dataclass, field

from .device import (
    COLORS,
    INTENSITIES,
    MODE_CODES,
    REGION_CODES,
    REGIONS,
    Profile,
    ZoneState,
    find_hidraw,
)

CAP_HZ = 15.0                      # validated smooth ceiling
DEFAULT_HZ = 12.0
LIT_PALETTE = COLORS[1:]           # everything except "off"

# The four "intensity" codes are NOT a brightness ramp. Only two are pure
# colour: "high" (full) and "medium" (dim). "low" and "light" both mix in bright
# white - a pale flash, not a dim step. Fades use just these two, then "off".
DIM_RAMP = ("high", "medium")
FROST = ("low", "light")            # colour + white, for effects that want it


# --------------------------------------------------------------------------- #
# frame + effects
# --------------------------------------------------------------------------- #
@dataclass
class Frame:
    zones: dict[str, ZoneState]
    mode: str = "normal"

    @classmethod
    def solid(cls, color: str, intensity: str = "high", mode: str = "normal") -> "Frame":
        return cls({r: ZoneState(color, intensity) for r in REGIONS}, mode)


class Effect:
    """Base class. ``name`` and ``params`` are metadata; ``frame(t)`` is the work."""

    name = "effect"

    def __init__(self, **params):
        self.params = params

    def frame(self, t: float) -> Frame:  # t = seconds since the effect started
        raise NotImplementedError


class Static(Effect):
    name = "static"

    def __init__(self, profile: Profile):
        super().__init__()
        self._frame = Frame({r: ZoneState(profile.zones[r].color,
                                          profile.zones[r].intensity)
                             for r in REGIONS}, profile.mode)

    def frame(self, t: float) -> Frame:
        return self._frame


class Cycle(Effect):
    """All zones step through the palette together."""

    name = "cycle"

    def __init__(self, speed: float = 1.5, intensity: str = "high",
                 palette: list[str] | None = None):
        super().__init__(speed=speed, intensity=intensity)
        self.speed = speed
        self.intensity = intensity
        self.palette = palette or LIT_PALETTE

    def frame(self, t: float) -> Frame:
        c = self.palette[int(t * self.speed) % len(self.palette)]
        return Frame.solid(c, self.intensity)


class Wave(Effect):
    """Palette scrolls across the three zones."""

    name = "wave"

    def __init__(self, speed: float = 1.5, spread: int = 2, intensity: str = "high",
                 palette: list[str] | None = None):
        super().__init__(speed=speed, spread=spread, intensity=intensity)
        self.speed = speed
        self.spread = spread
        self.intensity = intensity
        self.palette = palette or LIT_PALETTE

    def frame(self, t: float) -> Frame:
        base = t * self.speed
        n = len(self.palette)
        zones = {
            r: ZoneState(self.palette[int(base + i * self.spread) % n], self.intensity)
            for i, r in enumerate(REGIONS)
        }
        return Frame(zones)


class Breathe(Effect):
    """Fixed colours that swell and fade (host-side, any colour/speed).

    Only two pure-colour levels exist and ``medium`` also shifts hue, so:

    * ``style='swell'`` (default) - off -> medium -> high -> medium -> off, a
      soft swell that picks up a hue shift at the dim end (green reads yellow).
    * ``style='pulse'`` - off <-> high only: true colour, harder edges.
    * ``floor='medium'`` - never go fully dark (swell style only).
    """

    name = "breathe"

    def __init__(self, colors: dict[str, str] | str = "purple", period: float = 4.0,
                 style: str = "swell", floor: str = "off"):
        super().__init__(period=period, style=style, floor=floor)
        if isinstance(colors, str):
            colors = {r: colors for r in REGIONS}
        self.colors = colors
        self.period = max(0.5, period)
        if style == "pulse":
            self.ramp = ("off", "high")
        elif floor == "medium":
            self.ramp = ("medium", "high")
        else:
            self.ramp = (floor, "medium", "high")

    def frame(self, t: float) -> Frame:
        phase = (t % self.period) / self.period
        tri = 1.0 - abs(1.0 - 2.0 * phase)          # 0 at ends, 1 in the middle
        level = self.ramp[round(tri * (len(self.ramp) - 1))]
        if level == "off":
            return Frame({r: ZoneState("off", "high") for r in REGIONS})
        return Frame({r: ZoneState(self.colors.get(r, "purple"), level)
                      for r in REGIONS})


class Random(Effect):
    """One zone re-rolls to a random palette colour each step (round-robin), so
    only a single zone changes per frame - much lighter on the firmware than
    reshuffling all three."""

    name = "random"

    def __init__(self, period: float = 0.5, intensity: str = "high",
                 palette: list[str] | None = None):
        super().__init__(period=period, intensity=intensity)
        self.period = max(0.1, period)
        self.intensity = intensity
        self.palette = palette or LIT_PALETTE

    def frame(self, t: float) -> Frame:
        step = int(t / self.period)
        zones = {}
        for i, r in enumerate(REGIONS):
            last = step - ((step - i) % len(REGIONS))     # newest step that hit zone i
            rng = random.Random(last * len(REGIONS) + i)
            zones[r] = ZoneState(rng.choice(self.palette), self.intensity)
        return Frame(zones)


class Pulse(Effect):
    """A single lit zone bounces left-right-left."""

    name = "pulse"

    def __init__(self, speed: float = 2.0, color: str = "red", intensity: str = "high",
                 bg: str = "off"):
        super().__init__(speed=speed, color=color)
        self.speed = speed
        self.color = color
        self.intensity = intensity
        self.bg = bg

    def frame(self, t: float) -> Frame:
        pos = (math.sin(t * self.speed) + 1) / 2          # 0..1
        lit = min(2, int(pos * 3))
        return Frame({r: ZoneState(self.color if i == lit else self.bg, self.intensity)
                      for i, r in enumerate(REGIONS)})


class Sweep(Effect):
    """A colour that travels across the zones. Each zone steps down
    high -> medium -> off while the next zone steps back up, the two crossing
    for one step where both sit at 'medium' (the overlap). Only pure-colour
    levels - no white 'frost' flash.
    """

    name = "sweep"

    def __init__(self, speed: float = 3.0, colors: dict[str, str] | str = "sky",
                 bounce: bool = False):
        super().__init__(speed=speed, bounce=bool(bounce))
        self.step_dur = 1.0 / max(0.5, speed)             # seconds per step
        if isinstance(colors, str):
            colors = {r: colors for r in REGIONS}
        self.colors = colors
        order = list(range(len(REGIONS)))
        if bounce and len(REGIONS) > 2:
            order = order + order[-2:0:-1]                # 0,1,2,1 -> loops smoothly
        self._steps = self._build(order)

    def _build(self, order: list[int]) -> list[dict[str, ZoneState]]:
        steps: list[dict[str, ZoneState]] = []

        def state(**lit: str) -> dict[str, ZoneState]:
            out = {r: ZoneState("off", "high") for r in REGIONS}
            for r, lvl in lit.items():
                out[r] = ZoneState(self.colors.get(r, "sky"), lvl)
            return out

        for k in range(len(order)):
            cur = REGIONS[order[k]]
            nxt = REGIONS[order[(k + 1) % len(order)]]
            steps.append(state(**{cur: "high"}))                    # cur full, nxt off
            steps.append(state(**{cur: "medium", nxt: "medium"}))   # dim crossfade
        return steps

    def frame(self, t: float) -> Frame:
        return Frame(dict(self._steps[int(t / self.step_dur) % len(self._steps)]))


@dataclass
class Keyframe:
    zones: dict[str, ZoneState]
    hold: float = 0.5                                     # seconds
    mode: str = "normal"


class Sequence(Effect):
    """Loop a hand-built list of keyframes - the 'custom animation' editor's output."""

    name = "sequence"

    def __init__(self, keyframes: list[Keyframe], loop: bool = True):
        super().__init__()
        self.keyframes = keyframes or [Keyframe({r: ZoneState("off") for r in REGIONS})]
        self.loop = loop
        self.total = sum(max(0.05, k.hold) for k in self.keyframes)

    def frame(self, t: float) -> Frame:
        if self.loop:
            t = t % self.total
        acc = 0.0
        for k in self.keyframes:
            acc += max(0.05, k.hold)
            if t < acc:
                return Frame(dict(k.zones), k.mode)
        last = self.keyframes[-1]
        return Frame(dict(last.zones), last.mode)

    def to_dict(self) -> dict:
        return {"loop": self.loop, "keyframes": [
            {"hold": k.hold, "mode": k.mode,
             "zones": {r: {"color": z.color, "intensity": z.intensity}
                       for r, z in k.zones.items()}}
            for k in self.keyframes]}

    @classmethod
    def from_dict(cls, d: dict) -> "Sequence":
        kfs = []
        for kd in d.get("keyframes", []):
            zd = kd.get("zones", {})
            zones = {r: ZoneState(zd.get(r, {}).get("color", "off"),
                                  zd.get(r, {}).get("intensity", "high"))
                     for r in REGIONS}
            kfs.append(Keyframe(zones, float(kd.get("hold", 0.5)),
                                kd.get("mode", "normal")))
        return cls(kfs, bool(d.get("loop", True)))


BUILTINS = {
    "cycle": Cycle,
    "wave": Wave,
    "sweep": Sweep,
    "breathe": Breathe,
    "random": Random,
    "pulse": Pulse,
}


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #
def _sfeature(length: int) -> int:
    return 0xC0000000 | (length << 16) | (ord("H") << 8) | 0x06


class Engine:
    """Runs one effect at a time on a background thread."""

    def __init__(self, rate_hz: float = DEFAULT_HZ,
                 on_status=None):
        self.rate = min(CAP_HZ, max(1.0, rate_hz))
        self._effect: Effect | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._on_status = on_status         # callable(str) for GUI/logging
        self.dropped = 0
        self.frames = 0

    # -- lifecycle ----------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, effect: Effect) -> None:
        self.set_effect(effect)
        if self.running:
            return
        self._stop.clear()
        self.dropped = self.frames = 0
        self._thread = threading.Thread(target=self._run, name="msikey-fx", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        t = self._thread
        if t:
            t.join(timeout)
        self._thread = None

    def set_effect(self, effect: Effect) -> None:
        with self._lock:
            self._effect = effect
            self._t0 = time.perf_counter()

    # -- worker -----------------------------------------------------------
    def _open(self) -> int | None:
        path = find_hidraw()
        if not path:
            return None
        try:
            return os.open(path, os.O_RDWR)
        except OSError:
            return None

    def _run(self) -> None:
        fd = self._open()
        if fd is None:
            self._status("keyboard not accessible")
            return
        period = 1.0 / self.rate
        last: dict[str, ZoneState] = {}
        last_mode: str | None = None
        consec_fail = 0
        next_t = time.perf_counter()
        try:
            while not self._stop.is_set():
                with self._lock:
                    eff = self._effect
                    t = time.perf_counter() - self._t0
                if eff is not None:
                    try:
                        frame = eff.frame(t)
                        ok, consec_fail, last_mode = self._send(
                            fd, frame, last, last_mode, consec_fail)
                    except Exception as e:  # a broken effect must not kill the loop
                        self._status(f"effect error: {e}")
                        ok = False
                    self.frames += 1
                    if not ok:
                        self.dropped += 1
                    if consec_fail >= 5:
                        os.close(fd)
                        fd = self._open() or fd
                        last, last_mode, consec_fail = {}, None, 0
                        self._status("reconnected")
                next_t += period
                sleep = next_t - time.perf_counter()
                if sleep > 0:
                    self._stop.wait(sleep)
                else:
                    next_t = time.perf_counter()
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _send(self, fd, frame, last, last_mode, consec_fail):
        changed = [r for r in REGIONS
                   if last.get(r) != frame.zones[r]]
        ok = True
        for r in changed:
            z = frame.zones[r]
            pkt = bytes([0x01, 0x02, 0x42, REGION_CODES[r],
                         COLORS.index(z.color), INTENSITIES.index(z.intensity), 0, 0])
            if self._ioctl(fd, pkt):
                last[r] = z
            else:
                ok = False
        if changed or frame.mode != last_mode:
            if self._ioctl(fd, bytes([0x01, 0x02, 0x41,
                                      MODE_CODES[frame.mode], 0, 0, 0, 0])):
                last_mode = frame.mode
            else:
                ok = False
        consec_fail = 0 if ok else consec_fail + 1
        return ok, consec_fail, last_mode

    @staticmethod
    def _ioctl(fd, pkt) -> bool:
        try:
            fcntl.ioctl(fd, _sfeature(len(pkt)), pkt)
            return True
        except OSError:
            return False

    def _status(self, msg: str) -> None:
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass


def make_effect(name: str, **params) -> Effect:
    if name not in BUILTINS:
        raise ValueError(f"unknown effect {name!r}; try {', '.join(BUILTINS)}")
    return BUILTINS[name](**params)
