# MSIKey

A small GTK4 / libadwaita app to manage the backlight on **MSI SteelSeries
3-zone keyboards** (USB `1770:ff00`, as found in MSI GT/GE/GS laptops).

It replaces hand-typed `msi-keyboard` commands and systemd units with a GUI:
pick a colour and brightness per zone, choose an animation, save profiles, and
optionally re-apply your lighting automatically at login.

The UI is forced to a dark theme with an MSI black-and-red accent to match the
laptop's colourway (`Adw.ColorScheme.FORCE_DARK` + a small CSS override in
`msikey/gui.py` — `_MSI_RED` / `_THEME_CSS`).

![zones: left / middle / right · modes: normal, gaming, breathe, demo, wave]

## How it talks to the keyboard

The keyboard takes HID *feature reports* (report id `1`):

| Bytes | Meaning |
|---|---|
| `01 02 42 <region> <colour> <intensity> 00 00` | set one region |
| `01 02 41 <mode> 00 00 00 00` | set the animation mode |

* regions: `left=1 middle=2 right=3`
* colours: `off orange yellow green sky blue purple white red` → `0..8`
* intensity: `high=1 medium=0 low=2 light=3` (see [HARDWARE.md](HARDWARE.md) — not a simple brightness ramp)
* modes: `normal gaming breathe demo wave` → `1..5`

MSIKey writes these straight to `/dev/hidrawN` with `HIDIOCSFEATURE`. That works
next to the in-kernel `gt683r_led` driver and needs **no libusb detach**, so it
avoids the device re-enumeration that the `msi-keyboard` CLI triggers.

If direct access fails it falls back to `pkexec msi-keyboard …`.

## Install

```sh
./install.sh
```

`install.sh` checks for and installs everything the app needs:

* GTK4 / libadwaita GI bindings + polkit (via `apt`, required)
* `msi-keyboard` — the fallback transport (via `apt`, non-fatal if not in your repos)
* a launcher at `~/.local/bin/msikey-gui` and a desktop entry
* the udev rule `/etc/udev/rules.d/99-msikey.rules` + adds you to `plugdev`,
  so the app runs without root

Replug the keyboard (or reboot once) after the first install.

The same checks run inside the app: if the udev rule or `msi-keyboard` is
missing, a **Set up…** button appears on the *Connection* row and does all of
it with one authorisation prompt (`msikey-gui setup` on the CLI).

Run without installing:

```sh
./msikey-gui
```

If you skip `install.sh` (or run on a machine that has neither the udev rule nor
the `msi-keyboard` package), the app shows a **Set up…** button on the
*Connection* row. It asks for authorisation once, installs the udev rule, adds
you to `plugdev`, and `apt`-installs `msi-keyboard` for the fallback path.

## Command line

```
msikey-gui                 # launch the GUI
msikey-gui apply --last     # re-apply the last used settings
msikey-gui apply "RGB split" # apply a saved profile
msikey-gui list             # list saved profiles
msikey-gui off              # backlight off (remembers the colours)
msikey-gui on               # restore the colours from before it was off
msikey-gui setup            # install the udev rule + msi-keyboard pkg (asks for auth)
```

## Files

| Path | |
|---|---|
| `msikey/device.py` | protocol + hidraw transport |
| `msikey/profile.py` | profiles, last-state, login autostart |
| `msikey/udev.py` | udev-rule installer |
| `msikey/gui.py` | GTK4 / libadwaita UI |
| `data/systemd-sleep-msikey` | reapplies the backlight after suspend/hibernate (installed to `/usr/lib/systemd/system-sleep/msikey`) |
| `~/.config/msikey/` | `profiles.json`, `state.json`, `last-lit.json` |

## Requirements

Python 3.10+, `python3-gi`, `gir1.2-gtk-4.0`, `gir1.2-adw-1`, `policykit-1`,
and (for the fallback path) the `msi-keyboard` package.

## Contributing

Issues and pull requests welcome. The protocol tables in `msikey/device.py`
are deliberately kept identical to the `msi-keyboard` CLI; if your MSI model
exposes more zones or colours, that's the place to extend.

## License

MIT — see [LICENSE](LICENSE). Use it, change it, ship it, sell it; just keep
the copyright notice. No warranty: this writes raw HID reports to your
keyboard's firmware.

## Disclaimer

Not affiliated with, endorsed by, or supported by Micro-Star International
(MSI) or SteelSeries. "MSI" and "SteelSeries" are trademarks of their
respective owners; used here only to describe hardware compatibility.
