#!/usr/bin/env python3
"""Tiger-style dock with OS X magnification.

Everything is drawn with cairo on a transparent layer-shell surface that spans
the bottom of the screen. Only the shelf and icons accept input; the rest of the
strip is click-through.

Magnification follows the classic OS X model: every icon's size depends on the
pointer's distance from that icon's *resting* centre, and the magnified row is
then shifted so the point under the pointer stays under the pointer. That is
what keeps the motion smooth and stops icons jittering away from the cursor.
"""
import json
import math
import os
import sys
import time

import cairo
import gi

import search
import sounds

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, GtkLayerShell, Pango, PangoCairo  # noqa: E402

try:
    gi.require_version("GioUnix", "2.0")
    from gi.repository import GioUnix
    DesktopAppInfo = GioUnix.DesktopAppInfo
except (ValueError, ImportError):
    DesktopAppInfo = Gio.DesktopAppInfo

HERE = os.path.dirname(os.path.abspath(__file__))
BUS_NAME = "local.TigerDock"
BUS_PATH = "/local/TigerDock"
BUS_XML = """
<node><interface name="local.TigerDock">
  <method name="NoWindow"><arg type="s" name="id" direction="in"/></method>
  <method name="Search"/>
</interface></node>"""

DEFAULTS = {
    "icon_size": 48,          # resting icon size (px)
    "max_icon_size": 96,      # size of the icon directly under the pointer
    "spread": 3.0,            # how many icons either side are affected
    "spacing": 4,             # gap between icons
    "edge_padding": 10,       # shelf padding left/right of the first/last icon
    "icon_theme": "oxygen",   # glossy theme suits Aqua; "" = system theme
    "label_font": "Sans Bold 10.5",
    "trash": True,
    "folders": ["~/Downloads", "~/Documents"],   # stacks next to the trash
    "mounts": True,           # show removable drives / mounted volumes
    "sounds": True,           # procedural sound effects
    "volume": 0.5,            # 0..1
    "categories": ["Internet", "Office", "Graphics", "Multimedia", "Development", "Games",
                   "Education", "Science", "Utilities", "System", "Settings", "Other"],
}

MIP_SIZES = (32, 48, 64, 96, 128, 256)


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(os.path.join(HERE, "config.json")) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    return cfg


def ease(cur, target, dt, tau):
    """Frame-rate independent exponential approach."""
    return target + (cur - target) * math.exp(-dt / tau)


def rounded_top_rect(cr, x, y, w, h, r):
    """Rect with rounded top corners; bottom is drawn past the screen edge."""
    cr.new_sub_path()
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.arc(x + w - r, y + r, r, 1.5 * math.pi, 2 * math.pi)
    cr.line_to(x + w, y + h)
    cr.line_to(x, y + h)
    cr.close_path()


def pill(cr, x, y, w, h):
    r = h / 2
    cr.new_sub_path()
    cr.arc(x + r, y + r, r, 0.5 * math.pi, 1.5 * math.pi)
    cr.arc(x + w - r, y + r, r, 1.5 * math.pi, 0.5 * math.pi)
    cr.close_path()


# --------------------------------------------------------------------------- items

