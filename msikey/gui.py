"""GTK4 / libadwaita front-end for the MSI keyboard."""

from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, __version__, effects
from .device import (
    COLOR_RGB,
    COLORS,
    INTENSITIES,
    KeyboardError,
    MODES,
    REGIONS,
    Profile,
    ZoneState,
    apply_profile,
    find_hidraw,
)
from .profile import (
    autostart_enabled,
    load_last_lit,
    load_profiles,
    load_sequences,
    load_state,
    save_profiles,
    save_sequences,
    save_state,
    set_autostart,
)
from .udev import driver_installed, rule_installed, setup_access

_UNSAVED = "Custom (unsaved)"

# Display labels for the intensity dropdown. The hardware codes are a 2x2
# grid (pure colour / colour+white) x (high / low brightness), not a ramp -
# see HARDWARE.md - so the labels spell that out instead of using the bare
# high/medium/low/light names, which read as four brightness steps.
INTENSITY_LABELS = {
    "high": "High",
    "medium": "Low",
    "low": "Low + White",
    "light": "High + White",
}


# MSI "black & red" gaming colourway.
_MSI_RED = "#d81f26"
_MSI_RED_HOVER = "#e63a40"
_MSI_RED_LIGHT = "#ff7a7f"

_THEME_CSS = f"""
@define-color accent_bg_color {_MSI_RED};
@define-color accent_color {_MSI_RED_LIGHT};
@define-color accent_fg_color #ffffff;
@define-color window_bg_color #121316;
@define-color view_bg_color #191b1f;
@define-color headerbar_bg_color #0d0e10;
@define-color card_bg_color #1f2228;
@define-color sidebar_bg_color #141519;
@define-color popover_bg_color #1f2228;
window {{ background-color: @window_bg_color; }}
headerbar {{ box-shadow: inset 0 -2px 0 alpha({_MSI_RED}, .9); }}
.suggested-action {{
    background-image: linear-gradient(to bottom, {_MSI_RED_HOVER}, {_MSI_RED});
    color: #fff;
}}
.suggested-action:hover {{ background-image: image({_MSI_RED_HOVER}); }}
row.activatable:selected, row.activatable:active {{ background-color: alpha({_MSI_RED}, .18); }}
.msikey-swatch {{
    border-radius: 5px; min-width: 16px; min-height: 16px;
    border: 1px solid alpha(#ffffff, .18);
    box-shadow: 0 0 4px alpha(#000, .6);
}}
"""


def _load_css() -> None:
    rules = [_THEME_CSS]
    for name, (r, g, b) in COLOR_RGB.items():
        rules.append(f".msikey-c-{name}{{background:#{r:02x}{g:02x}{b:02x};}}")
    rules.append(".msikey-c-off{background-image:linear-gradient("
                 "45deg,#3a3a3a 25%,transparent 25%,transparent 50%,#3a3a3a 50%,"
                 "#3a3a3a 75%,transparent 75%);background-size:8px 8px;}")
    provider = Gtk.CssProvider()
    provider.load_from_string("".join(rules))
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


def _color_factory() -> Gtk.SignalListItemFactory:
    factory = Gtk.SignalListItemFactory()

    def setup(_f, item):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        swatch = Gtk.Box()
        swatch.add_css_class("msikey-swatch")
        label = Gtk.Label(xalign=0)
        box.append(swatch)
        box.append(label)
        item.set_child(box)

    def bind(_f, item):
        box = item.get_child()
        swatch = box.get_first_child()
        label = swatch.get_next_sibling()
        name = item.get_item().get_string()
        for c in COLORS:
            swatch.remove_css_class(f"msikey-c-{c}")
        swatch.add_css_class(f"msikey-c-{name}")
        label.set_text(name.capitalize())

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    return factory


