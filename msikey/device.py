"""Low-level driver for the MSI SteelSeries 3-zone RGB keyboard (USB 1770:ff00).

The keyboard is controlled with HID *feature reports* (report id 1):

    01 02 42 <region> <color> <intensity> 00 00      set one region's colour
    01 02 41 <mode>    00      00          00 00      set the animation mode

We send these straight to /dev/hidrawN with the HIDIOCSFEATURE ioctl, which
works alongside the in-kernel ``gt683r_led`` driver and needs no libusb detach
(unlike the ``msi-keyboard`` CLI, which is kept only as a fallback path).
"""

from __future__ import annotations

import fcntl
import glob
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field

VENDOR_ID = 0x1770
PRODUCT_ID = 0xFF00

# --- protocol tables (match the `msi-keyboard` CLI so behaviour is identical) ---
COLORS = ["off", "red", "orange", "yellow", "green", "sky", "blue", "purple", "white"]
COLOR_CODES = {name: i for i, name in enumerate(COLORS)}

INTENSITIES = ["high", "medium", "low", "light"]
INTENSITY_CODES = {name: i for i, name in enumerate(INTENSITIES)}

REGIONS = ["left", "middle", "right"]
REGION_CODES = {"left": 1, "middle": 2, "right": 3}

MODES = ["normal", "gaming", "breathe", "demo", "wave"]
MODE_CODES = {name: i + 1 for i, name in enumerate(MODES)}

# Approximate on-screen RGB for each hardware colour, used for swatches only.
COLOR_RGB = {
    "off": (0x24, 0x24, 0x28),
    "red": (0xFF, 0x1E, 0x1E),
    "orange": (0xFF, 0x7A, 0x00),
    "yellow": (0xFF, 0xD5, 0x00),
    "green": (0x22, 0xC5, 0x5E),
    "sky": (0x38, 0xBD, 0xF8),
    "blue": (0x25, 0x63, 0xEB),
    "purple": (0xA2, 0x1C, 0xAF),
    "white": (0xF5, 0xF5, 0xF5),
}
INTENSITY_SCALE = {"high": 1.0, "medium": 0.66, "low": 0.40, "light": 0.20}


class KeyboardError(Exception):
    """Any failure talking to the keyboard, with a human-readable message."""


# --------------------------------------------------------------------------- #
# state model
# --------------------------------------------------------------------------- #
@dataclass
class ZoneState:
    color: str = "off"
    intensity: str = "high"

    def validated(self) -> "ZoneState":
        if self.color not in COLOR_CODES:
            self.color = "off"
        if self.intensity not in INTENSITY_CODES:
            self.intensity = "high"
        return self