class Icon:
    """Mip-mapped icon surfaces plus a pre-rendered soft drop shadow."""

    def __init__(self, theme, gicon, names, scale, sizes=MIP_SIZES):
        self.levels = {}
        self.scale = scale
        self._gray = {}
        for size in sizes:
            px = size * scale
            info = None
            if gicon is not None:
                info = theme.lookup_by_gicon(gicon, px, Gtk.IconLookupFlags.FORCE_SIZE)
            for n in names:
                if info is None:
                    info = theme.lookup_icon(n, px, Gtk.IconLookupFlags.FORCE_SIZE)
            if info is None:
                info = theme.lookup_icon("application-x-executable", px,
                                         Gtk.IconLookupFlags.FORCE_SIZE)
            try:
                pix = info.load_icon()
            except (GLib.Error, AttributeError):
                continue
            surf = Gdk.cairo_surface_create_from_pixbuf(pix, scale, None)
            self.levels[size] = (surf, self._shadow(surf, size, scale))

    @staticmethod
    def _shadow(surf, size, scale):
        """Cheap blurred silhouette: stacked offset masks, rendered once."""
        pad = max(4, size // 10)
        out = cairo.ImageSurface(cairo.FORMAT_ARGB32,
                                 (size + 2 * pad) * scale, (size + 2 * pad) * scale)
        out.set_device_scale(scale, scale)
        cr = cairo.Context(out)
        r = size / 24.0
        steps = 12
        for i in range(steps):
            a = 2 * math.pi * i / steps
            for rad, alpha in ((r, 0.05), (r * 2, 0.03)):
                cr.set_source_rgba(0, 0, 0, alpha)
                cr.mask_surface(surf, pad + math.cos(a) * rad,
                                pad + math.sin(a) * rad + size / 28.0)
        return out, pad

    def gray(self, mip):
        """Desaturated copy of one mip level (made on first use)."""
        g = self._gray.get(mip)
        if g is None:
            surf = self.levels[mip][0]
            px = mip * self.scale
            g = cairo.ImageSurface(cairo.FORMAT_ARGB32, px, px)
            g.set_device_scale(self.scale, self.scale)
            c = cairo.Context(g)
            c.set_source_surface(surf, 0, 0)
            c.paint()
            c.set_operator(cairo.OPERATOR_HSL_SATURATION)
            c.set_source_rgb(0.5, 0.5, 0.5)
            c.mask_surface(surf, 0, 0)
            self._gray[mip] = g
        return g

    def pick(self, size_px):
        for s in MIP_SIZES:
            if s >= size_px and s in self.levels:
                return s, self.levels[s]
        s = max(self.levels)
        return s, self.levels[s]


class Item:
    kind = "app"

    def __init__(self):
        self.x = 0.0          # current left edge
        self.size = 0.0       # current drawn size
        self.rest_center = 0.0
        self.bounce_t0 = None
        self.bounce_until_running = False
        self.running = False


class AppItem(Item):
    def __init__(self, info):
        super().__init__()
        self.info = info
        self.id = info.get_id()[:-len(".desktop")] if info.get_id() else ""
        self.label = info.get_display_name()
        self.gicon = info.get_icon()
        self.names = self._match_names(info)

    @staticmethod
    def _match_names(info):
        names = set()
        exe = info.get_executable() or ""
        if exe and os.path.basename(exe) not in ("flatpak", "env", "sh", "bash"):
            names.add(os.path.basename(exe))
        wm = info.get_startup_wm_class()
        if wm:
            names.add(wm.lower())
        if info.get_id():
            names.add(info.get_id()[:-len(".desktop")].split(".")[-1].lower())
        return names

    def activate(self, dock):
        dock.activate_app(self)


def folder_places(paths):
    out = []
    for p in paths:
        path = os.path.expanduser(p)
        if not os.path.isdir(path):
            continue
        f = Gio.File.new_for_path(path)
        try:
            inf = f.query_info("standard::display-name,standard::icon", Gio.FileQueryInfoFlags.NONE, None)
            label, gicon = inf.get_display_name(), inf.get_icon()
        except GLib.Error:
            label, gicon = os.path.basename(path.rstrip("/")) or path, None
        out.append(PlaceItem("dir:" + path, label, f, gicon))
    return out


def mount_places(monitor):
    out = []
    for m in monitor.get_mounts():
        if m.is_shadowed():
            continue
        root = m.get_root()
        path = root.get_path() or ""
        # Skip system mounts; keep removable media, network shares, etc.
        if path and not (path.startswith("/run/media/") or path.startswith("/media/")
                         or path.startswith("/mnt/")) and root.is_native():
            continue
        out.append(PlaceItem("mnt:" + root.get_uri(), m.get_name(), root, m.get_icon(), m))
    return out


class Separator(Item):
    kind = "sep"


# name -> (icon names, freedesktop main categories)
CATEGORIES = {
    "Internet":    (["applications-internet"], ["Network"]),
    "Office":      (["applications-office"], ["Office"]),
    "Graphics":    (["applications-graphics"], ["Graphics"]),
    "Multimedia":  (["applications-multimedia"], ["AudioVideo", "Audio", "Video"]),
    "Development": (["applications-development"], ["Development"]),
    "Games":       (["applications-games"], ["Game"]),
    "Education":   (["applications-education"], ["Education"]),
    "Science":     (["applications-science", "applications-education"], ["Science"]),
    "Utilities":   (["applications-utilities", "applications-accessories"], ["Utility"]),
    "System":      (["applications-system"], ["System"]),
    "Settings":    (["preferences-system", "preferences-desktop"], ["Settings"]),
    "Other":       (["applications-other"], []),
}


def categorize_apps():
    """Group every visible desktop entry into one dock category."""
    lookup = {fd: name for name, (_i, fds) in CATEGORIES.items() for fd in fds}
    groups = {name: [] for name in CATEGORIES}
    seen = set()
    for info in Gio.AppInfo.get_all():
        if not info.should_show() or not info.get_id() or info.get_id() in seen:
            continue
        seen.add(info.get_id())
        cats = [c for c in (info.get_categories() or "").split(";") if c]
        # Settings wins over System so control panels land together.
        if "Settings" in cats:
            name = "Settings"
        else:
            name = next((lookup[c] for c in cats if c in lookup), "Other")
        groups[name].append(AppItem(info))
    for apps in groups.values():
        apps.sort(key=lambda a: a.label.lower())
    return groups


APP_MIPS = (32, 48, 64, 96)       # app icons never draw bigger than ~70 px


def app_icon(dock, app):
    if not hasattr(app, "icon"):
        app.icon = Icon(dock.icon_theme(), app.gicon, [app.id], dock.get_scale_factor(), APP_MIPS)
    return app.icon


class SearchAnchor(Item):
    """Invisible stand-in the search blob grows from (middle of the bar)."""
    kind = "search"
    label = ""

    def __init__(self):
        super().__init__()
        self.apps = []


class CategoryItem(Item):
    kind = "cat"

    def __init__(self, name, icon_names, apps):
        super().__init__()
        self.label = name
        self.icon_names = icon_names
        self.apps = apps
        self.pull = Spring()       # 0 = in its dock slot, 1 = riding in the blob
        self.pull_to = None        # last (cx, cy, size) inside the blob

    def activate(self, dock):
        dock.bubble.toggle(self)


MAX_STACK = 30      # files per page of a folder stack (newest first); scroll for more
MAX_FILES = 2000


class FileEntry:
    """One file inside a folder stack; quacks like an AppItem for the blob."""
    running = False
    names = frozenset()

    def __init__(self, gfile, label, gicon):
        self.gfile = gfile
        self.id = gfile.get_uri()
        self.label = label
        self.gicon = gicon

    def open(self):
        try:
            Gio.AppInfo.launch_default_for_uri(self.gfile.get_uri(), None)
        except GLib.Error as e:
            print("dock: open failed:", e, file=sys.stderr)


class PlaceItem(CategoryItem):
    """A folder or mounted volume; its blob shows the folder's contents."""

    def __init__(self, key, label, gfile, gicon, mount=None):
        super().__init__(label, ["folder"], [])
        self.key = key
        self.gfile = gfile
        self.gicon = gicon
        self.mount = mount
        self._entries = {}
        self.files = []
        self.page = 0
        self.open_entry = self._open_entry()
        self.apps = [self.open_entry]

    @property
    def pages(self):
        return max(1, -(-len(self.files) // MAX_STACK))

    def set_page(self, page):
        """Show one page of files (plus the Open entry); True if it changed."""
        page = max(0, min(page, self.pages - 1))
        apps = self.files[page * MAX_STACK:(page + 1) * MAX_STACK] + [self.open_entry]
        changed = page != self.page or [a.id for a in apps] != [a.id for a in self.apps]
        self.page, self.apps = page, apps
        return changed

    def _open_entry(self):
        return FileEntry(self.gfile, "Open " + self.label, self.gicon or Gio.ThemedIcon.new("folder"))

    def refresh(self):
        """Re-read the folder; reuse entries so loaded icons carry over."""
        files = []
        try:
            en = self.gfile.enumerate_children(
                "standard::name,standard::display-name,standard::icon,standard::is-hidden,"
                "time::modified,time::created", Gio.FileQueryInfoFlags.NONE, None)
            for inf in en:
                if inf.get_is_hidden() or inf.get_name().startswith("."):
                    continue
                t = (inf.get_attribute_uint64("time::created")
                     or inf.get_attribute_uint64("time::modified"))
                files.append((t, inf))
            en.close(None)
        except GLib.Error as e:
            print("dock: can't read", self.label, e.message, file=sys.stderr)
        files.sort(key=lambda f: -f[0])
        apps, keep = [], {}
        for _t, inf in files[:MAX_FILES]:
            child = self.gfile.get_child(inf.get_name())
            uri = child.get_uri()
            e = self._entries.get(uri) or FileEntry(child, inf.get_display_name(), inf.get_icon())
            keep[uri] = e
            apps.append(e)
        self._entries = keep
        self.files = apps
        return self.set_page(self.page)

    def eject(self, dock):
        m = self.mount
        if m is None:
            return
        op = Gio.MountOperation()
        flags = Gio.MountUnmountFlags.NONE

        def done(res, finish):
            try:
                finish(res)
            except GLib.Error as e:
                print("dock: eject failed:", e.message, file=sys.stderr)
        if m.can_eject():
            m.eject_with_operation(flags, op, None, lambda _s, r: done(r, m.eject_with_operation_finish))
        elif m.can_unmount():
            m.unmount_with_operation(flags, op, None, lambda _s, r: done(r, m.unmount_with_operation_finish))


TRASH_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                         "Trash", "files")


class TrashItem(PlaceItem):
    # Read the XDG trash folder directly: trash:/// needs gvfs, which KDE lacks.
    def __init__(self):
        super().__init__("trash", "Trash", Gio.File.new_for_path(TRASH_DIR),
                         Gio.ThemedIcon.new("user-trash"))
        self.full = False

    def _open_entry(self):
        return FileEntry(Gio.File.new_for_uri("trash:///"), "Open Trash", Gio.ThemedIcon.new("user-trash"))

    def is_full(self):
        try:
            return any(True for _ in os.scandir(TRASH_DIR))
        except OSError:
            return False




# --------------------------------------------------------------------------- dock

class Dock(Gtk.Window):
    LABEL_H = 34          # room reserved above the tallest icon for the label
    BOUNCE_T = 0.62       # seconds per launch bounce
    MAX_BOUNCES = 8

    def __init__(self, cfg):
        super().__init__(title="Tiger Dock")
        self.cfg = cfg
        self.base = cfg["icon_size"]
        self.max = max(cfg["max_icon_size"], self.base)
        self.spread = max(cfg["spread"], 0.5)
        self.spacing = cfg["spacing"]
        self.edge = cfg["edge_padding"]
        self.sep_w = max(10, round(self.base * 0.28))
        self.pad_top = max(3, round(self.base * 0.08))
        self.pad_bottom = max(7, round(self.base * 0.16))   # triangle lives here
        self.shelf_h = self.base + self.pad_top + self.pad_bottom
        self.bounce_h = round(self.base * 0.75)
        self.height = max(self.max, self.base + self.bounce_h) + self.pad_bottom + self.LABEL_H
        self.height += -self.height % 4

        self.mouse_x = None
        self.hover = False
        self.mag = 0.0
        self.pressed = None
        self.last_t = None
        self.region_key = None
        self.dirty = True
        self.stripes = self._make_stripes()
        self.icons = {}

        self.sfx = sounds.Player(cfg["sounds"], cfg["volume"])
        self.search_geom = ([], 1, 1, 1, 1)
        self._setup_window()
        self.font = Pango.FontDescription.from_string(cfg["label_font"])

        self.items = []
        self.volume_monitor = Gio.VolumeMonitor.get()
        for sig in ("mount-added", "mount-removed", "mount-changed"):
            self.volume_monitor.connect(sig, lambda *_: self._on_apps_changed(None))
        self._build_items()
        self._fit_height()
        self.last_bbox = None
        self.rest_id = None

        self.area = Gtk.DrawingArea()
        self.area.set_size_request(-1, self.height)
        self.area.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK
                             | Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.BUTTON_PRESS_MASK
                             | Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.SCROLL_MASK
                             | Gdk.EventMask.SMOOTH_SCROLL_MASK)
        self.scroll_acc = 0.0
        self.area.connect("scroll-event", self.on_scroll)
        # Drop folders from the file manager onto the dock to add them.
        self.area.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY | Gdk.DragAction.LINK)
        self.area.drag_dest_add_uri_targets()
        self.area.connect("drag-data-received", self.on_drop)
        self.drag = None           # (item, x, y) while a folder is dragged off the dock
        self.press_xy = None
        self.area.connect("draw", self.on_draw)
        self.area.connect("motion-notify-event", self.on_motion)
        self.area.connect("enter-notify-event", self.on_motion)
        self.area.connect("leave-notify-event", self.on_leave)
        self.area.connect("button-press-event", self.on_press)
        self.area.connect("button-release-event", self.on_release)
        self.add(self.area)

        self.connect("realize", lambda *_: (self._load_icons(), self._preload_app_icons()))
        self.bubble = Blob(self)
        self.search = search.SearchBar(self)
        self.hovered = None
        self.tick_id = None
        self.wake()

        self._poll_state()
        GLib.timeout_add(1500, self._poll_state)

        # Installed / removed apps show up live: GLib watches every
        # applications/ dir (system, ~/.local, flatpak) and tells us.
        self.rebuild_id = None
        self.app_monitor = Gio.AppInfoMonitor.get()
        self.app_monitor.connect("changed", self._on_apps_changed)

    # ---- app list ----------------------------------------------------------

    def _build_items(self):
        """(Re)build the dock items, reusing existing objects so open blobs,
        loaded icons, springs and running state carry over."""
        old_cats = {i.label: i for i in self.items if type(i) is CategoryItem}
        old_apps = {a.id: a for c in old_cats.values() for a in c.apps}
        old_trash = next((i for i in self.items if isinstance(i, TrashItem)), None)
        groups = categorize_apps()
        items = []
        for name in self.cfg["categories"]:
            if name == "|":
                items.append(Separator())
                continue
            if name not in CATEGORIES:
                if not old_cats and not self.items:
                    print("dock: unknown category", name, file=sys.stderr)
                continue
            apps = [old_apps.get(a.id, a) for a in groups[name]]
            for a, fresh in zip(apps, groups[name]):
                a.info, a.label = fresh.info, fresh.label      # pick up edits
            if not apps:
                continue
            cat = old_cats.get(name)
            if cat is None:
                cat = CategoryItem(name, CATEGORIES[name][0], apps)
            cat.apps = apps
            items.append(cat)
        old_places = {i.key: i for i in self.items if isinstance(i, PlaceItem)}
        places = folder_places(self.cfg["folders"])
        if self.cfg["mounts"]:
            places += mount_places(self.volume_monitor)
        right = [old_places.get(p.key, p) for p in places]
        for p, fresh in zip(right, places):
            p.label, p.mount = fresh.label, fresh.mount
        if self.cfg["trash"]:
            right.append(old_trash or TrashItem())
        if right:
            if items and items[-1].kind != "sep":
                items.append(Separator())
            items += right
        self.items = items

    def all_apps(self):
        seen = {}
        for it in self.items:
            if type(it) is CategoryItem:
                for a in it.apps:
                    seen.setdefault(a.id, a)
        return sorted(seen.values(), key=lambda a: a.label.lower())

    def _preload_app_icons(self):
        todo = iter(self.all_apps())

        def chunk():
            for _ in range(3):
                app = next(todo, None)
                if app is None:
                    return False
                app_icon(self, app)
            return True
        GLib.idle_add(chunk, priority=GLib.PRIORITY_LOW)

    def toggle_search(self):
        self.search.toggle()
        return False

    def _fit_height(self):
        """The blob lives in the same surface, so reserve room for the biggest one."""
        biggest = max((blob_radius(MAX_STACK + 1 if isinstance(i, PlaceItem) else len(i.apps))
                       for i in self.items if isinstance(i, CategoryItem)), default=0)
        gap = self.max - self.base + 26
        h = max(self.height, self.shelf_h + round(gap * 1.25 + biggest * 2.7) + BLOB_LABEL)
        # The search blob holds every app; size it to fit under the search bar.
        geo = Gdk.Display.get_default().get_monitor(0).get_geometry()
        self.search_geom = search_layout(len(self.all_apps()), geo.width - 60,
                                         geo.height - self.shelf_h - self.gap_lift()
                                         - search.TOP - search.BODY_H - 90)
        _pts, _rx, ry, _cw, _ch = self.search_geom
        h = max(h, self.shelf_h + self.gap_lift() + round(2 * ry) + 40)
        h += -h % 4
        changed = h != self.height
        self.height = h
        return changed

    def gap_lift(self):
        return self.max - self.base + 12          # how high the ball floats (Blob.gap)

    def _on_apps_changed(self, _monitor):
        # Package managers write many files at once; settle first.
        if self.rebuild_id is not None:
            GLib.source_remove(self.rebuild_id)
        self.rebuild_id = GLib.timeout_add(600, self._rebuild_apps)

    def _rebuild_apps(self):
        self.rebuild_id = None
        self._build_items()
        n = sum(len(i.apps) for i in self.items if type(i) is CategoryItem)
        print(f"dock: app list changed, {n} apps", file=sys.stderr, flush=True)
        if self.get_realized():
            self._load_icons(only_missing=True)
        if self._fit_height():
            self.area.set_size_request(-1, self.height)
        if self.pressed not in self.items:
            self.pressed = None
        self.hovered = None                      # re-evaluated on next draw
        self.bubble.apps_changed()
        self._poll_state()
        self.last_bbox = None
        self.dirty = True
        self.area.queue_draw()
        self.wake()
        return GLib.SOURCE_REMOVE

    # ---- setup -----------------------------------------------------------

    def _setup_window(self):
        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_namespace(self, "tiger-dock")
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.TOP)
        for edge in (GtkLayerShell.Edge.BOTTOM, GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT):
            GtkLayerShell.set_anchor(self, edge, True)
        GtkLayerShell.set_exclusive_zone(self, self.shelf_h)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
        visual = self.get_screen().get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)
        self.set_app_paintable(True)

    def icon_theme(self):
        if not hasattr(self, "_theme"):
            self._theme = Gtk.IconTheme.get_default()
            if self.cfg["icon_theme"]:
                self._theme = Gtk.IconTheme()
                self._theme.set_custom_theme(self.cfg["icon_theme"])
        return self._theme

    def _load_icons(self, only_missing=False):
        theme = self.icon_theme()
        scale = self.get_scale_factor()
        for it in self.items:
            if only_missing and (hasattr(it, "icon") or hasattr(it, "icon_empty")):
                continue
            if isinstance(it, TrashItem):
                it.icon_empty = Icon(theme, None, ["user-trash"], scale)
                it.icon_full = Icon(theme, None, ["user-trash-full", "user-trash"], scale)
            elif isinstance(it, PlaceItem):
                it.icon = Icon(theme, it.gicon, ["folder", "drive-removable-media"], scale)
            elif isinstance(it, CategoryItem):
                it.icon = Icon(theme, None, it.icon_names, scale)

    # ---- state polling: running apps + trash ------------------------------

    def _poll_state(self):
        procs = set()
        me = os.getpid()
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or int(pid) == me:
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    argv0 = f.read().split(b"\0", 1)[0].decode(errors="ignore")
                with open(f"/proc/{pid}/comm") as f:
                    comm = f.read().strip()
            except OSError:
                continue
            if argv0:
                procs.add(os.path.basename(argv0).lower())
            procs.add(comm.lower())
        changed = False
        for it in self.items:
            if isinstance(it, PlaceItem) and not isinstance(it, TrashItem):
                continue
            if type(it) is CategoryItem:
                for app in it.apps:
                    app.running = any(n.lower() in procs or n.lower()[:15] in procs
                                      for n in app.names)
                running = any(a.running for a in it.apps)
                changed |= running != it.running
                it.running = running
            elif isinstance(it, TrashItem):
                full = it.is_full()
                changed |= full != it.full
                it.full = full
        if changed:
            self.dirty = True
            self.wake()
        return True

    # ---- launching / activation ------------------------------------------

    def launch(self, item):
        try:
            item.info.launch([], None)
        except GLib.Error as e:
            print("dock: launch failed:", e, file=sys.stderr)
            return
        # The category bounces until the app shows up as running.
        cat = self.category_of(item)
        if cat is not None:
            cat.bounce_t0 = time.monotonic()
            cat.bounce_app = item
            cat.bounce_until_running = True
        self.wake()

    def category_of(self, app):
        return next((c for c in self.items if type(c) is CategoryItem and app in c.apps), None)

    def activate_app(self, item):
        """Focus an existing window via KWin; KWin calls back NoWindow if none."""
        self.sfx.play("launch")
        self.search.close(launched=True)
        self.bubble.close(quiet=True)
        if isinstance(item, FileEntry):
            item.open()
            return
        if not item.running or not kwin_activate(item):
            self.launch(item)

    def on_no_window(self, app_id):
        for c in self.items:
            for app in getattr(c, "apps", []):
                if app.id == app_id:
                    self.launch(app)
                    return

    # ---- layout -------------------------------------------------------------

    def _rest_width(self, it):
        return self.sep_w if it.kind == "sep" else self.base

    def layout(self, width):
        s, unit = self.spacing, self.base + self.spacing
        rest = [self._rest_width(it) for it in self.items]
        total = sum(rest) + s * (len(rest) - 1)
        x = (width - total) / 2
        starts = []
        for it, w in zip(self.items, rest):
            starts.append(x)
            it.rest_center = x + w / 2
            x += w + s

        mx = self.mouse_x if self.mouse_x is not None else width / 2
        sizes = []
        for it, w in zip(self.items, rest):
            if it.kind == "sep" or self.mag <= 0.001:
                sizes.append(w)
                continue
            d = abs(mx - it.rest_center) / unit
            k = (math.cos(math.pi * min(d / self.spread, 1.0)) + 1) / 2
            sizes.append(self.base + (self.max - self.base) * k * self.mag)

        # Magnified row, relative to 0.
        mstarts, x = [], 0.0
        for w in sizes:
            mstarts.append(x)
            x += w + s

        # Map pointer through the slot it sits in so it stays fixed.
        left0 = starts[0] - s / 2
        right0 = starts[-1] + rest[-1] + s / 2
        if mx <= left0:
            mapped = mstarts[0] - s / 2 - (left0 - mx)
        elif mx >= right0:
            mapped = mstarts[-1] + sizes[-1] + s / 2 + (mx - right0)
        else:
            mapped = mstarts[-1] + sizes[-1] + s / 2
            for i, w in enumerate(rest):
                a, b = starts[i] - s / 2, starts[i] + w + s / 2
                if mx < b:
                    f = (mx - a) / (b - a)
                    mapped = mstarts[i] - s / 2 + f * (sizes[i] + s)
                    break
        offset = mx - mapped
        for it, x0, w in zip(self.items, mstarts, sizes):
            it.x = offset + x0
            it.size = w

    def shelf_rect(self, height):
        first, last = self.items[0], self.items[-1]
        x0 = first.x - self.edge
        x1 = last.x + last.size + self.edge
        return x0, height - self.shelf_h, x1 - x0, self.shelf_h

    def item_at(self, x, y, height):
        sx, sy, sw, sh = self.shelf_rect(height)
        if not (sx <= x <= sx + sw):
            return None
        for it in self.items:
            top = height - self.pad_bottom - it.size
            if it.kind != "sep" and it.x - self.spacing / 2 <= x <= it.x + it.size + self.spacing / 2 \
                    and y >= min(top, sy):
                return it
        return None

    def bounce_offset(self, it, now):
        if it.bounce_t0 is None:
            return 0.0
        t = now - it.bounce_t0
        n = int(t / self.BOUNCE_T)
        if n >= self.MAX_BOUNCES or (n >= 1 and it.bounce_until_running and getattr(it, 'bounce_app', it).running):
            it.bounce_t0 = None
            return 0.0
        p = (t % self.BOUNCE_T) / self.BOUNCE_T
        return self.bounce_h * 4 * p * (1 - p)

    # ---- events -------------------------------------------------------------

    def on_motion(self, _w, ev):
        self.hover = True
        if self.drag is not None and not ev.state & Gdk.ModifierType.BUTTON1_MASK:
            self._drag_end(ev.x, ev.y)          # missed the release
        if ev.state & Gdk.ModifierType.BUTTON1_MASK and self._drag_motion(ev.x, ev.y):
            self.mouse_x = ev.x
            return False
        if self.bubble.contains(ev.x, ev.y):
            # Inside the blob the dock holds still, so the icons don't slide away.
            self.bubble.pointer(ev.x, ev.y)
        else:
            self.bubble.pointer_out()
            self.mouse_x = ev.x
            # Phase 2 starts once the pointer settles over a category.
            if self.rest_id is not None:
                GLib.source_remove(self.rest_id)
            self.rest_id = GLib.timeout_add(REST_MS, self._on_rest)
        self.dirty = True
        self.wake()
        return False

    def _on_rest(self):
        self.rest_id = None
        self.bubble.on_rest()
        return False

    def on_leave(self, _w, ev):
        if self.drag is not None:
            # Left the surface mid-drag: treat it as pulled off the dock.
            self._drag_end(ev.x, min(ev.y, 0))
            self.pressed = None
        if ev.detail != Gdk.NotifyType.INFERIOR:
            self.hover = False
            self.pressed = None
            self.bubble.pointer_out()
            self.bubble.on_dock_hover(None)
            self.wake()
        return False

    # ---- folders: scroll, drop in, drag out ---------------------------------

    def on_scroll(self, _w, ev):
        if not self.bubble.contains(ev.x, ev.y):
            return False
        if ev.direction == Gdk.ScrollDirection.SMOOTH:
            ok, _dx, dy = ev.get_scroll_deltas()
            self.scroll_acc += dy
        else:
            self.scroll_acc += {Gdk.ScrollDirection.DOWN: 1, Gdk.ScrollDirection.RIGHT: 1,
                                Gdk.ScrollDirection.UP: -1, Gdk.ScrollDirection.LEFT: -1}.get(ev.direction, 0)
        if abs(self.scroll_acc) >= 1:
            self.bubble.scroll(1 if self.scroll_acc > 0 else -1)
            self.scroll_acc = 0.0
        return True

    def save_folders(self):
        home = os.path.expanduser("~")
        self.cfg["folders"] = ["~" + p[len(home):] if p == home or p.startswith(home + "/") else p
                               for p in self.cfg["folders"]]
        path = os.path.join(HERE, "config.json")
        try:
            with open(path) as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = {}
        raw["folders"] = self.cfg["folders"]
        with open(path, "w") as f:
            json.dump(raw, f, indent=2)
        self._rebuild_apps()

    def _folder_slot(self, x):
        """Index in cfg["folders"] a drop at x lands on."""
        dirs = [i for i in self.items if isinstance(i, PlaceItem) and i.key.startswith("dir:")]
        return sum(1 for i in dirs if i.x + i.size / 2 < x)

    def on_drop(self, _w, ctx, x, _y, data, _info, time_):
        known = {os.path.realpath(os.path.expanduser(p)) for p in self.cfg["folders"]}
        at = self._folder_slot(x)
        added = 0
        for uri in data.get_uris() or []:
            path = Gio.File.new_for_uri(uri).get_path()
            if not path or not os.path.isdir(path) or os.path.realpath(path) in known:
                continue
            known.add(os.path.realpath(path))
            self.cfg["folders"].insert(at + added, path)
            added += 1
        if added:
            self.sfx.pop(0)
            self.save_folders()

    def _drag_motion(self, x, y):
        """Pull a docked folder up and away; returns True while dragging."""
        it = self.pressed
        if self.drag is None:
            if not (isinstance(it, PlaceItem) and it.key.startswith("dir:")) or self.press_xy is None:
                return False
            if math.hypot(x - self.press_xy[0], y - self.press_xy[1]) < 8:
                return False
            self.bubble.close(quiet=True)
            self.drag = it
        self.drag_xy = (x, y)
        self.dirty = True
        self.wake()
        return True

    def _on_ball_folder(self, x, y):
        b = self.bubble
        it = b.open_item
        if not (isinstance(it, PlaceItem) and it.key.startswith("dir:")) or b.geom is None:
            return False
        cx, cy, size = b.cat_target()
        return abs(x - cx) <= size / 2 and abs(y - cy) <= size / 2

    def drag_off(self, y):
        """Released far enough above the shelf to leave the dock."""
        return y < self.area.get_allocated_height() - self.shelf_h - 50

    def _drag_end(self, x, y):
        it, self.drag = self.drag, None
        self.press_xy = None
        if it is None:
            return
        path = it.key[len("dir:"):]
        if self.drag_off(y):
            self.cfg["folders"] = [p for p in self.cfg["folders"]
                                   if os.path.realpath(os.path.expanduser(p)) != os.path.realpath(path)]
            self.sfx.unpop(0)
        else:                       # dropped back on the shelf: reorder
            folders = [p for p in self.cfg["folders"]
                       if os.path.realpath(os.path.expanduser(p)) != os.path.realpath(path)]
            others = [i for i in self.items if isinstance(i, PlaceItem) and i.key.startswith("dir:")
                      and i is not it]
            at = sum(1 for i in others if i.x + i.size / 2 < x)
            folders.insert(at, path)
            if folders == [os.path.expanduser(p) for p in self.cfg["folders"]] or \
                    folders == self.cfg["folders"]:
                self.dirty = True
                self.wake()
                return
            self.cfg["folders"] = folders
        self.save_folders()

    def draw_drag(self, cr, h):
        it = self.drag
        x, y = self.drag_xy
        size = self.base * 1.1
        cr.save()
        if self.drag_off(y):
            cr.push_group()
            self.draw_icon_at(cr, it, x - size / 2, y - size / 2, size, self.bounce_h)
            cr.pop_group_to_source()
            cr.paint_with_alpha(0.55)          # about to disappear
        else:
            self.draw_icon_at(cr, it, x - size / 2, y - size / 2, size, self.bounce_h)
        cr.restore()

    def on_press(self, _w, ev):
        if ev.button == 1:
            self.press_xy = (ev.x, ev.y)
            b = self.bubble
            if b.contains(ev.x, ev.y) and self._on_ball_folder(ev.x, ev.y):
                self.pressed = b.open_item       # grab the folder from the ball's centre
            elif b.contains(ev.x, ev.y):
                b.press(ev.x, ev.y)
            else:
                self.pressed = self.item_at(ev.x, ev.y, self.area.get_allocated_height())
            self.dirty = True
            self.wake()
        return True

    def on_release(self, _w, ev):
        if ev.button == 3:      # right-click a drive to eject it
            it = self.item_at(ev.x, ev.y, self.area.get_allocated_height())
            if isinstance(it, PlaceItem):
                it.eject(self)
            return True
        if ev.button != 1:
            return True
        self.press_xy = None
        if self.drag is not None:
            self._drag_end(ev.x, ev.y)
            self.pressed = None
            return True
        if self.bubble.pressed is not None:
            self.bubble.release(ev.x, ev.y)
        else:
            it = self.item_at(ev.x, ev.y, self.area.get_allocated_height())
            if it is not None and it is self.pressed:
                it.activate(self)
        self.pressed = None
        self.dirty = True
        self.wake()
        return True

    def wake(self):
        """Run the frame clock only while something is changing."""
        if self.tick_id is None:
            self.last_t = None
            self.tick_id = self.area.add_tick_callback(self.on_tick)

    def on_tick(self, widget, clock):
        now = time.monotonic()
        dt = 0.016 if self.last_t is None else min(now - self.last_t, 0.05)
        self.last_t = now
        target = 1.0 if (self.hover or self.bubble.open_item is not None) else 0.0
        animating = self.dirty or any(it.bounce_t0 is not None for it in self.items)
        animating |= self.bubble.step(dt)
        if self.mag != target:
            animating = True
            if abs(self.mag - target) > 0.002:
                self.mag = ease(self.mag, target, dt, 0.075 if target else 0.11)
            else:
                self.mag = target
        if animating:
            self.dirty = False
            self._queue_damage()
            return GLib.SOURCE_CONTINUE
        self.tick_id = None
        return GLib.SOURCE_REMOVE

    def _content_bbox(self):
        w, h = self.area.get_allocated_width(), self.area.get_allocated_height()
        if not self.items or w <= 1:
            return 0, 0, w, h
        self.layout(w)
        sx, sy, sw, sh = self.shelf_rect(h)
        x0, x1 = sx - 90, sx + sw + 90                      # room for labels
        y0 = h - (max(self.max, self.base + self.bounce_h) + self.pad_bottom + self.LABEL_H)
        if self.bubble.shape(h) is not None:
            bx0, by0, bx1, _ = self.bubble.bbox()
            x0, x1, y0 = min(x0, bx0 - 90), max(x1, bx1 + 90), min(y0, by0 - BLOB_LABEL)
        return max(0, int(x0)), max(0, int(y0)), min(w, int(x1) + 1), h

    def _queue_damage(self):
        """Redraw only the area the dock and blob occupy (old and new)."""
        if self.drag is not None or getattr(self, "_was_drag", False):
            self._was_drag = self.drag is not None
            self.last_bbox = None
            self.area.queue_draw()
            return
        cur = self._content_bbox()
        prev = self.last_bbox or cur
        x0, y0 = min(cur[0], prev[0]), min(cur[1], prev[1])
        x1, y1 = max(cur[2], prev[2]), max(cur[3], prev[3])
        self.last_bbox = cur
        self.area.queue_draw_area(x0, y0, x1 - x0, y1 - y0)

    # ---- drawing ------------------------------------------------------------

    def _update_input_region(self, height):
        sx, sy, sw, sh = self.shelf_rect(height)
        rects = [(sx, sy, sw, sh)]
        if self.drag is not None:       # keep the pointer while a folder is dragged
            rects.append((0, 0, self.area.get_allocated_width(), height))
        for it in self.items:
            if it.kind != "sep":
                rects.append((it.x, height - self.pad_bottom - it.size, it.size, it.size))
        b = self.bubble.bbox() if self.bubble.opening else None
        if b is not None:
            rects.append((b[0], b[1], b[2] - b[0], b[3] - b[1] + 1))
        rects = [cairo.RectangleInt(int(math.floor(x)), int(math.floor(y)),
                                    int(math.ceil(w)) + 1, int(math.ceil(h)) + 1)
                 for x, y, w, h in rects]
        key = tuple((r.x, r.y, r.width, r.height) for r in rects)
        if key != self.region_key and self.get_window() is not None:
            self.region_key = key
            self.get_window().input_shape_combine_region(cairo.Region(rects), 0, 0)

    def on_draw(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        if not self.items:
            return
        self.layout(w)
        self.bubble.shape(h)
        self._update_input_region(h)

        now = time.monotonic()
        hovered = self.item_at(self.mouse_x, h - 1, h) if self.hover and self.mouse_x is not None \
            and self.drag is None else None
        if hovered is not self.hovered:
            self.hovered = hovered
            self.bubble.on_dock_hover(hovered)

        self.bubble.draw_shadow(cr)
        self.draw_shelf(cr, h)
        riding = []
        for it in self.items:
            if it.kind == "sep":
                self.draw_separator(cr, it, h)
                continue
            lift = self.bounce_offset(it, now)
            if it.kind == "cat" and it.pull.x > 0.001:
                riding.append((it, lift))
            elif it is self.drag:
                cr.push_group()
                self.draw_icon(cr, it, h, lift)
                cr.pop_group_to_source()
                cr.paint_with_alpha(0.3)
            else:
                self.draw_icon(cr, it, h, lift)
            if it.kind == "cat" and it.running:
                self.draw_triangle(cr, it.x + it.size / 2, h)
        for it, lift in riding:
            # Slide the category icon from its slot into the body (and back).
            if it is self.bubble.open_item and self.bubble.geom is not None:
                it.pull_to = self.bubble.cat_target()
            sx, sy = it.x + it.size / 2, h - self.pad_bottom - it.size / 2 - lift
            tx, ty, tsize = it.pull_to or (sx, sy, it.size)
            k = it.pull.x
            size = lerp(it.size, tsize, k)
            self.draw_icon_at(cr, it, lerp(sx, tx, k) - size / 2, lerp(sy, ty, k) - size / 2, size, 0.0)
        self.bubble.draw_contents(cr, w)
        if self.drag is not None:
            self.draw_drag(cr, h)
            return
        if hovered is not None and self.mag > 0.5 and hovered is not self.bubble.open_item:
            self.draw_label(cr, hovered, w, h, self.bounce_offset(hovered, now))

    @staticmethod
    def _make_stripes():
        stripe = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 4)
        sc = cairo.Context(stripe)
        sc.set_source_rgba(1, 1, 1, 0.22)
        sc.rectangle(0, 0, 1, 2)
        sc.fill()
        pat = cairo.SurfacePattern(stripe)
        pat.set_extend(cairo.EXTEND_REPEAT)
        pat.set_filter(cairo.FILTER_NEAREST)
        return pat

    def shelf_path(self, cr, h):
        """Bar outline; when the blob is out, its top edge flows through the blob."""
        x, y, w, sh = self.shelf_rect(h)
        y = round(y) + 0.5
        if self.tick_id is None:          # at rest: snap to pixels for a crisp edge
            x, w = round(x) + 0.5, round(w) - 1
        r = 9
        cr.new_path()
        if self.bubble.geom is not None:
            self.bubble.outline(cr, x, y, w, r, h)
        else:
            cr.move_to(x, h + r)
            cr.line_to(x, y + r)
            cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
            cr.arc(x + w - r, y + r, r, 1.5 * math.pi, 2 * math.pi)
            cr.line_to(x + w, h + r)
        cr.close_path()
        return y

    def draw_shelf(self, cr, h):
        y = self.shelf_path(cr, h)
        path = cr.copy_path()
        g = self.bubble.geom

        # One gradient for the whole body: glassy blob on top, frosted bar below.
        if g is not None:
            top = g["by"] - g["ry"]
            grad = cairo.LinearGradient(0, top, 0, h)
            span = max(h - top, 1)
            grad.add_color_stop_rgba(0.0, 1, 1, 1, 0.93)
            grad.add_color_stop_rgba(min((g["by"] + g["ry"] - top) / span, (y - top) / span),
                                     0.88, 0.90, 0.94, 0.90)
            grad.add_color_stop_rgba((y - top) / span, 1, 1, 1, 0.62)
            grad.add_color_stop_rgba(1.0, 0.88, 0.89, 0.92, 0.52)
        else:
            grad = cairo.LinearGradient(0, y, 0, h)
            grad.add_color_stop_rgba(0.0, 1, 1, 1, 0.62)
            grad.add_color_stop_rgba(0.5, 0.94, 0.95, 0.97, 0.50)
            grad.add_color_stop_rgba(1.0, 0.88, 0.89, 0.92, 0.52)
        cr.set_source(grad)
        cr.fill_preserve()
        cr.save()
        cr.clip()
        cr.set_source(self.stripes)          # Panther/Tiger pinstripes
        cr.paint()
        self.bubble.draw_gloss(cr)
        cr.append_path(path)                 # inner white highlight
        cr.set_line_width(3)
        cr.set_source_rgba(1, 1, 1, 0.70)
        cr.stroke()
        cr.restore()
        cr.append_path(path)                 # soft dark outer edge
        cr.set_line_width(1)
        cr.set_source_rgba(0, 0, 0, 0.30)
        cr.stroke()

    def draw_separator(self, cr, it, h):
        cx = round(it.x + it.size / 2) + 0.5
        top, bot = h - self.shelf_h + 5, h - 4
        cr.set_line_width(1)
        cr.set_source_rgba(0, 0, 0, 0.35)
        cr.move_to(cx, top)
        cr.line_to(cx, bot)
        cr.stroke()
        cr.set_source_rgba(1, 1, 1, 0.75)
        cr.move_to(cx + 1, top)
        cr.line_to(cx + 1, bot)
        cr.stroke()

    def draw_icon(self, cr, it, h, lift):
        self.draw_icon_at(cr, it, it.x, h - self.pad_bottom - it.size - lift, it.size, lift)

    def draw_icon_at(self, cr, it, x, y, size, lift):
        icon = getattr(it, "icon", None)
        if isinstance(it, TrashItem):
            icon = it.icon_full if it.full else it.icon_empty
        if icon is None or not icon.levels:
            return
        # Surfaces carry the device scale, so logical mip size is what matters.
        mip, (surf, (shadow, pad)) = icon.pick(size)
        k = size / mip

        cr.save()
        cr.translate(x, y)
        cr.scale(k, k)
        # Shadow fades as the icon lifts off the shelf.
        cr.set_source_surface(shadow, -pad, -pad + lift / k * 0.15)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint_with_alpha(max(0.0, 1.0 - lift / self.bounce_h * 0.7))
        if it is self.pressed:
            cr.push_group()
            cr.set_source_surface(surf, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_GOOD)
            cr.paint()
            cr.set_operator(cairo.OPERATOR_ATOP)
            cr.set_source_rgba(0, 0, 0, 0.42)
            cr.paint()
            cr.pop_group_to_source()
            cr.paint()
        else:
            cr.set_source_surface(surf, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_GOOD)
            cr.paint()
        cr.restore()

    def draw_triangle(self, cr, cx, h):
        tw, th = 7, 5
        by = h - 1.5
        cr.move_to(cx - tw / 2, by)
        cr.line_to(cx + tw / 2, by)
        cr.line_to(cx, by - th)
        cr.close_path()
        cr.set_source_rgba(1, 1, 1, 0.55)       # faint glow so it reads on any wallpaper
        cr.set_line_width(1.5)
        cr.stroke_preserve()
        cr.set_source_rgba(0.05, 0.05, 0.05, 0.95)
        cr.fill()

    def draw_label(self, cr, it, w, h, lift):
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(self.font)
        layout.set_text(it.label, -1)
        _ink, logical = layout.get_pixel_extents()
        tw, th = logical.width, logical.height
        pw, ph = tw + 22, th + 6
        cx = it.x + it.size / 2
        px = min(max(cx - pw / 2, 4), w - pw - 4)
        py = h - self.pad_bottom - it.size - lift - ph - 8

        cr.save()
        cr.push_group()
        pill(cr, round(px), round(py), round(pw), round(ph))
        cr.set_source_rgba(0, 0, 0, 0.68)
        cr.fill()
        cr.move_to(round(px + (pw - tw) / 2), round(py + (ph - th) / 2) + 1)
        cr.set_source_rgba(0, 0, 0, 0.6)
        PangoCairo.show_layout(cr, layout)
        cr.move_to(round(px + (pw - tw) / 2), round(py + (ph - th) / 2))
        cr.set_source_rgba(1, 1, 1, 1)
        PangoCairo.show_layout(cr, layout)
        cr.pop_group_to_source()
        cr.paint_with_alpha(min(1.0, (self.mag - 0.5) * 3))
        cr.restore()


