#!/bin/sh
# MSIKey installer.
#   ./install.sh            install for the current user (needs sudo for the udev rule)
#   ./install.sh --uninstall
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PREFIX="${PREFIX:-$HOME/.local}"
BIN="$PREFIX/bin"
APPS="$PREFIX/share/applications"
RULE=/etc/udev/rules.d/99-msikey.rules
SLEEP_HOOK=/usr/lib/systemd/system-sleep/msikey

sudo_run() {
    if [ "$(id -u)" -eq 0 ]; then sh -c "$1"
    elif command -v sudo >/dev/null; then sudo sh -c "$1"
    elif command -v pkexec >/dev/null; then pkexec sh -c "$1"
    else echo "need root to touch $RULE" >&2; return 1
    fi
}

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$BIN/msikey-gui" "$APPS/io.github.msikey.MSIKey.desktop"
    sudo_run "rm -f $RULE $SLEEP_HOOK && udevadm control --reload" || true
    echo "removed launcher, desktop entry, udev rule and sleep hook (config in ~/.config/msikey kept)"
    exit 0
fi

echo "==> apt dependencies (GTK4 + libadwaita for Python)"
if command -v apt-get >/dev/null; then
    MISSING=""
    for p in python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 pkexec; do
        dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"
    done
    if [ -n "$MISSING" ]; then
        echo "    installing:$MISSING"
        sudo_run "apt-get update && apt-get install -y$MISSING"
    else
        echo "    already present"
    fi

    # msi-keyboard: fallback transport (MSIKey talks to hidraw directly, but
    # this covers odd firmware and non-udev setups). Non-fatal if unavailable.
    if command -v msi-keyboard >/dev/null; then
        echo "==> msi-keyboard fallback: already present"
    else
        echo "==> msi-keyboard fallback: installing"
        sudo_run "apt-get install -y msi-keyboard" \
            || echo "    (not available in your repos - skipping; direct hidraw still works)"
    fi
else
    echo "    no apt-get - install python3-gi / gir1.2-gtk-4.0 / gir1.2-adw-1 yourself"
fi

echo "==> launcher -> $BIN/msikey-gui"
mkdir -p "$BIN"
ln -sf "$HERE/msikey-gui" "$BIN/msikey-gui"

echo "==> desktop entry -> $APPS"
mkdir -p "$APPS"
sed "s|Exec=msikey-gui|Exec=$BIN/msikey-gui|" \
    "$HERE/data/io.github.msikey.MSIKey.desktop" > "$APPS/io.github.msikey.MSIKey.desktop"

echo "==> udev rule -> $RULE"
sudo_run "cp '$HERE/data/99-msikey.rules' $RULE && udevadm control --reload && \
    (udevadm trigger --subsystem-match=hidraw --attr-match=idVendor=1770 --action=add || \
     udevadm trigger --subsystem-match=hidraw)"

echo "==> resume-from-suspend hook -> $SLEEP_HOOK"
sudo_run "cp '$HERE/data/systemd-sleep-msikey' $SLEEP_HOOK && chmod 755 $SLEEP_HOOK"

case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "note: $BIN is not on your PATH" ;;
esac
echo
echo "Done. Launch 'MSIKey' from your menu, or run: msikey-gui"
echo "If lighting still needs root, replug the keyboard or reboot once."