@dataclass
class Profile:
    name: str = "Default"
    mode: str = "normal"
    zones: dict = field(default_factory=lambda: {r: ZoneState() for r in REGIONS})

    def validated(self) -> "Profile":
        if self.mode not in MODE_CODES:
            self.mode = "normal"
        for r in REGIONS:
            self.zones.setdefault(r, ZoneState()).validated()
        return self

    def is_lit(self) -> bool:
        """True if at least one zone is set to something other than 'off'."""
        return any(z.color != "off" for z in self.zones.values())

    def copy(self) -> "Profile":
        return Profile.from_dict(self.to_dict())

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "mode": self.mode,
            "zones": {r: asdict(z) for r, z in self.zones.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        p = cls(name=str(d.get("name", "Default")), mode=str(d.get("mode", "normal")))
        zones = d.get("zones", {}) or {}
        for r in REGIONS:
            zd = zones.get(r, {}) or {}
            p.zones[r] = ZoneState(
                color=str(zd.get("color", "off")),
                intensity=str(zd.get("intensity", "high")),
            )
        return p.validated()

    def cli_args(self) -> list[str]:
        args = ["-m", self.mode]
        for r in REGIONS:
            z = self.zones[r]
            args += ["-c", f"{r},{z.color},{z.intensity}"]
        return args


# --------------------------------------------------------------------------- #
# hidraw transport
# --------------------------------------------------------------------------- #
def _hidiocsfeature(length: int) -> int:
    # _IOC(_IOC_WRITE|_IOC_READ, 'H', 0x06, length)
    return 0xC0000000 | (length << 16) | (ord("H") << 8) | 0x06


def find_hidraw() -> str | None:
    """Return the /dev/hidrawN path for the MSI keyboard, or None."""
    needle = f"HID_ID=0003:{VENDOR_ID:08X}:{PRODUCT_ID:08X}"
    for sysdir in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            uevent = open(os.path.join(sysdir, "device", "uevent")).read()
        except OSError:
            continue
        if needle in uevent.upper():
            return "/dev/" + os.path.basename(sysdir)
    return None


def _packets(profile: Profile) -> list[bytes]:
    out = []
    for r in REGIONS:
        z = profile.zones[r]
        out.append(
            bytes(
                [
                    0x01,
                    0x02,
                    0x42,
                    REGION_CODES[r],
                    COLOR_CODES[z.color],
                    INTENSITY_CODES[z.intensity],
                    0x00,
                    0x00,
                ]
            )
        )
    out.append(bytes([0x01, 0x02, 0x41, MODE_CODES[profile.mode], 0, 0, 0, 0]))
    return out


def _hidraw_once(profile: Profile) -> None:
    path = find_hidraw()
    if not path:
        raise KeyboardError("MSI keyboard not found (no matching /dev/hidraw device).")
    try:
        fd = os.open(path, os.O_RDWR)
    except PermissionError as e:
        raise KeyboardError(
            f"No permission to open {path}. Click “Set up…” "
            "(or run: msikey-gui setup)."
        ) from e
    except OSError as e:
        raise KeyboardError(f"Cannot open {path}: {e}") from e
    try:
        for pkt in _packets(profile.validated()):
            fcntl.ioctl(fd, _hidiocsfeature(len(pkt)), pkt)
    except OSError as e:
        raise KeyboardError(f"Failed to send HID feature report: {e}") from e
    finally:
        os.close(fd)


def apply_via_hidraw(profile: Profile, retries: int = 4) -> None:
    """Send the profile over hidraw, retrying briefly.

    The in-kernel ``gt683r_led`` driver re-enumerates the device (new
    ``/dev/hidrawN`` node) whenever a libusb tool like ``msi-keyboard`` runs, so
    a first attempt can hit a disappearing node. Permission errors are fatal and
    are not retried.
    """
    last: KeyboardError | None = None
    for attempt in range(retries):
        try:
            _hidraw_once(profile)
            return
        except KeyboardError as e:
            last = e
            if "No permission" in str(e):
                raise
            time.sleep(0.15 * (attempt + 1))
    assert last is not None
    raise last


def apply_via_cli(profile: Profile, allow_pkexec: bool = True) -> None:
    exe = shutil.which("msi-keyboard")
    if not exe:
        raise KeyboardError("`msi-keyboard` CLI not installed for the fallback path.")
    cmd: list[str] = []
    if os.geteuid() != 0:
        has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        pk = shutil.which("pkexec") if allow_pkexec else None
        if not pk or not has_display:
            raise KeyboardError(
                "No direct access to the keyboard and cannot ask for authorisation "
                "here. Run: msikey-gui setup"
            )
        cmd.append(pk)
    cmd += [exe, *profile.validated().cli_args()]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise KeyboardError(f"Running msi-keyboard failed: {e}") from e
    if res.returncode != 0:
        msg = (res.stderr or res.stdout).strip()
        raise KeyboardError(msg or f"msi-keyboard exited with code {res.returncode}.")


def apply_profile(profile: Profile, prefer: str = "auto") -> str:
    """Apply *profile* to the keyboard. Returns the backend used ('hidraw'|'cli')."""
    errors: list[str] = []
    if prefer in ("auto", "hidraw"):
        try:
            apply_via_hidraw(profile)
            return "hidraw"
        except KeyboardError as e:
            if prefer == "hidraw":
                raise
            errors.append(str(e))
    try:
        apply_via_cli(profile)
        return "cli"
    except KeyboardError as e:
        errors.append(str(e))
    raise KeyboardError(" ".join(errors))


def device_present() -> bool:
    return find_hidraw() is not None or (
        os.path.exists("/dev/bus/usb") and _usb_present()
    )


def _usb_present() -> bool:
    for d in glob.glob("/sys/bus/usb/devices/*/idVendor"):
        try:
            if open(d).read().strip() == f"{VENDOR_ID:04x}":
                pid = open(os.path.join(os.path.dirname(d), "idProduct")).read().strip()
                if pid == f"{PRODUCT_ID:04x}":
                    return True
        except OSError:
            continue
    return False