# --------------------------------------------------------------------------- blob

def ease_out_back(t, k=1.7):
    t -= 1
    return 1 + t * t * ((k + 1) * t + k)


def draw_label_pill(cr, font, text, cx, bottom, width, alpha=1.0):
    """Dark rounded label with white text, centred on cx, clamped to [0, width]."""
    layout = PangoCairo.create_layout(cr)
    layout.set_font_description(font)
    layout.set_text(text, -1)
    _ink, logical = layout.get_pixel_extents()
    tw, th = logical.width, logical.height
    pw, ph = tw + 22, th + 6
    px = round(min(max(cx - pw / 2, 2), width - pw - 2))
    py = round(bottom - ph)
    cr.save()
    cr.push_group()
    pill(cr, px, py, pw, ph)
    cr.set_source_rgba(0, 0, 0, 0.68)
    cr.fill()
    tx, ty = round(px + (pw - tw) / 2), round(py + (ph - th) / 2)
    cr.move_to(tx, ty + 1)
    cr.set_source_rgba(0, 0, 0, 0.6)
    PangoCairo.show_layout(cr, layout)
    cr.move_to(tx, ty)
    cr.set_source_rgba(1, 1, 1, 1)
    PangoCairo.show_layout(cr, layout)
    cr.pop_group_to_source()
    cr.paint_with_alpha(alpha)
    cr.restore()


