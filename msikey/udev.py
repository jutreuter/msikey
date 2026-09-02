"""Set up host access to the keyboard: udev rule + the `msi-keyboard` package.

MSIKey's primary path (writing HID feature reports to /dev/hidrawN) needs no
kernel driver, only the udev rule for permissions. The `msi-keyboard` package is
the fallback transport; we offer to install it too so a fresh machine is covered
either way.
"""

from __future__ import annotations

import os
import shutil
import subprocess

RULE_PATH = "/etc/udev/rules.d/99-msikey.rules"
RULE_TEXT = """\
# MSIKey - MSI SteelSeries RGB keyboard (1770:ff00)
# Grant the active local session read/write access to the HID node so lighting
# can be changed without root.
KERNEL=="hidraw*", ATTRS{idVendor}=="1770", ATTRS{idProduct}=="ff00", TAG+="uaccess", GROUP="plugdev", MODE="0660"
SUBSYSTEM=="usb", ATTR{idVendor}=="1770", ATTR{idProduct}=="ff00", TAG+="uaccess", GROUP="plugdev", MODE="0660"
"""

_RULE_SNIPPET = f"""\
cat > {RULE_PATH} <<'EOF'
{RULE_TEXT}EOF
udevadm control --reload
udevadm trigger --subsystem-match=hidraw --attr-match=idVendor=1770 --action=add \\
    || udevadm trigger --subsystem-match=hidraw
"""

# Add the invoking user to plugdev (bonus; the uaccess tag already covers the
# active session). PKEXEC_UID / SUDO_UID identify the real user under pkexec/sudo.
_PLUGDEV_SNIPPET = """\
_u=$(getent passwd "${PKEXEC_UID:-${SUDO_UID:-}}" | cut -d: -f1)
[ -n "$_u" ] && id -nG "$_u" | tr ' ' '\\n' | grep -qx plugdev || \\
    { [ -n "$_u" ] && usermod -aG plugdev "$_u"; }
"""

_APT_SNIPPET = """\
if ! command -v msi-keyboard >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq && apt-get install -y msi-keyboard || \\
            echo "MSIKEY: could not install msi-keyboard (non-fatal)" >&2
    else
        echo "MSIKEY: no apt-get; install the msi-keyboard package manually" >&2
    fi
fi
"""


def _run_privileged(script: str, use_pkexec: bool) -> None:
    body = "set -e\n" + script
    if os.geteuid() == 0:
        cmd = ["sh", "-c", body]
    elif use_pkexec and shutil.which("pkexec"):
        cmd = ["pkexec", "sh", "-c", body]
    else:
        raise RuntimeError("Need root (pkexec not found). Run: sudo ./install.sh")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip() or "Authorisation cancelled.")


def rule_installed() -> bool:
    try:
        return "1770" in open(RULE_PATH).read()
    except OSError:
        return False


def driver_installed() -> bool:
    """True if the fallback `msi-keyboard` CLI is available."""
    return shutil.which("msi-keyboard") is not None


def setup_complete() -> bool:
    return rule_installed() and driver_installed()


def setup_access(include_driver: bool = True, use_pkexec: bool = True) -> str:
    """Install the udev rule and (optionally) the msi-keyboard package.

    Returns a short human-readable summary of what was done.
    """
    script = _RULE_SNIPPET + _PLUGDEV_SNIPPET
    if include_driver and not driver_installed():
        script += _APT_SNIPPET
    _run_privileged(script, use_pkexec)

    done = ["udev rule installed"]
    done.append("msi-keyboard present" if driver_installed() else "msi-keyboard NOT installed")
    return " · ".join(done) + " — replug the keyboard if lighting still needs a password"
