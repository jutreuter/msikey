"""Entry point.

    python -m msikey                 launch the GUI
    python -m msikey apply           apply the last-saved state (headless)
    python -m msikey apply --last    same, explicit
    python -m msikey apply NAME      apply a saved profile by name
    python -m msikey list            list saved profiles
    python -m msikey setup           install the udev rule + msi-keyboard pkg
    python -m msikey off             turn the backlight off (remembers the colours)
    python -m msikey on              restore the colours from before it was off
    python -m msikey effect NAME [--param value ...]   run an animation (Ctrl-C to stop)
        effects: cycle, wave, sweep, breathe, random, pulse
        e.g. msikey effect sweep --colors purple --speed 2
             msikey effect breathe --colors green --style pulse
"""

from __future__ import annotations

import sys
import time

from . import __version__
from .device import REGIONS, KeyboardError, apply_profile
from .profile import load_last_lit, load_profiles, load_state, save_state


def _apply(profile, label: str) -> int:
    try:
        backend = apply_profile(profile)
    except KeyboardError as e:
        print(f"msikey: {e}", file=sys.stderr)
        return 1
    save_state(profile)
    print(f"{label} via {backend}")
    return 0


def _cmd_apply(args: list[str]) -> int:
    profiles = load_profiles()
    if args and args[0] not in ("--last", "-l"):
        name = args[0]
        if name not in profiles:
            print(f"msikey: no profile named {name!r}", file=sys.stderr)
            return 2
        profile = profiles[name]
    else:
        profile = load_state()
    return _apply(profile, f"applied {profile.name!r}")


def _cmd_off() -> int:
    profile = load_state().copy()          # keep current zones' brightness
    profile.name = "Off"
    for z in profile.zones.values():
        z.color = "off"
    return _apply(profile, "backlight off")


def _cmd_on() -> int:
    return _apply(load_last_lit().copy(), "backlight restored")


def _cmd_effect(args: list[str]) -> int:
    from . import effects

    if not args or args[0] in ("-h", "--help"):
        print("effects: " + ", ".join(effects.BUILTINS))
        return 0 if args else 2
    name = args[0]
    params: dict = {}
    rate = effects.DEFAULT_HZ
    it = iter(args[1:])
    for a in it:
        if not a.startswith("--"):
            print(f"msikey: unexpected argument {a!r}", file=sys.stderr)
            return 2
        key, val = a[2:], next(it, "")
        if key == "rate":
            rate = float(val)
            continue
        try:
            val = int(val) if val.lstrip("-").isdigit() else float(val)
        except ValueError:
            pass
        params[key] = val

    try:
        eff = effects.make_effect(name, **params)
    except (ValueError, TypeError) as e:
        print(f"msikey: {e}", file=sys.stderr)
        return 2

    import signal

    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("now", True))

    eng = effects.Engine(rate_hz=rate, on_status=lambda m: print(f"  [{m}]"))
    eng.start(eff)
    print(f"running {name} at {eng.rate:g} Hz - Ctrl-C to stop")
    try:
        while eng.running and not stop["now"]:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        eng.stop()
        drop = f"{100 * eng.dropped / eng.frames:.0f}%" if eng.frames else "n/a"
        print(f"\nstopped ({eng.frames} frames, {drop} dropped); restoring")
        try:
            apply_profile(load_last_lit())
        except KeyboardError:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv and argv[0] in ("-v", "--version"):
        print(f"msikey {__version__}")
        return 0

    if not argv or argv[0] == "gui":
        from .gui import run_gui

        return run_gui()

    cmd, rest = argv[0], argv[1:]
    if cmd == "apply":
        return _cmd_apply(rest)
    if cmd == "off":
        return _cmd_off()
    if cmd == "on":
        return _cmd_on()
    if cmd == "effect":
        return _cmd_effect(rest)
    if cmd == "list":
        for name, p in load_profiles().items():
            zones = " ".join(f"{r}:{p.zones[r].color}" for r in REGIONS)
            print(f"{name:24}  mode={p.mode:8}  {zones}")
        return 0
    if cmd in ("setup", "install-udev"):
        from .udev import setup_access

        try:
            summary = setup_access(include_driver=(cmd == "setup"))
        except RuntimeError as e:
            print(f"msikey: {e}", file=sys.stderr)
            return 1
        print(summary)
        return 0

    print(f"msikey: unknown command {cmd!r} (try --help)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