class Spring:
    """Damped spring, integrated with small fixed substeps for stability."""
    __slots__ = ("x", "v")

    def __init__(self, x=0.0):
        self.x, self.v = x, 0.0

    def step(self, target, dt, k, c):
        n = int(dt / 0.004) + 1
        h = dt / n
        for _ in range(n):
            self.v += (k * (target - self.x) - c * self.v) * h
            self.x += self.v * h

    def near(self, target, eps, veps):
        return abs(self.x - target) < eps and abs(self.v) < veps


BLOB_ICON = 44        # app icon size inside the ball
BLOB_GAP = 16         # space between neighbouring icons
BLOB_PAD = 14         # rim padding around the outermost icons
BLOB_LABEL = 34       # headroom above the ball for the top row's labels
CAT_IN_BALL = 60      # category icon size at the centre of the ball
WAVE_RX, WAVE_RY = 74.0, 62.0   # phase-1 wave crest (half width, height)
PAGE_SLIDE_T = 0.5    # seconds for a folder page to slide in
REST_MS = 260         # pointer still this long over a category -> phase 2


def lerp(a, b, t):
    return a + (b - a) * t


# 2D magnification inside the blob: like the OS X dock, but by distance in the
# plane rather than along a row.
APP_MAG = 1.55                 # hovered icon size multiplier
APP_MAG_R = 1.7 * (44 + 16)    # falloff radius (px): neighbours grow a little


