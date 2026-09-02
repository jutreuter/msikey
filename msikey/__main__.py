"""Entry point.

    python -m msikey                 launch the GUI
    python -m msikey apply           apply the last-saved state (headless)
    python -m msikey apply --last    same, explicit
    python -m msikey apply NAME      apply a saved profile by name
    python -m msikey list            list saved profiles
    python -m msikey setup           install the udev rule + msi-keyboard pkg
    python -m msikey off             turn the backlight off (remembers the colours)
    python -m msikey on              restore the colours from before it was off
"""

from __future__ import annotations

import sys

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