# effect name -> parameter spec rows: (name, kind, *args)
#   scale: lo, hi, default, digits      int: lo, hi, default
#   color: default                      bool: default
#   choice: [options], default
FX_SPECS: dict[str, list[tuple]] = {
    "cycle":   [("speed", "scale", 0.3, 5.0, 1.5, 1)],
    "wave":    [("speed", "scale", 0.3, 5.0, 1.5, 1), ("spread", "int", 1, 7, 2)],
    "sweep":   [("speed", "scale", 0.5, 8.0, 3.0, 1), ("colors", "color", "sky"),
                ("bounce", "bool", False)],
    "breathe": [("colors", "color", "purple"), ("period", "scale", 1.0, 10.0, 4.0, 1),
                ("style", "choice", ["swell", "pulse"], "swell"),
                ("floor", "choice", ["off", "medium"], "off")],
    "random":  [("period", "scale", 0.1, 2.0, 0.5, 2)],
    "pulse":   [("speed", "scale", 0.5, 6.0, 2.0, 1), ("color", "color", "red")],
}


def _make_param_row(spec: tuple, on_change):
    """Return (Adw row widget, getter() -> value) for one effect parameter."""
    name, kind = spec[0], spec[1]
    title = name.replace("_", " ").capitalize()

    if kind in ("scale", "int"):
        if kind == "scale":
            lo, hi, default, digits = spec[2], spec[3], spec[4], spec[5]
            step = 10 ** -digits
        else:
            lo, hi, default, digits, step = spec[2], spec[3], spec[4], 0, 1
        row = Adw.SpinRow.new_with_range(lo, hi, step)
        row.set_digits(digits)
        row.set_title(title)
        row.set_value(default)
        row.connect("notify::value", on_change)
        return row, row.get_value

    if kind == "color":
        row = Adw.ActionRow(title=title)
        dd = Gtk.DropDown(model=Gtk.StringList.new(COLORS[1:]), valign=Gtk.Align.CENTER)
        dd.set_factory(_color_factory())
        dd.set_selected(max(0, COLORS[1:].index(spec[2])))
        dd.connect("notify::selected", on_change)
        row.add_suffix(dd)
        return row, lambda: COLORS[1:][dd.get_selected()]

    if kind == "bool":
        row = Adw.SwitchRow(title=title, active=bool(spec[2]))
        row.connect("notify::active", on_change)
        return row, row.get_active

    if kind == "choice":
        options, default = spec[2], spec[3]
        row = Adw.ComboRow(title=title, model=Gtk.StringList.new(options))
        row.set_selected(options.index(default))
        row.connect("notify::selected", on_change)
        return row, lambda: options[row.get_selected()]

    raise ValueError(kind)


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="MSIKey", default_width=460,
                         default_height=640)
        self._loading = False
        self._applying = False
        self.profiles: dict[str, Profile] = load_profiles()
        self.profile: Profile = load_state()
        self._last_on: Profile | None = None

        toast_overlay = Adw.ToastOverlay()
        self.set_content(toast_overlay)
        self._toasts = toast_overlay

        toolbar = Adw.ToolbarView()
        toast_overlay.set_child(toolbar)

        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        self.apply_btn = Gtk.Button(label="Apply")
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.connect("clicked", lambda *_: self.apply(force=True))
        header.pack_start(self.apply_btn)

        self.power_btn = Gtk.ToggleButton(icon_name="system-shutdown-symbolic",
                                          tooltip_text="Toggle backlight")
        self.power_btn.connect("toggled", self._on_power)
        header.pack_start(self.power_btn)

        menu = Gio.Menu()
        menu.append("Set up keyboard access…", "app.setup")
        menu.append("Turn backlight off", "app.off")
        menu.append("About MSIKey", "app.about")
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(menu_btn)

        self.banner = Adw.Banner(revealed=False)
        toolbar.add_top_bar(self.banner)

        self.stack = Adw.ViewStack()
        toolbar.set_content(self.stack)
        toolbar.add_bottom_bar(Adw.ViewSwitcherBar(stack=self.stack, reveal=True))

        self.engine = effects.Engine(
            on_status=lambda m: GLib.idle_add(self._fx_status_msg, m))
        self.sequences: dict[str, dict] = load_sequences()
        self._seq_rows: list = []
        self._param_getters: dict = {}
        self._param_rows: list = []

        page = Adw.PreferencesPage()
        self.stack.add_titled_with_icon(page, "static", "Static",
                                        "display-brightness-symbolic")

        # -- effect ---------------------------------------------------------
        g_effect = Adw.PreferencesGroup(title="Effect")
        page.add(g_effect)
        self.mode_row = Adw.ComboRow(
            title="Animation",
            subtitle="How the zone colours are displayed",
            model=Gtk.StringList.new([m.capitalize() for m in MODES]),
        )
        self.mode_row.connect("notify::selected", self._on_mode)
        g_effect.add(self.mode_row)

        # -- zones --------------------------------------------------------
        g_zones = Adw.PreferencesGroup(
            title="Zones", description="This keyboard has three lighting zones")
        page.add(g_zones)
        self.link_row = Adw.SwitchRow(title="Link zones",
                                      subtitle="Apply one colour and brightness to all three")
        self.link_row.connect("notify::active", lambda *_: self._sync_from_profile())
        g_zones.add(self.link_row)

        self.color_dd: dict[str, Gtk.DropDown] = {}
        self.intensity_dd: dict[str, Gtk.DropDown] = {}
        for region in REGIONS:
            row = Adw.ActionRow(title=region.capitalize())
            cdd = Gtk.DropDown(model=Gtk.StringList.new(COLORS), valign=Gtk.Align.CENTER)
            cdd.set_factory(_color_factory())
            cdd.set_tooltip_text("Colour")
            cdd.connect("notify::selected", self._on_zone_changed, region)
            idd = Gtk.DropDown.new_from_strings(
                [INTENSITY_LABELS[i] for i in INTENSITIES])
            idd.set_valign(Gtk.Align.CENTER)
            idd.set_tooltip_text("Brightness / white mix")
            idd.connect("notify::selected", self._on_zone_changed, region)
            row.add_suffix(cdd)
            row.add_suffix(idd)
            self.color_dd[region] = cdd
            self.intensity_dd[region] = idd
            g_zones.add(row)

        # -- profiles ----------------------------------------------------
        g_prof = Adw.PreferencesGroup(title="Profiles")
        page.add(g_prof)
        self.profile_row = Adw.ComboRow(title="Profile", model=Gtk.StringList.new([]))
        self.profile_row.connect("notify::selected", self._on_profile_pick)
        g_prof.add(self.profile_row)

        btns = Gtk.Box(spacing=6, homogeneous=True, margin_top=6, margin_bottom=6,
                       margin_start=6, margin_end=6)
        for label, cb in (
            ("Save", self._save_current),
            ("Save as…", self._save_as),
            ("Delete", self._delete_profile),
        ):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda _w, cb=cb: cb())
            btns.append(b)
        brow = Adw.PreferencesRow(activatable=False)
        brow.set_child(btns)
        g_prof.add(brow)

        # -- behaviour --------------------------------------------------
        g_beh = Adw.PreferencesGroup(title="Behaviour")
        page.add(g_beh)
        self.instant_row = Adw.SwitchRow(title="Apply changes instantly", active=True)
        g_beh.add(self.instant_row)
        self.autostart_row = Adw.SwitchRow(
            title="Restore lighting on login",
            subtitle="Re-applies the last settings when you log in")
        self.autostart_row.set_active(autostart_enabled())
        self.autostart_row.connect("notify::active", self._on_autostart)
        g_beh.add(self.autostart_row)
        self.status_row = Adw.ActionRow(title="Connection", subtitle="…")
        self.setup_btn = Gtk.Button(label="Set up…", valign=Gtk.Align.CENTER,
                                    tooltip_text="Install the udev rule and the "
                                                 "msi-keyboard driver package")
        self.setup_btn.add_css_class("suggested-action")
        self.setup_btn.connect(
            "clicked",
            lambda *_: self.get_application().activate_action("setup", None))
        self.status_row.add_suffix(self.setup_btn)
        g_beh.add(self.status_row)

        self._build_effects_page()
        self._build_sequence_page()

        self._refresh_profile_list()
        self._sync_from_profile()
        self._refresh_status()
        self._rebuild_params()
        self.stack.connect("notify::visible-child-name", self._on_tab)
        self.connect("close-request", self._on_close)

    def _on_close(self, *_):
        self.engine.stop()
        return False

    def _on_tab(self, *_):
        on_static = self.stack.get_visible_child_name() == "static"
        self.apply_btn.set_visible(on_static)
        if not on_static and self.stack.get_visible_child_name() == "effects":
            self._fx_apply()          # start whatever the effects tab has selected
        elif on_static:
            if self.engine.running:
                self.engine.stop()
            self.apply(force=True)

    # ================================================================== #
    # Effects tab
    # ================================================================== #
    def _build_effects_page(self) -> None:
        page = Adw.PreferencesPage()
        self.stack.add_titled_with_icon(page, "effects", "Effects",
                                        "media-playlist-repeat-symbolic")

        g = Adw.PreferencesGroup(
            title="Animation",
            description="Host-driven - runs while MSIKey is open")
        page.add(g)

        names = ["None"] + [n.capitalize() for n in effects.BUILTINS] + \
                [f"↻ {n}" for n in self.sequences]
        self.fx_names = ["none"] + list(effects.BUILTINS) + \
                        [f"seq:{n}" for n in self.sequences]
        self.fx_row = Adw.ComboRow(title="Effect",
                                   model=Gtk.StringList.new(names))
        self.fx_row.connect("notify::selected", self._on_fx_pick)
        g.add(self.fx_row)

        self.fx_status = Adw.ActionRow(title="Status", subtitle="stopped")
        g.add(self.fx_status)

        self.fx_params = Adw.PreferencesGroup(title="Parameters")
        page.add(self.fx_params)

        gs = Adw.PreferencesGroup()
        page.add(gs)
        save_btn = Gtk.Button(label="Save as profile…", margin_top=6,
                              margin_bottom=6, margin_start=6, margin_end=6)
        save_btn.connect("clicked", lambda *_: self._save_fx_profile())
        srow = Adw.PreferencesRow(activatable=False)
        srow.set_child(save_btn)
        gs.add(srow)

    def _on_fx_pick(self, *_):
        if self._loading:
            return
        self._rebuild_params()
        self._fx_apply()

    def _rebuild_params(self) -> None:
        for row in self._param_rows:
            self.fx_params.remove(row)
        self._param_rows.clear()
        self._param_getters = {}
        key = self.fx_names[self.fx_row.get_selected()]
        spec = FX_SPECS.get(key, [])
        self.fx_params.set_visible(bool(spec))
        for p in spec:
            row, getter = _make_param_row(p, self._on_param_changed)
            self.fx_params.add(row)
            self._param_rows.append(row)
            self._param_getters[p[0]] = getter

    def _on_param_changed(self, *_):
        if self._loading:
            return
        if self.engine.running:
            self._fx_apply()

    def _current_effect(self) -> effects.Effect | None:
        key = self.fx_names[self.fx_row.get_selected()]
        if key == "none":
            return None
        if key.startswith("seq:"):
            return effects.Sequence.from_dict(self.sequences[key[4:]])
        params = {name: get() for name, get in self._param_getters.items()}
        try:
            return effects.make_effect(key, **params)
        except (ValueError, TypeError):
            return None

    def _fx_apply(self) -> None:
        eff = self._current_effect()
        if eff is None:
            if self.engine.running:
                self.engine.stop()
            self.fx_status.set_subtitle("stopped")
            self.apply(force=True)      # back to the static profile
            return
        self.engine.start(eff)
        self.fx_status.set_subtitle(f"running at {self.engine.rate:g} Hz")

    def _fx_status_msg(self, msg: str) -> bool:
        self.fx_status.set_subtitle(msg)
        return False

    def _save_fx_profile(self) -> None:
        key = self.fx_names[self.fx_row.get_selected()]
        if key in ("none",) or key.startswith("seq:"):
            self._toast("Pick a built-in effect first")
            return
        params = {name: get() for name, get in self._param_getters.items()}
        prof = Profile(name=f"{key.capitalize()} (effect)")
        prof.mode = "normal"
        # stash effect spec in the profile name-space via a saved sequence-like blob
        self.sequences[prof.name] = {"effect": key, "params": params}
        save_sequences(self.sequences)
        self._toast(f"Saved “{prof.name}”")
        self._reload_fx_names()

    def _reload_fx_names(self) -> None:
        self._loading = True
        try:
            names = ["None"] + [n.capitalize() for n in effects.BUILTINS] + \
                    [f"↻ {n}" for n in self.sequences]
            self.fx_names = ["none"] + list(effects.BUILTINS) + \
                            [f"seq:{n}" for n in self.sequences]
            self.fx_row.set_model(Gtk.StringList.new(names))
        finally:
            self._loading = False

    # ================================================================== #
    # Sequence tab (keyframe editor)
    # ================================================================== #
    def _build_sequence_page(self) -> None:
        page = Adw.PreferencesPage()
        self.stack.add_titled_with_icon(page, "sequence", "Sequence",
                                        "view-list-symbolic")
        g = Adw.PreferencesGroup(
            title="Keyframes",
            description="Each frame holds its colours for a time, then the next")
        page.add(g)
        self.seq_group = g

        self.seq_frames: list[dict] = [
            {"zones": {r: ("red", "high") for r in REGIONS}, "hold": 0.5},
            {"zones": {r: ("blue", "high") for r in REGIONS}, "hold": 0.5},
        ]
        self._rebuild_seq_rows()

        gb = Adw.PreferencesGroup()
        page.add(gb)
        box = Gtk.Box(spacing=6, homogeneous=True, margin_top=6, margin_bottom=6,
                      margin_start=6, margin_end=6)
        for label, cb in (("Add frame", self._seq_add),
                          ("Play", self._seq_play),
                          ("Stop", self._seq_stop),
                          ("Save…", self._seq_save)):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda _w, cb=cb: cb())
            box.append(b)
        r = Adw.PreferencesRow(activatable=False)
        r.set_child(box)
        gb.add(r)

    def _rebuild_seq_rows(self) -> None:
        for row in self._seq_rows:
            self.seq_group.remove(row)
        self._seq_rows.clear()
        for i, fr in enumerate(self.seq_frames):
            row = Adw.ActionRow(title=f"Frame {i + 1}")
            for r in REGIONS:
                dd = Gtk.DropDown(model=Gtk.StringList.new(COLORS),
                                  valign=Gtk.Align.CENTER)
                dd.set_factory(_color_factory())
                dd.set_selected(COLORS.index(fr["zones"][r][0]))
                dd.connect("notify::selected", self._seq_edit, i, r)
                row.add_suffix(dd)
            spin = Gtk.SpinButton.new_with_range(0.1, 10.0, 0.1)
            spin.set_value(fr["hold"])
            spin.set_valign(Gtk.Align.CENTER)
            spin.set_tooltip_text("Hold (seconds)")
            spin.connect("value-changed", self._seq_hold, i)
            row.add_suffix(spin)
            rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            rm.add_css_class("flat")
            rm.connect("clicked", lambda _w, i=i: self._seq_del(i))
            row.add_suffix(rm)
            self.seq_group.add(row)
            self._seq_rows.append(row)

    def _seq_edit(self, dd, _p, i, region):
        if self._loading:
            return
        color = COLORS[dd.get_selected()]
        self.seq_frames[i]["zones"][region] = (color,
                                               self.seq_frames[i]["zones"][region][1])
        if self.engine.running:
            self._seq_play()

    def _seq_hold(self, spin, i):
        if self._loading:
            return
        self.seq_frames[i]["hold"] = spin.get_value()
        if self.engine.running:
            self._seq_play()

    def _seq_add(self):
        self.seq_frames.append(
            {"zones": {r: ("off", "high") for r in REGIONS}, "hold": 0.5})
        self._rebuild_seq_rows()

    def _seq_del(self, i):
        if len(self.seq_frames) > 1:
            self.seq_frames.pop(i)
            self._rebuild_seq_rows()
            if self.engine.running:
                self._seq_play()

    def _seq_to_dict(self) -> dict:
        return {"loop": True, "keyframes": [
            {"hold": f["hold"], "mode": "normal",
             "zones": {r: {"color": c, "intensity": inten}
                       for r, (c, inten) in f["zones"].items()}}
            for f in self.seq_frames]}

    def _seq_play(self):
        self.engine.start(effects.Sequence.from_dict(self._seq_to_dict()))
        self.fx_status.set_subtitle("sequence running")

    def _seq_stop(self):
        self.engine.stop()
        self.apply(force=True)

    def _seq_save(self):
        dialog = Adw.MessageDialog(transient_for=self, heading="Save sequence",
                                   body="Name:")
        entry = Gtk.Entry(text="My sequence", activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")

        def on_resp(_d, resp):
            if resp != "save":
                return
            name = entry.get_text().strip() or "My sequence"
            self.sequences[name] = self._seq_to_dict()
            save_sequences(self.sequences)
            self._reload_fx_names()
            self._toast(f"Saved “{name}”")

        dialog.connect("response", on_resp)
        dialog.present()

    # ------------------------------------------------------------------ #
    # syncing widgets <-> self.profile
    # ------------------------------------------------------------------ #
    def _sync_from_profile(self) -> None:
        self._loading = True
        try:
            self.mode_row.set_selected(MODES.index(self.profile.mode))
            linked = self.link_row.get_active()
            for region in REGIONS:
                z = self.profile.zones[region]
                self.color_dd[region].set_selected(COLORS.index(z.color))
                self.intensity_dd[region].set_selected(INTENSITIES.index(z.intensity))
                sensitive = region == "left" or not linked
                self.color_dd[region].set_sensitive(sensitive)
                self.intensity_dd[region].set_sensitive(sensitive)
        finally:
            self._loading = False
        self._update_power_btn()

    def _on_mode(self, *_):
        if self._loading:
            return
        self.profile.mode = MODES[self.mode_row.get_selected()]
        self._mark_dirty()
        self.apply()

    def _on_zone_changed(self, _dd, _param, region):
        if self._loading:
            return
        color = COLORS[self.color_dd[region].get_selected()]
        intensity = INTENSITIES[self.intensity_dd[region].get_selected()]
        targets = REGIONS if self.link_row.get_active() else [region]
        for t in targets:
            self.profile.zones[t] = ZoneState(color, intensity)
        if self.link_row.get_active():
            self._sync_from_profile()
        self._update_power_btn()
        self._mark_dirty()
        self.apply()

    def _update_power_btn(self) -> None:
        prev = self._loading
        self._loading = True
        try:
            off = not self.profile.is_lit()
            self.power_btn.set_active(off)
            self.power_btn.set_tooltip_text(
                "Turn backlight on" if off else "Turn backlight off")
        finally:
            self._loading = prev

    def _on_power(self, btn: Gtk.ToggleButton):
        if self._loading:
            return
        if self.engine.running:
            self.engine.stop()
        if btn.get_active():  # pressed in == turn off, remembering the colours
            if self.profile.is_lit():
                self._last_on = self.profile.copy()
            for z in self.profile.zones.values():
                z.color = "off"
        else:  # restore the colours from before it was switched off
            self.profile = (self._last_on or load_last_lit()).copy()
            self._last_on = None
        self._sync_from_profile()
        self._mark_dirty()
        self.apply(force=True)

    # ------------------------------------------------------------------ #
    # profiles
    # ------------------------------------------------------------------ #
    def _profile_names(self) -> list[str]:
        return list(self.profiles.keys())

    def _refresh_profile_list(self, select: str | None = None) -> None:
        self._loading = True
        try:
            names = self._profile_names() + [_UNSAVED]
            self.profile_row.set_model(Gtk.StringList.new(names))
            target = select or self._match_profile_name() or _UNSAVED
            self.profile_row.set_selected(names.index(target))
        finally:
            self._loading = False

    def _match_profile_name(self) -> str | None:
        cur = self.profile.to_dict()
        cur.pop("name", None)
        for name, p in self.profiles.items():
            d = p.to_dict()
            d.pop("name", None)
            if d == cur:
                return name
        return None

    def _on_profile_pick(self, *_):
        if self._loading:
            return
        names = self._profile_names() + [_UNSAVED]
        name = names[self.profile_row.get_selected()]
        if name == _UNSAVED or name not in self.profiles:
            return
        self.profile = Profile.from_dict(self.profiles[name].to_dict())
        self._sync_from_profile()
        self.apply(force=True)

    def _mark_dirty(self) -> None:
        save_state(self.profile)
        self._refresh_profile_list()

    def _save_current(self) -> None:
        name = self._match_profile_name()
        if name is None:
            self._save_as()
            return
        self.profiles[name] = Profile.from_dict({**self.profile.to_dict(), "name": name})
        save_profiles(self.profiles)
        self._toast(f"Saved “{name}”")

    def _save_as(self) -> None:
        dialog = Adw.MessageDialog(transient_for=self, heading="Save profile",
                                   body="Name for this lighting profile:")
        entry = Gtk.Entry(text="My profile", activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")

        def on_resp(_d, resp):
            if resp != "save":
                return
            name = entry.get_text().strip() or "My profile"
            self.profiles[name] = Profile.from_dict(
                {**self.profile.to_dict(), "name": name})
            save_profiles(self.profiles)
            self._refresh_profile_list(select=name)
            self._toast(f"Saved “{name}”")

        dialog.connect("response", on_resp)
        dialog.present()

    def _delete_profile(self) -> None:
        name = self._match_profile_name()
        if name is None:
            self._toast("Current settings aren't a saved profile")
            return
        dialog = Adw.MessageDialog(
            transient_for=self, heading=f"Delete “{name}”?",
            body="This can't be undone.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_resp(_d, resp):
            if resp == "delete":
                self.profiles.pop(name, None)
                save_profiles(self.profiles)
                self._refresh_profile_list()
                self._toast(f"Deleted “{name}”")

        dialog.connect("response", on_resp)
        dialog.present()

    # ------------------------------------------------------------------ #
    # behaviour
    # ------------------------------------------------------------------ #
    def _on_autostart(self, row, *_):
        if self._loading:
            return
        try:
            set_autostart(row.get_active())
        except OSError as e:
            self._toast(f"Autostart change failed: {e}")

    # ------------------------------------------------------------------ #
    # applying
    # ------------------------------------------------------------------ #
    def apply(self, force: bool = False) -> None:
        if self._applying:
            return
        if not force and not self.instant_row.get_active():
            return
        self._applying = True
        profile = Profile.from_dict(self.profile.to_dict())

        def worker():
            try:
                backend = apply_profile(profile)
                msg = None if backend == "hidraw" else "Applied (via msi-keyboard)"
            except KeyboardError as e:
                msg = str(e)
            GLib.idle_add(self._apply_done, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_done(self, msg: str | None) -> bool:
        self._applying = False
        if msg:
            self._toast(msg)
        self._refresh_status()
        return False

    def _refresh_status(self) -> None:
        path = find_hidraw()
        has_rule = rule_installed()
        has_driver = driver_installed()
        if path:
            if has_rule:
                note = "direct access"
            else:
                note = "needs setup — click “Set up…”"
            if not has_driver:
                note += " · no msi-keyboard fallback"
            self.status_row.set_subtitle(f"{path} — {note}")
            self.banner.set_revealed(False)
        else:
            self.status_row.set_subtitle("keyboard not detected")
            self.banner.set_title("MSI keyboard (1770:ff00) not found")
            self.banner.set_revealed(True)
        self.setup_btn.set_visible(not (has_rule and has_driver))

    def _toast(self, text: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=text, timeout=4))


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.win: Window | None = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        _load_css()
        for name, cb in (
            ("about", self._about),
            ("off", self._off),
            ("setup", self._setup),
            ("quit", lambda *_: self.quit()),
        ):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
        self.set_accels_for_action("app.quit", ["<Primary>q"])

    def do_activate(self):
        if not self.win:
            self.win = Window(self)
        self.win.present()

    def do_shutdown(self):
        if self.win:
            self.win.engine.stop()
        Adw.Application.do_shutdown(self)

    def _about(self, *_):
        about = Adw.AboutWindow(
            transient_for=self.win, application_name="MSIKey",
            application_icon="input-keyboard", version=__version__,
            developer_name="MSIKey",
            comments="Lighting control for MSI SteelSeries 3-zone keyboards "
                     "(USB 1770:ff00).",
            license_type=Gtk.License.MIT_X11)
        about.present()

    def _off(self, *_):
        if self.win:
            self.win.power_btn.set_active(True)

    def _setup(self, *_):
        win = self.win
        if win is None:
            return
        win.setup_btn.set_sensitive(False)
        win._toast("Requesting authorisation…")

        def worker():
            try:
                msg = setup_access()
            except RuntimeError as e:
                msg = str(e)

            def done():
                win.setup_btn.set_sensitive(True)
                win._toast(msg)
                win._refresh_status()
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()


def run_gui() -> int:
    return App().run(None)