def ring_points(n):
    """App icon offsets in rings around the centre, which holds the category icon."""
    if n <= 0:
        return []
    step = BLOB_ICON + BLOB_GAP
    inner = CAT_IN_BALL / 2 + BLOB_ICON / 2 + BLOB_GAP * 1.1
    if n <= 12:                         # one even ring reads best
        r = max(inner, step / (2 * math.sin(math.pi / n))) if n > 1 else inner
        return [(r * math.cos(-math.pi / 2 + 2 * math.pi * j / n),
                 r * math.sin(-math.pi / 2 + 2 * math.pi * j / n)) for j in range(n)]
    radii, caps = [], []
    while sum(caps) < n:
        r = inner + len(radii) * step
        radii.append(r)
        caps.append(max(1, int(2 * math.pi * r / step)))
    if len(radii) == 1:
        counts = [n]
        radii[0] = max(inner, step / (2 * math.sin(math.pi / n))) if n > 1 else inner
    else:
        total = sum(caps)
        counts = [min(c, round(n * c / total)) for c in caps]
        while sum(counts) < n:
            i = min(range(len(caps)), key=lambda j: (counts[j] / caps[j]) if counts[j] < caps[j] else 9)
            counts[i] += 1
        while sum(counts) > n:
            counts[counts.index(max(counts))] -= 1
    pts = []
    for k, (r, cnt) in enumerate(zip(radii, counts)):
        if cnt <= 0:
            continue
        off = -math.pi / 2 + (math.pi / cnt if k % 2 else 0)
        pts += [(r * math.cos(off + 2 * math.pi * j / cnt),
                 r * math.sin(off + 2 * math.pi * j / cnt)) for j in range(cnt)]
    return pts


SEARCH_CELL = (80.0, 86.0)     # per app in the search blob: icon + name below
SEARCH_PAD = 18.0
SEARCH_ICON = 0.56             # matched icon size, as a fraction of the cell width


