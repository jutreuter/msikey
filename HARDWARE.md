# Hardware notes

Findings from probing real MSI SteelSeries keyboards (USB `1770:ff00`). PRs with
results from other models welcome.

## Protocol quirks

* **State only latches on a mode packet.** After one or more `42` (set-region)
  packets, the LEDs don't change until a `41` (set-mode) packet is sent. The
  app always sends the three region packets followed by one mode packet.
* **Unknown region codes stall the endpoint.** Sending a `42` packet with a
  region the firmware doesn't have fails with `EPROTO` (errno 71), *and* every
  subsequent feature report on that `hidraw` fd also fails until the fd is
  closed and reopened. `apply_via_hidraw()` opens a fresh fd per call, so a
  stray error self-clears on the next apply.
* **Zone count is fixed in firmware** — there is no way to subdivide a zone or
  address a key individually over this protocol.

## Per-model zone map

Run `tools/probe-zones.py` to test your own.

| Model | Zones that work | Notes |
|---|---|---|
| GT72VR 6RD | 1 = left, 2 = middle, 3 = right (keys only) | Region codes 4–7 rejected (`EPROTO`); the front light bar is not independently addressable |

Region codes from the original `msi-keyboard` project that are **not** present
on the GT72VR: 4 (logo), 5 (front-left), 6 (front-right), 7 (mouse).