def _ellipse_rows(n, cw, ch, aspect):
    """Rows of cells packed inside an ellipse; returns (points, rx, ry)."""
    ry = ch
    while True:
        rx = ry * aspect
        rows = int((2 * ry - 2 * SEARCH_PAD) // ch)
        if rows >= 1:
            ys = [(-(rows - 1) / 2 + i) * ch for i in range(rows)]
            caps = []
            for y in ys:
                yy = min(abs(y) + ch / 2, ry)
                half = rx * math.sqrt(max(0.0, 1 - (yy / ry) ** 2)) - SEARCH_PAD
                caps.append(max(0, int(2 * half // cw)))
            if sum(caps) >= n:
                break
        ry += 4
    # Fill rows in proportion to their width, so the block itself looks round.
    total = sum(caps)
    counts = [min(c, int(c * n / total)) for c in caps]
    order = sorted(range(len(caps)), key=lambda i: abs(ys[i]))
    while sum(counts) < n:
        for i in order:
            if sum(counts) < n and counts[i] < caps[i]:
                counts[i] += 1
    pts = []
    for y, cnt in zip(ys, counts):
        pts += [((j - (cnt - 1) / 2) * cw, y) for j in range(cnt)]
    return pts, rx, ry


def search_layout(n, avail_w, avail_h):
    """Alphabetical grid inside a wide ellipse that fits the given space."""
    cw, ch = SEARCH_CELL
    best = None
    for _shrink in range(10):
        for aspect in (1.7, 1.9, 2.1, 2.4, 2.7, 3.0):
            pts, rx, ry = _ellipse_rows(max(n, 1), cw, ch, aspect)
            best = (pts, rx, ry, cw, ch)
            if 2 * rx <= avail_w and 2 * ry <= avail_h:
                return best
        cw, ch = cw * 0.92, ch * 0.92
    return best


def blob_radius(n):
    pts = ring_points(n)
    reach = max((math.hypot(x, y) for x, y in pts), default=0.0)
    return max(reach + BLOB_ICON * 0.62, CAT_IN_BALL * 0.62) + BLOB_PAD


class Blob:
    """Soft body that grows out of the dock bar in two phases.

    Phase 1 (pointer moving): the bar's top edge swells into a wave under the
    hovered category and the category icon rides up into its crest. The wave
    travels with the pointer from category to category.

    Phase 2 (pointer resting): the wave inflates into a ball. The category icon
    settles at its centre and the apps spread out around it.

    Both phases are one shape: an ellipse merged into the bar by two curves.
    The wave is that ellipse sunk into the bar and attached near its equator;
    the ball is it lifted clear with a liquid neck. A single spring `p` morphs
    between them, so the transition is continuous.
    """

    def __init__(self, dock):
        self.dock = dock
        self.open_item = None
        self.opening = False
        self.full = False               # phase-2 target
        self.show_id = self.hide_id = None
        self.inside = False
        self.hover_app = None
        self.ptr = None                 # pointer inside the blob (screen px)
        self.amag = Spring()            # 0..1 app magnification in/out
        self.pressed = None
        self.slots = []
        self.old_slots = []
        self.xfade = 1.0
        self.slide = 0                  # +1/-1 while a folder page slides in
        self.R_target = 0.0
        self.s = Spring()               # emergence 0..1
        self.p = Spring()               # phase: 0 wave .. 1 ball
        self.R = Spring()               # ball radius
        self.bx = Spring()              # body centre x
        self.fx = Spring()              # where it meets the bar
        self.wob = Spring()             # squash (-) / stretch (+)
        # How far each foot has wrapped round the bar's end corner (px). Sprung,
        # so the foot glides round the corner instead of jumping across it.
        self.wrap = [Spring(), Spring()]
        self.wrap_t = [0.0, 0.0]
        self.gap = dock.max - dock.base + 12
        self.geom = None
        self.A = Spring()               # aspect: rx / ry (1 = round ball)
        self.A.x = 1.0
        self.A_target = 1.0
        # search mode
        self.search = None              # dict while searching
        self.anchor = SearchAnchor()
        self.cell = SEARCH_CELL

    # ---- state -------------------------------------------------------------

    def _cancel(self, attr):
        sid = getattr(self, attr)
        if sid is not None:
            GLib.source_remove(sid)
            setattr(self, attr, None)

    @property
    def active(self):
        return self.open_item is not None and self.opening

    def reveal(self):
        return max(0.0, min(1.0, (self.p.x - 0.55) / 0.45))

    def on_dock_hover(self, item):
        if self.search is not None:      # search owns the blob until it ends
            return
        if item is not None and item is self.open_item:
            self._cancel("hide_id")
            if not self.opening:
                self.opening = True
                self.dock.wake()
            return
        if isinstance(item, CategoryItem):
            self._cancel("hide_id")
            self.show_for(item)
        elif self.active and not self.inside:
            self.schedule_hide()

    def on_rest(self):
        """Pointer has stopped over the open category: inflate to phase 2."""
        if self.search is None and self.active and self.dock.hovered is self.open_item and not self.full:
            self.full = True
            self.dock.sfx.pop(self.cat_index(self.open_item))
            self.dock.wake()

    def cat_index(self, item):
        """Position among the dock's categories; picks that category's notes."""
        cats = [c for c in self.dock.items if isinstance(c, CategoryItem)]
        return cats.index(item) if item in cats else 0

    def schedule_hide(self, delay=380):
        if self.search is not None:
            return
        self._cancel("hide_id")
        self.hide_id = GLib.timeout_add(delay, self._hide_cb)

    def _hide_cb(self):
        self.hide_id = None
        if not self.inside:
            self.close()
        return False

    def toggle(self, item):
        if self.search is not None:
            self.dock.search.close()
        if self.open_item is item and self.opening and self.full:
            self.close()
        else:
            if self.open_item is not item or not self.opening:
                self.show_for(item)
            if not self.full:
                self.dock.sfx.pop(self.cat_index(item))
            self.full = True
            self.dock.wake()

    def show_for(self, item):
        self._cancel("hide_id")
        if isinstance(item, PlaceItem):
            if item is not self.open_item:
                item.page = 0
            item.refresh()
        scale = self.dock.get_scale_factor()
        for app in item.apps:
            if not hasattr(app, "icon"):
                app.icon = app_icon(self.dock, app)
        new_slots = [(app, x, y) for app, (x, y) in zip(item.apps, ring_points(len(item.apps)))]
        self.R_target = blob_radius(len(item.apps))
        cx = item.x + item.size / 2
        if self.open_item is None or self.s.x < 0.05:
            self.s.x = self.s.v = self.p.x = self.p.v = 0.0
            self.R.x, self.R.v = self.R_target, 0.0
            self.bx.x, self.bx.v = cx, 0.0
            self.fx.x, self.fx.v = cx, 0.0
            self.wob.x = self.wob.v = 0.0
            self.old_slots, self.xfade = [], 1.0
        elif item is not self.open_item:
            if self.reveal() > 0.05:
                self.old_slots, self.xfade = self.slots, 0.0
        if item is not self.open_item or not self.opening:
            self.dock.sfx.wave(self.cat_index(item))
        self.slots = new_slots
        self.full = False
        self.open_item = item
        self.opening = True
        self.hover_app = self.pressed = None
        self.dock.dirty = True
        self.dock.wake()

    def apps_changed(self):
        """The app list changed under an open blob: follow it."""
        item = self.open_item
        if item is None:
            return
        if self.search is not None:
            self._search_slots()
            self.set_query(self.search["q"])
            return
        if item not in self.dock.items:
            self.close(quiet=True)
            return
        if [a for a, _x, _y in self.slots] == item.apps:
            return
        scale = self.dock.get_scale_factor()
        for app in item.apps:
            if not hasattr(app, "icon"):
                app.icon = app_icon(self.dock, app)
        if self.reveal() > 0.05:
            self.old_slots, self.xfade = self.slots, 0.0
        self.slots = [(app, x, y) for app, (x, y) in zip(item.apps, ring_points(len(item.apps)))]
        self.R_target = blob_radius(len(item.apps))
        self.hover_app = self.pressed = None

    # ---- search mode -----------------------------------------------------------

    def _center_anchor(self):
        sx, _sy, sw, _sh = self.dock.shelf_rect(self.dock.area.get_allocated_height())
        a = self.anchor
        a.size = self.dock.base
        a.x = sx + sw / 2 - a.size / 2

    def _search_slots(self):
        apps = self.dock.all_apps()
        pts, rx, ry, cw, ch = self.dock.search_geom
        if len(pts) != len(apps):
            self.dock._fit_height()
            pts, rx, ry, cw, ch = self.dock.search_geom
        for a in apps:
            app_icon(self.dock, a)
        self.cell = (cw, ch)
        self.anchor.apps = apps
        self.slots = [(a, x, y) for a, (x, y) in zip(apps, pts)]
        self.R_target = ry
        self.A_target = rx / ry
        st = self.search
        for a in apps:
            st["m"].setdefault(a.id, Spring())
            st["sel"].setdefault(a.id, Spring())
            if not st["q"]:
                st["m"][a.id].x = 1.0

    def begin_search(self):
        self._cancel("hide_id")
        self._center_anchor()
        self.search = dict(q="", matches=[], idx=0, hl={}, m={}, sel={}, labels={})
        was_open = self.open_item is not None and self.s.x > 0.05
        if was_open and self.reveal() > 0.05:
            self.old_slots, self.xfade = self.slots, 0.0
        self._search_slots()
        cx = self.anchor.x + self.anchor.size / 2
        if not was_open:
            self.s.x = self.s.v = self.p.x = self.p.v = 0.0
            self.R.x, self.R.v = self.R_target, 0.0
            self.A.x, self.A.v = self.A_target, 0.0
            self.bx.x, self.bx.v = cx, 0.0
            self.fx.x, self.fx.v = cx, 0.0
            self.wob.x = self.wob.v = 0.0
            self.old_slots, self.xfade = [], 1.0
        self.open_item = self.anchor
        self.opening = self.full = True
        self.hover_app = self.pressed = None
        self.dock.sfx.pop(len(sounds.PENTA) - 1)          # highest bloop
        self.dock.dirty = True
        self.dock.wake()

    def end_search(self, quiet=False):
        if self.search is None:
            return
        self.search = None
        self.A_target = 1.0
        self.close(quiet=quiet)

    @staticmethod
    def _rank(label, q):
        lo = label.lower()
        i = lo.find(q)
        if i < 0:
            return None
        word = i == 0 or not lo[i - 1].isalnum()
        return (0 if i == 0 else 1 if word else 2, i, lo), i

    def set_query(self, q):
        st = self.search
        if st is None:
            return
        q = q.strip().lower()
        prev = self.search_selected()
        st["q"], st["hl"], st["labels"] = q, {}, {}
        ranked = []
        for app, _x, _y in self.slots:
            r = self._rank(app.label, q) if q else None
            if r is not None:
                ranked.append((r[0], app))
                st["hl"][app.id] = (r[1], r[1] + len(q))
        ranked.sort(key=lambda e: e[0])
        st["matches"] = [a for _r, a in ranked]
        # keep the selection while it still matches; otherwise the best match
        st["idx"] = st["matches"].index(prev) if prev in st["matches"] else 0
        self.dock.dirty = True
        self.dock.wake()

    def search_selected(self):
        st = self.search
        if st is None or not st["matches"]:
            return None
        return st["matches"][st["idx"] % len(st["matches"])]

    def search_count(self):
        return len(self.search["matches"]) if self.search else 0

    def search_cycle(self, d):
        st = self.search
        if st is None or len(st["matches"]) < 2:
            return
        st["idx"] = (st["idx"] + d) % len(st["matches"])
        self.dock.sfx.hover(st["idx"])
        self.dock.dirty = True
        self.dock.wake()

    def close(self, quiet=False):
        self._cancel("hide_id")
        if self.open_item is None or not self.opening:
            return
        if not quiet and self.reveal() > 0.3:
            self.dock.sfx.unpop(self.cat_index(self.open_item))
        self.opening = False
        self.full = False
        self.inside = False
        self.hover_app = self.pressed = None
        self.dock.wake()

    # ---- physics -----------------------------------------------------------

    def step(self, dt):
        """Advance the springs; returns True while anything is moving."""
        moving = False
        for it in self.dock.items:          # category icons riding in/out of the body
            if isinstance(it, CategoryItem):
                tgt = 1.0 if (it is self.open_item and self.opening) else 0.0
                if not it.pull.near(tgt, 0.002, 0.01):
                    it.pull.step(tgt, dt, 300, 27)
                    moving = True
                elif it.pull.x != tgt:
                    it.pull.x, it.pull.v = tgt, 0.0
                    moving = True
        tgt = 1.0 if (self.inside and self.ptr is not None and self.reveal() > 0.8) else 0.0
        if not self.amag.near(tgt, 0.002, 0.01):
            self.amag.step(tgt, dt, 260, 30)
            moving = True
        elif self.amag.x != tgt:
            self.amag.x, self.amag.v = tgt, 0.0
            moving = True
        st = self.search
        if st is not None:
            sel = self.search_selected()
            for app, _x, _y in self.slots:
                mt = 1.0 if (not st["q"] or app.id in st["hl"]) else 0.0
                for sp, t, k, c in ((st["m"][app.id], mt, 300, 26),
                                    (st["sel"][app.id], 1.0 if app is sel else 0.0, 340, 20)):
                    if not sp.near(t, 0.002, 0.01):
                        sp.step(t, dt, k, c)
                        moving = True
                    elif sp.x != t:
                        sp.x, sp.v = t, 0.0
            self._center_anchor()
        if not self.A.near(self.A_target, 0.001, 0.005):
            self.A.step(self.A_target, dt, 200, 22)
            moving = True
        it = self.open_item
        if it is None:
            return moving
        w = self.dock.area.get_allocated_width()
        cx = it.x + it.size / 2
        if self.opening:
            self.s.step(1.0, dt, 260, 20)
            if self.full:
                self.p.step(1.0, dt, 170, 12)       # jelly: overshoot, wobble, settle
            else:
                self.p.step(0.0, dt, 260, 28)
            self.R.step(self.R_target, dt, 200, 22)
            pe = max(0.0, min(1.0, self.p.x))
            half = lerp(WAVE_RX, self.R.x * self.A.x, pe)
            sx, _sy, sw, _sh = self.dock.shelf_rect(self.dock.area.get_allocated_height())
            # A wave is part of the bar, so it must stay on the bar; the ball
            # floats above it and only has to stay on screen.
            lo = lerp(sx + WAVE_RX + 6, half + 6, pe)
            hi = lerp(sx + sw - WAVE_RX - 6, w - half - 6, pe)
            self.bx.step(min(max(cx, lo), max(lo, hi)), dt, 150, 22)
        else:
            self.s.step(0.0, dt, 380, 36)
            self.p.step(0.0, dt, 380, 36)
            self.bx.step(cx, dt, 260, 30)
        self.fx.step(cx, dt, 320, 34)
        if self.xfade < 1.0:
            self.xfade = min(1.0, self.xfade + dt / (PAGE_SLIDE_T if self.slide else 0.18))
            if self.xfade >= 1.0:
                self.slide = 0
        # Stretch tall while inflating, squash while sliding; then jiggle back.
        stretch = max(-0.2, min(0.2, self.p.v * 0.05 - abs(self.bx.v) * 0.00025))
        self.wob.step(stretch, dt, 260, 9)
        for sp, t in zip(self.wrap, self.wrap_t):
            sp.step(t, dt, 1400, 75)          # ~25 ms: smooths, no visible lag

        if not self.opening and self.s.x < 0.02:
            self.open_item = None
            self.geom = None
            self.wrap = [Spring(), Spring()]
            self.wrap_t = [0.0, 0.0]
            return True
        p_t = 1.0 if (self.opening and self.full) else 0.0
        settled = (self.s.near(1.0 if self.opening else 0.0, 0.002, 0.01)
                   and self.p.near(p_t, 0.002, 0.01)
                   and abs(self.bx.v) < 0.5 and abs(self.fx.v) < 0.5 and abs(self.R.v) < 0.3
                   and abs(self.wob.x) < 0.002 and abs(self.wob.v) < 0.02 and self.xfade >= 1.0
                   and all(sp.near(t, 0.05, 0.5) for sp, t in zip(self.wrap, self.wrap_t)))
        return moving or not settled

    # ---- geometry ----------------------------------------------------------

    def shape(self, h):
        """Body geometry for this frame (None when fully closed)."""
        if self.open_item is None or self.s.x <= 0.005:
            self.geom = None
            return None
        d = self.dock
        y_bar = h - d.shelf_h + 0.5
        s = max(0.0, min(self.s.x, 1.2))
        p = max(-0.1, min(self.p.x, 1.15))
        pe = max(0.0, min(p, 1.0))
        st = self.wob.x
        R = self.R.x
        rx_w, ry_w = WAVE_RX * (0.55 + 0.45 * s), WAVE_RY * s
        rx = lerp(rx_w, R * self.A.x * s, p) * (1 - 0.5 * st)
        ry = max(lerp(ry_w, R * s, p) * (1 + st), 1.0)
        lift = lerp(-0.85 * ry_w, self.gap * s, pe)
        by = y_bar - lift - ry
        phi = lerp(2.15, 1.0, pe)
        fw = lerp(rx * 2.7, rx * 0.95 + 0.30 * max(lift, 0.0), pe) * min(1.0, s)
        L = lerp(rx * 0.75, min(max(lift, 0.0) * 0.42 + 6, min(rx, ry) * 0.45), pe)
        foot = lerp(0.45, 0.6, pe)                 # how far the flank runs flat along the bar
        self.geom = dict(bx=self.bx.x, by=by, rx=rx, ry=ry, fx=self.fx.x, fw=fw, phi=phi,
                         L=L, foot=foot, lift=lift, y_bar=y_bar, s=s, p=p, pe=pe)
        return self.geom

    def outline(self, cr, x, y, w, r, h):
        """Whole bar outline with the body merged into its top edge.

        Each flank starts at a foot point on the bar's outline. Normally that is
        on the flat top; near either end of the bar the foot slides round the
        rounded corner and down the end, so the bar's end itself becomes the
        wave's flank instead of meeting it in a notch.
        """
        g = self.geom
        bx, by, rx, ry, phi, L = g["bx"], g["by"], g["rx"], g["ry"], g["phi"], g["L"]
        fw = max(4.0, g["fw"])
        side_max = max(0.0, (h - y) - r - 4)
        quarter = math.pi * r / 2

        def foot(overflow, left):
            """Point + forward tangent `overflow` px past the corner's start."""
            ccx = x + r if left else x + w - r
            if overflow <= 0:                          # on the flat top
                px = (x + r - overflow) if left else (x + w - r + overflow)
                return (px, y), ((1, 0) if left else (1, 0)), "top", None
            if overflow <= quarter:                    # on the corner arc
                if left:
                    a = 1.5 * math.pi - overflow / r
                else:
                    a = 1.5 * math.pi + overflow / r
                pt = (ccx + r * math.cos(a), y + r + r * math.sin(a))
                return pt, (-math.sin(a), math.cos(a)), "arc", a
            d2 = min(overflow - quarter, side_max)     # down the bar's end
            pt = (x, y + r + d2) if left else (x + w, y + r + d2)
            return pt, ((0, -1) if left else (0, 1)), "side", None

        tl, tr = math.pi / 2 + phi, math.pi / 2 - phi

        def ept(t):
            return bx + rx * math.cos(t), by + ry * math.sin(t)

        def etan(t):
            dx, dy = -rx * math.sin(t), ry * math.cos(t)
            n = math.hypot(dx, dy) or 1.0
            return dx / n, dy / n

        lx_want, rx_want = g["fx"] - fw, g["fx"] + fw
        reach = quarter + side_max * 0.45

        def soft(ov):
            # Past the corner start, ease toward a limit instead of clamping,
            # so the foot glides round the corner rather than snapping.
            # Only wrap the corner once the wave has real height; a nearly flat
            # wave leaves the bar's rounded corner untouched. A risen wave
            # reaches for the corner a little early, so no dip forms beside it.
            e = max(0.0, min(1.0, (g["ry"] - 1.0) / 14.0))
            e = e * e * (3 - 2 * e)
            ov += 16.0 * e
            if ov <= 0:
                return ov
            return reach * math.tanh(ov / reach) * e

        def wrapped(side, ov):
            # Lag only the wrapped (positive) part; position on the flat top
            # follows the shape directly, so the result stays continuous.
            d = soft(ov)
            self.wrap_t[side] = max(d, 0.0)
            return min(d, 0.0) + self.wrap[side].x

        (P, T, where, a) = foot(wrapped(0, (x + r) - lx_want), True)
        fl = max(g["fx"] - P[0], 6.0)

        # Left end of the bar, up to the left foot.
        cr.move_to(x, h + r)
        if where == "side":
            cr.line_to(*P)
        else:
            cr.line_to(x, y + r)
            if where == "arc":
                cr.arc(x + r, y + r, r, math.pi, a)
            else:
                cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
                cr.line_to(*P)
        al, ar = ept(tl), ept(tr)
        tlx, tly = etan(tl)
        trx, try_ = etan(tr)
        # Control points never reach past the feet, or the flank bulges out
        # beyond the bar's end and shows a horn at the corner.
        # When the handle would reach past the foot, shorten it along the
        # ellipse's tangent (never move it sideways: that breaks the join).
        Ll = L
        if tlx > 1e-3 and al[0] - tlx * Ll < P[0]:
            Ll = max((al[0] - P[0]) / tlx, L * 0.25)
        c2 = (al[0] - tlx * Ll, min(al[1] - tly * Ll, y - 1))
        k = fl * g["foot"]
        cr.curve_to(P[0] + T[0] * k, P[1] + T[1] * k, c2[0], c2[1], al[0], al[1])

        cr.save()
        cr.translate(bx, by)
        cr.scale(rx, ry)
        cr.arc(0, 0, 1, tl, tr + 2 * math.pi)
        cr.restore()

        (Q, U, where, a) = foot(wrapped(1, rx_want - (x + w - r)), False)
        fr = max(Q[0] - g["fx"], 6.0)
        Lr = L
        if trx > 1e-3 and ar[0] + trx * Lr > Q[0]:
            Lr = max((Q[0] - ar[0]) / trx, L * 0.25)
        c3 = (ar[0] + trx * Lr, min(ar[1] + try_ * Lr, y - 1))
        k = fr * g["foot"]
        cr.curve_to(c3[0], c3[1], Q[0] - U[0] * k, Q[1] - U[1] * k, Q[0], Q[1])
        if where == "top":
            cr.line_to(x + w - r, y)
            cr.arc(x + w - r, y + r, r, 1.5 * math.pi, 2 * math.pi)
        elif where == "arc":
            cr.arc(x + w - r, y + r, r, a, 2 * math.pi)
        cr.line_to(x + w, h + r)

    def is_ball(self):
        return self.geom is not None and self.opening and self.geom["pe"] > 0.6

    def contains(self, x, y):
        """Pointer is inside the inflated ball (the dock then holds still)."""
        if not self.is_ball():
            return False
        g = self.geom
        ex = (x - g["bx"]) / (g["rx"] + 6)
        ey = (y - g["by"]) / (g["ry"] + 6)
        if ex * ex + ey * ey <= 1:
            return True
        return abs(x - g["fx"]) <= g["fw"] + 6 and g["by"] <= y <= g["y_bar"] - 1

    def bbox(self):
        g = self.geom
        if g is None:
            return None
        x0 = min(g["bx"] - g["rx"], g["fx"] - g["fw"]) - 8
        x1 = max(g["bx"] + g["rx"], g["fx"] + g["fw"]) + 8
        return x0, g["by"] - g["ry"] - 6, x1, g["y_bar"]

    def cat_target(self):
        """Where the open category icon sits inside the body: (cx, cy, size)."""
        g = self.geom
        it = self.open_item
        return g["bx"], g["by"], lerp(it.size, CAT_IN_BALL * g["s"], g["pe"])

    def _slot_xy(self, dx, dy):
        g = self.geom
        R = self.R.x or 1.0
        spread = 0.55 + 0.45 * self.reveal()
        return (g["bx"] + dx * g["rx"] / (R * self.A.x) * spread,
                g["by"] + dy * g["ry"] / R * spread)

    def app_layout(self):
        """[(app, cx, cy, size)]: icons keep their slots and only grow with
        closeness to the pointer (2D, cosine falloff like the OS X dock)."""
        g = self.geom
        if g is None:
            return []
        rv = self.reveal()
        st = self.search
        if st is not None:
            cw, ch = self.cell
            base = cw * SEARCH_ICON * (0.6 + 0.4 * rv)
            mag, mag_r = 1.22, 1.4 * cw
        else:
            base = BLOB_ICON * (0.6 + 0.4 * rv)
            mag, mag_r = APP_MAG, APP_MAG_R
        m = self.amag.x
        out = []
        off = self.slide_offset(False) if st is None else 0.0
        for app, dx, dy in self.slots:
            ax, ay = self._slot_xy(dx, dy)
            ay += off
            size = base
            if st is not None:
                # matches full size, the rest small; the selection a bit bigger
                size = base * lerp(0.58, 1.0, st["m"][app.id].x) * (1 + 0.24 * st["sel"][app.id].x)
                ay -= ch * 0.14                    # room for the name underneath
            if m > 0.001 and self.ptr is not None:
                vx, vy = ax - self.ptr[0], ay - self.ptr[1]
                d = math.hypot(vx, vy)
                f = (math.cos(math.pi * min(d / mag_r, 1.0)) + 1) / 2
                size *= 1 + (mag - 1) * f * m
            out.append((app, ax, ay, size))
        return out

    def app_at(self, x, y):
        if self.geom is None or self.reveal() < 0.8:
            return None
        best, best_d = None, None
        for app, ax, ay, size in self.app_layout():
            half = size / 2 + BLOB_GAP / 2
            if abs(x - ax) <= half and abs(y - ay) <= half:
                d = math.hypot(x - ax, y - ay)
                if best is None or d < best_d:
                    best, best_d = app, d
        return best

    # ---- pointer -----------------------------------------------------------

    def pointer(self, x, y):
        self.inside = True
        self._cancel("hide_id")
        self.ptr = (x, y)
        app = self.app_at(x, y)
        if app is not self.hover_app:
            self.hover_app = app
            if app is not None and self.reveal() >= 0.98:
                apps = [a for a, _x, _y in self.slots]
                self.dock.sfx.hover(apps.index(app) if app in apps else 0)
        if self.reveal() > 0.8:                 # the lens follows the pointer
            self.dock.dirty = True
            self.dock.wake()

    def pointer_out(self):
        if self.inside:
            self.inside = False
            if self.hover_app is not None:
                self.hover_app = None
            self.dock.dirty = True
            self.dock.wake()
            if self.dock.hovered is not self.open_item:
                self.schedule_hide()

    def slide_offset(self, old):
        """Vertical shift of the outgoing (old) or incoming page while sliding."""
        if not self.slide or self.geom is None:
            return 0.0
        t = self.xfade
        t = t * t * (3 - 2 * t)                  # smoothstep
        dist = self.geom["ry"] * 0.9
        return -self.slide * dist * t if old else self.slide * dist * (1 - t)

    def scroll(self, d):
        """Page through a folder stack that holds more than one bubble's worth."""
        item = self.open_item
        if self.xfade < 0.85:                    # let the current slide finish
            return
        if self.search is not None or not isinstance(item, PlaceItem) or not item.set_page(item.page + d):
            return
        self.old_slots, self.xfade, self.slide = self.slots, 0.0, d
        self.slots = [(a, x, y) for a, (x, y) in zip(item.apps, ring_points(len(item.apps)))]
        self.R_target = blob_radius(len(item.apps))
        self.hover_app = self.pressed = None
        for a in item.apps:
            app_icon(self.dock, a)
        self.dock.sfx.hover(item.page)
        self.dock.dirty = True
        self.dock.wake()

    def press(self, x, y):
        self.pressed = self.app_at(x, y)

    def release(self, x, y):
        app = self.app_at(x, y)
        if app is not None and app is self.pressed:
            self.dock.activate_app(app)
        self.pressed = None

    # ---- drawing -----------------------------------------------------------

    def draw_shadow(self, cr):
        g = self.geom
        if g is None or g["pe"] <= 0.02:
            return
        k = g["pe"] * g["s"]
        for i, a in enumerate((0.05, 0.045, 0.035, 0.025)):
            cr.save()
            cr.translate(g["bx"], g["by"] + 4)
            cr.scale(g["rx"] + 1 + i * 1.6, g["ry"] + 1 + i * 1.6)
            cr.arc(0, 0, 1, 0, 2 * math.pi)
            cr.restore()
            cr.set_source_rgba(0, 0, 0, a * k)
            cr.fill()

    def draw_gloss(self, cr):
        g = self.geom
        if g is None:
            return
        cr.save()
        cr.translate(g["bx"], g["by"] - g["ry"] * 0.42)
        cr.scale(g["rx"] * 0.82, g["ry"] * 0.52)
        cr.arc(0, 0, 1, 0, 2 * math.pi)
        cr.restore()
        gg = cairo.LinearGradient(0, g["by"] - g["ry"], 0, g["by"])
        gg.add_color_stop_rgba(0.0, 1, 1, 1, 0.65 * g["pe"])
        gg.add_color_stop_rgba(1.0, 1, 1, 1, 0.0)
        cr.set_source(gg)
        cr.fill()

    def draw_contents(self, cr, width):
        g = self.geom
        if g is None:
            return
        if self.search is not None:
            return self._draw_search(cr, width)
        rv = self.reveal()
        label = None
        if rv > 0.0:
            cr.save()
            if self.slide:                       # pages slide inside the ball, not past it
                cr.save()
                cr.translate(g["bx"], g["by"])
                cr.scale(max(1.0, g["rx"] - 4), max(1.0, g["ry"] - 4))
                cr.arc(0, 0, 1, 0, 2 * math.pi)
                cr.restore()
                cr.clip()
            if self.old_slots and self.xfade < 1.0:
                cr.push_group()
                off = self.slide_offset(True)
                for app, dx, dy in self.old_slots:
                    ax, ay = self._slot_xy(dx, dy)
                    ay += off
                    self._draw_icon(cr, app, ax - BLOB_ICON / 2, ay - BLOB_ICON / 2, BLOB_ICON, False)
                cr.pop_group_to_source()
                cr.paint_with_alpha(rv * (1 - self.xfade))
            cr.push_group()
            # Smallest first, so magnified icons sit on top of their neighbours.
            for app, ax, ay, size in sorted(self.app_layout(), key=lambda e: e[3]):
                self._draw_icon(cr, app, ax - size / 2, ay - size / 2, size, app is self.pressed)
                if app.running:
                    tw, th, ty = 6, 4, ay + size / 2 + 6
                    cr.move_to(ax - tw / 2, ty)
                    cr.line_to(ax + tw / 2, ty)
                    cr.line_to(ax, ty - th)
                    cr.close_path()
                    cr.set_source_rgba(0.05, 0.05, 0.05, 0.9)
                    cr.fill()
                if app is self.hover_app:
                    label = (app.label, ax, ay - size / 2 - 4)
            cr.pop_group_to_source()
            cr.paint_with_alpha(rv * (1.0 if self.slide else self.xfade))
            cr.restore()
        it = self.open_item
        if isinstance(it, PlaceItem) and it.pages > 1 and rv > 0.5:
            n = len(it.files)
            a, b = it.page * MAX_STACK + 1, min(n, (it.page + 1) * MAX_STACK)
            arrows = ("▲ " if it.page > 0 else "") + f"{a}–{b} of {n}" + (" ▼" if it.page < it.pages - 1 else "")
            draw_label_pill(cr, self.small_font(), arrows, g["bx"], g["by"] + g["ry"] - 4,
                            width, min(1.0, (rv - 0.5) * 2))
        if label is not None and rv >= 0.98 and self.xfade >= 1.0:
            draw_label_pill(cr, self.dock.font, label[0], label[1], label[2], width)
        elif self.opening and self.search is None and g["pe"] < 0.3 and g["s"] > 0.8:
            # Phase 1: name the category above the crest of the wave.
            draw_label_pill(cr, self.dock.font, self.open_item.label, g["bx"],
                            g["by"] - g["ry"] - 6, width, min(1.0, (g["s"] - 0.8) * 5))

    def _draw_search(self, cr, width):
        rv = self.reveal()
        if rv <= 0.0:
            return
        st = self.search
        sel = self.search_selected()
        lay = self.app_layout()
        # non-matches under matches under the selection
        lay.sort(key=lambda e: (st["sel"][e[0].id].x, st["m"][e[0].id].x, e[3]))
        cr.push_group()
        for app, ax, ay, size in lay:
            mt, sl = st["m"][app.id].x, st["sel"][app.id].x
            if sl > 0.01:                       # Tiger-blue glow behind the selection
                r = size * 0.78
                glow = cairo.RadialGradient(ax, ay, size * 0.2, ax, ay, r)
                glow.add_color_stop_rgba(0, 0.36, 0.62, 1.0, 0.55 * sl)
                glow.add_color_stop_rgba(1, 0.36, 0.62, 1.0, 0.0)
                cr.set_source(glow)
                cr.arc(ax, ay, r, 0, 2 * math.pi)
                cr.fill()
            self._draw_icon(cr, app, ax - size / 2, ay - size / 2, size, app is self.pressed,
                            color=max(0.0, min(1.0, mt)))
        # names: matches only (plus whatever the pointer is on)
        cw, ch = self.cell
        for app, ax, ay, size in lay:
            mt = st["m"][app.id].x
            show = st["q"] and app.id in st["hl"]
            if not show and app is not self.hover_app:
                continue
            a = max(0.0, min(1.0, mt)) if show else 1.0
            self._draw_name(cr, app, ax, ay + size / 2 + 3, cw * 1.3, a,
                            st["hl"].get(app.id), app is sel)
        cr.pop_group_to_source()
        cr.paint_with_alpha(rv * self.xfade)
        if self.old_slots and self.xfade < 1.0:
            cr.push_group()
            for app, dx, dy in self.old_slots:
                ax, ay = self._slot_xy(dx, dy)
                self._draw_icon(cr, app, ax - BLOB_ICON / 2, ay - BLOB_ICON / 2, BLOB_ICON, False)
            cr.pop_group_to_source()
            cr.paint_with_alpha(rv * (1 - self.xfade))

    def _draw_name(self, cr, app, cx, top, max_w, alpha, hl, selected):
        """Small name pill under an icon; the matched letters are highlighted."""
        if alpha <= 0.02:
            return
        key = (app.id, hl, selected)
        cache = self.search["labels"]
        layout = cache.get(key)
        if layout is None:
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(self.small_font())
            t = app.label
            if hl:
                a, b = hl
                esc = GLib.markup_escape_text
                color = "#ffffff" if selected else "#ffd75e"
                markup = (esc(t[:a]) + f'<span foreground="{color}" weight="heavy" underline="single">'
                          + esc(t[a:b]) + "</span>" + esc(t[b:]))
                layout.set_markup(markup, -1)
            else:
                layout.set_text(t, -1)
            layout.set_width(int(max_w * Pango.SCALE))
            layout.set_ellipsize(Pango.EllipsizeMode.END)
            layout.set_alignment(Pango.Alignment.CENTER)
            cache[key] = layout
        else:
            PangoCairo.update_layout(cr, layout)
        _ink, lg = layout.get_pixel_extents()
        tw, th = lg.width, lg.height
        pw, ph = tw + 12, th + 3
        px, py = round(cx - pw / 2), round(top)
        cr.save()
        cr.push_group()
        pill(cr, px, py, pw, ph)
        if selected:
            cr.set_source_rgba(0.16, 0.42, 0.88, 0.96)
        else:
            cr.set_source_rgba(0, 0, 0, 0.62)
        cr.fill()
        # The layout is laid out across max_w and centred in it.
        lx = cx - max_w / 2
        cr.move_to(lx, py + 1.5 + 1)
        cr.set_source_rgba(0, 0, 0, 0.5)
        PangoCairo.show_layout(cr, layout)
        cr.move_to(lx, py + 1.5)
        cr.set_source_rgba(1, 1, 1, 1)
        PangoCairo.show_layout(cr, layout)
        cr.pop_group_to_source()
        cr.paint_with_alpha(alpha)
        cr.restore()

    def small_font(self):
        if not hasattr(self, "_small_font"):
            self._small_font = Pango.FontDescription.from_string("Sans Bold 8")
        return self._small_font

    @staticmethod
    def _draw_icon(cr, app, x, y, size, pressed, color=1.0):
        icon = app.icon
        if not icon.levels:
            return
        mip, (surf, (shadow, pad)) = icon.pick(size)
        k = size / mip
        cr.save()
        cr.translate(x, y)
        cr.scale(k, k)
        cr.set_source_surface(shadow, -pad, -pad)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint_with_alpha(0.8 * (0.35 + 0.65 * color))
        if color < 0.999:                          # black & white, a little faded
            cr.set_source_surface(icon.gray(mip), 0, 0)
            cr.get_source().set_filter(cairo.FILTER_GOOD)
            cr.paint_with_alpha((1 - color) * 0.62)
            if color <= 0.001:
                cr.restore()
                return
            cr.set_source_surface(surf, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_GOOD)
            cr.paint_with_alpha(color)
            cr.restore()
            return
        cr.set_source_surface(surf, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        if pressed:
            cr.push_group()
            cr.paint()
            cr.set_operator(cairo.OPERATOR_ATOP)
            cr.set_source_rgba(0, 0, 0, 0.42)
            cr.paint()
            cr.pop_group_to_source()
        cr.paint()
        cr.restore()


# --------------------------------------------------------------------------- KWin

_script_n = 0


def kwin_activate(item):
    """Raise the app's window through a one-shot KWin script.

    The script calls back NoWindow on our bus name if it finds nothing, so a
    running-but-windowless app (e.g. a tray app) still gets a new window.
    """
    global _script_n
    _script_n += 1
    plugin = f"tigerdock_activate_{os.getpid()}_{_script_n}"
    names = sorted({item.id, item.id.lower(), *item.names})
    js = f"""
const ids = {json.dumps(names)};
const wanted = new Set(ids.map(s => s.toLowerCase()));
let hit = [];
for (const w of workspace.windowList()) {{
    if (!w.normalWindow) continue;
    const keys = [w.desktopFileName, w.resourceClass, w.resourceName]
        .map(s => String(s || "").toLowerCase());
    if (keys.some(k => wanted.has(k))) hit.push(w);
}}
if (hit.length) {{
    const order = workspace.stackingOrder;
    hit.sort((a, b) => order.indexOf(b) - order.indexOf(a));
    for (const w of hit) if (w.minimized) w.minimized = false;
    workspace.activeWindow = hit[0];
}} else {{
    callDBus("{BUS_NAME}", "{BUS_PATH}", "{BUS_NAME}", "NoWindow", {json.dumps(item.id)});
}}
"""
    path = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), plugin + ".js")
    try:
        with open(path, "w") as f:
            f.write(js)
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        sid = bus.call_sync("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "loadScript",
                            GLib.Variant("(ss)", (path, plugin)), GLib.VariantType("(i)"),
                            Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
        if sid < 0:
            return False
        bus.call_sync("org.kde.KWin", f"/Scripting/Script{sid}", "org.kde.kwin.Script", "run",
                      None, None, Gio.DBusCallFlags.NONE, 1000, None)
    except (GLib.Error, OSError) as e:
        print("dock: KWin activation unavailable:", e, file=sys.stderr)
        return False

    def cleanup():
        try:
            bus.call_sync("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript",
                          GLib.Variant("(s)", (plugin,)), None, Gio.DBusCallFlags.NONE, 1000, None)
        except GLib.Error:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass
        return False
    GLib.timeout_add(1500, cleanup)
    return True


# --------------------------------------------------------------------------- main

META_KEY = 0x01000022          # Qt::Key_Meta: the Meta key tapped on its own


def register_meta_shortcut(bus, on_press):
    """Bind a lone Meta tap to the search via KDE's global shortcut daemon.

    Plasma 6 handles modifier-only shortcuts in kglobalaccel (KWin's old
    kwinrc [ModifierOnlyShortcuts] is gone), so we register our own action
    there, give it the Meta key and listen for its press signal.
    """
    action = ["tiger-dock", "search", "Tiger Dock", "Search Applications"]

    def call(method, sig, args, ret=None, path="/kglobalaccel", iface="org.kde.KGlobalAccel"):
        return bus.call_sync("org.kde.kglobalaccel", path, iface, method,
                             GLib.Variant(sig, args) if sig else None,
                             GLib.VariantType(ret) if ret else None,
                             Gio.DBusCallFlags.NONE, 3000, None)
    try:
        call("doRegister", "(as)", (action,))
        # SetPresent (2) | NoAutoloading (4): Meta, regardless of saved config
        call("setShortcutKeys", "(asa(ai)u)", (action, [([META_KEY, 0, 0, 0],)], 2 | 4), "(a(ai))")
        path = call("getComponent", "(s)", (action[0],), "(o)").unpack()[0]
    except GLib.Error as e:
        print("dock: could not bind Meta:", e.message, file=sys.stderr)
        return

    def on_signal(_c, _sender, _path, _iface, _signal, params):
        comp, name, _ts = params.unpack()
        if comp == action[0] and name == action[1]:
            on_press()

    bus.signal_subscribe("org.kde.kglobalaccel", "org.kde.kglobalaccel.Component",
                         "globalShortcutPressed", path, None, Gio.DBusSignalFlags.NONE,
                         on_signal)
    keys = call("shortcutKeys", "(as)", (action,), "(a(ai))").unpack()[0]
    print("dock: Meta bound to search:", [hex(k) for k in keys[0][0]] if keys else "none",
          file=sys.stderr, flush=True)


def main():
    cfg = load_config()
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    if "--search" in sys.argv[1:]:          # toggle the running dock's search
        bus.call_sync(BUS_NAME, BUS_PATH, BUS_NAME, "Search", None, None,
                      Gio.DBusCallFlags.NONE, 2000, None)
        return
    res = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                        "RequestName", GLib.Variant("(su)", (BUS_NAME, 4)),  # DO_NOT_QUEUE
                        GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, -1, None).unpack()[0]
    if res not in (1, 4):  # primary owner / already owner
        print("dock: already running", file=sys.stderr)
        sys.exit(1)

    dock = Dock(cfg)
    node = Gio.DBusNodeInfo.new_for_xml(BUS_XML)

    def on_call(_conn, _sender, _path, _iface, method, params, invocation):
        if method == "NoWindow":
            dock.on_no_window(params.unpack()[0])
        elif method == "Search":
            print(time.strftime("%H:%M:%S"), "dock: Search requested by", _sender,
                  file=sys.stderr, flush=True)
            GLib.idle_add(dock.toggle_search)
        invocation.return_value(None)

    bus.register_object(BUS_PATH, node.interfaces[0], on_call, None, None)

    def meta():
        print(time.strftime("%H:%M:%S"), "dock: Meta pressed", file=sys.stderr, flush=True)
        GLib.idle_add(dock.toggle_search)
    register_meta_shortcut(bus, meta)
    dock.connect("destroy", Gtk.main_quit)
    dock.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
