"""Type-to-launch search bubble for Tiger Dock.

A round, jelly-soft glass bubble at the top middle of the screen. It grows
into a pill as you type, wobbles with each keystroke, and magnifies the letters
around the caret the way the OS X dock magnifies icons around the pointer.
The dock's blob shows the results; this window only owns the keyboard.
"""

import math
import time

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, GtkLayerShell, Pango, PangoCairo  # noqa: E402

FONT_PX = 22            # resting letter size
TEXT_MAG = 1.7          # letter size at the caret
TEXT_MAG_R = 64.0       # falloff radius around the caret (px)
BODY_H = 62             # bubble height (it is a circle when empty)
PAD_X = 26              # text padding inside the pill
TOP = 26                # gap from the top of the screen
N_PTS = 56              # soft-body resolution
WIN_H = 170


class Spring:
    __slots__ = ("x", "v")

    def __init__(self, x=0.0):
        self.x, self.v = x, 0.0

    def step(self, target, dt, k, c):
        n = max(1, int(math.ceil(dt / 0.004)))
        h = dt / n
        for _ in range(n):
            self.v += (-k * (self.x - target) - c * self.v) * h
            self.x += self.v * h

    def near(self, target, eps=0.01, veps=0.05):
        return abs(self.x - target) < eps and abs(self.v) < veps


def _stripes():
    stripe = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 4)
    sc = cairo.Context(stripe)
    sc.set_source_rgba(1, 1, 1, 0.22)
    sc.rectangle(0, 0, 1, 2)
    sc.fill()
    pat = cairo.SurfacePattern(stripe)
    pat.set_extend(cairo.EXTEND_REPEAT)
    pat.set_filter(cairo.FILTER_NEAREST)
    return pat


class SearchBar(Gtk.Window):
    def __init__(self, dock):
        super().__init__(title="Tiger Dock Search")
        self.dock = dock
        self.text = []
        self.caret = 0
        self.visible_now = False
        self.tick_id = None
        self.last_t = None
        self.last_key_t = 0.0
        self.blink_id = None
        self.glyphs = {}                     # char -> (layout, width, baseline)
        self.stripes = _stripes()

        self.appear = Spring(0.0)            # pops in / out
        self.hw = Spring(BODY_H / 2)         # half width of the pill
        self.squash = Spring(0.0)            # + flatter, - taller
        self.cx = Spring(0.0)                # smoothed caret (unmagnified px)
        self.off = [0.0] * N_PTS             # soft-body skin offsets (px, outward)
        self.vel = [0.0] * N_PTS

        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_namespace(self, "tiger-dock-search")
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        for edge in (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT):
            GtkLayerShell.set_anchor(self, edge, True)
        GtkLayerShell.set_exclusive_zone(self, 0)        # sit below the top bar, not over it
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
        visual = self.get_screen().get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)
        self.set_app_paintable(True)

        self.area = Gtk.DrawingArea()
        self.area.set_size_request(-1, WIN_H)
        self.area.connect("draw", self.on_draw)
        self.add(self.area)
        self.connect("key-press-event", self.on_key)
        self.connect("realize", self._on_realize)

        self.font = Pango.FontDescription.from_string("Sans")
        self.font.set_absolute_size(FONT_PX * Pango.SCALE)
        self.hint_font = Pango.FontDescription.from_string("Sans Bold 9")

    def _on_realize(self, *_):
        # Clicks pass straight through: this window is only for the keyboard.
        self.get_window().input_shape_combine_region(cairo.Region(), 0, 0)

    # ---- open / close ---------------------------------------------------------

    def toggle(self):
        if self.visible_now:
            self.close()
        else:
            self.open()
        return False

    def open(self):
        if self.visible_now:
            return
        self.visible_now = True
        self.text, self.caret = [], 0
        self.cx = Spring(0.0)
        self.hw = Spring(BODY_H / 2)
        self.appear.x, self.appear.v = 0.0, 0.0
        self.off = [0.0] * N_PTS
        self.vel = [4.0 * math.sin(i * 5.0) for i in range(N_PTS)]   # jelly on arrival
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.EXCLUSIVE)
        self.show_all()
        self.present()
        self.dock.bubble.begin_search()
        self._start_blink()
        self.wake()

    def close(self, launched=False):
        if not self.visible_now:
            return
        self.visible_now = False
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
        if self.blink_id is not None:
            GLib.source_remove(self.blink_id)
            self.blink_id = None
        self.dock.bubble.end_search(quiet=launched)
        self.wake()

    # ---- keyboard -------------------------------------------------------------

    def on_key(self, _w, ev):
        k = ev.keyval
        ctrl = bool(ev.state & Gdk.ModifierType.CONTROL_MASK)
        alt = bool(ev.state & Gdk.ModifierType.MOD1_MASK)
        blob = self.dock.bubble
        if k == Gdk.KEY_Escape:
            self.close()
        elif k in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            app = blob.search_selected()
            if app is not None:
                self.dock.activate_app(app)         # also closes the search
            else:
                self._poke(self._caret_screen_x(), -60)
        elif k in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            back = k == Gdk.KEY_ISO_Left_Tab or bool(ev.state & Gdk.ModifierType.SHIFT_MASK)
            blob.search_cycle(-1 if back else 1)
        elif k == Gdk.KEY_BackSpace:
            if self.caret > 0:
                n = 1
                if ctrl:                             # delete a word
                    i = self.caret
                    while i > 0 and self.text[i - 1] == " ":
                        i -= 1
                    while i > 0 and self.text[i - 1] != " ":
                        i -= 1
                    n = self.caret - i
                del self.text[self.caret - n:self.caret]
                self.caret -= n
                self._edited(delete=True)
        elif k == Gdk.KEY_Delete:
            if self.caret < len(self.text):
                del self.text[self.caret]
                self._edited(delete=True)
        elif k in (Gdk.KEY_Left, Gdk.KEY_KP_Left):
            self.caret = max(0, self.caret - 1)
            self._moved()
        elif k in (Gdk.KEY_Right, Gdk.KEY_KP_Right):
            self.caret = min(len(self.text), self.caret + 1)
            self._moved()
        elif k in (Gdk.KEY_Home, Gdk.KEY_KP_Home):
            self.caret = 0
            self._moved()
        elif k in (Gdk.KEY_End, Gdk.KEY_KP_End):
            self.caret = len(self.text)
            self._moved()
        elif ctrl and k in (Gdk.KEY_u, Gdk.KEY_U):
            if self.text:
                self.text, self.caret = [], 0
                self._edited(delete=True)
        else:
            cp = Gdk.keyval_to_unicode(k)
            ch = chr(cp) if cp else ""
            if ch and ch.isprintable() and not ctrl and not alt and len(self.text) < 60:
                self.text.insert(self.caret, ch)
                self.caret += 1
                self._edited()
        return True

    def _edited(self, delete=False):
        self.dock.sfx.key(delete)
        self.last_key_t = time.monotonic()
        self.dock.bubble.set_query("".join(self.text))
        self._poke(self._caret_screen_x(), -55 if delete else 85)
        self.squash.v += -0.9 if delete else 1.2
        self.wake()

    def _moved(self):
        self.last_key_t = time.monotonic()
        self.wake()

    # ---- text layout ------------------------------------------------------------

    def _glyph(self, cr, ch):
        g = self.glyphs.get(ch)
        if g is None:
            lay = PangoCairo.create_layout(cr)
            lay.set_font_description(self.font)
            lay.set_text(ch, -1)
            _ink, logical = lay.get_pixel_extents()
            g = (lay, logical.width, lay.get_baseline() / Pango.SCALE)
            self.glyphs[ch] = g
        return g

    def _measure(self, cr):
        """Per-letter placement: [(ch, x, scale)], text span, caret x.

        All x values are relative to the bubble centre. Letters grow by their
        distance to the caret; like the OS X dock, the magnified row is shifted
        so the caret itself does not move.
        """
        ws = [self._glyph(cr, ch)[1] for ch in self.text]
        total0 = sum(ws)
        caret0 = self.cx.x                         # smoothed, unmagnified
        xs, x = [], 0.0
        for w in ws:
            xs.append(x)
            x += w
        scales = []
        for x0, w in zip(xs, ws):
            d = abs(x0 + w / 2 - caret0)
            f = (math.cos(math.pi * min(d / TEXT_MAG_R, 1.0)) + 1) / 2
            scales.append(1 + (TEXT_MAG - 1) * f)
        # magnified position of the (smoothed) caret
        mx, mc = 0.0, None
        for x0, w, s in zip(xs, ws, scales):
            if mc is None and caret0 <= x0 + w:
                mc = mx + max(0.0, caret0 - x0) * s
            mx += w * s
        if mc is None:
            mc = mx
        anchor = caret0 - total0 / 2               # caret's resting place
        start = anchor - mc
        out, x = [], start
        for ch, w, s in zip(self.text, ws, scales):
            out.append((ch, x, s))
            x += w * s
        return out, (start, start + mx), anchor

    def _caret_target(self, cr=None):
        if cr is None:
            return self.cx.x
        return sum(self._glyph(cr, ch)[1] for ch in self.text[:self.caret])

    def _caret_screen_x(self):
        return getattr(self, "caret_sx", self.area.get_allocated_width() / 2)

    # ---- soft body ----------------------------------------------------------------

    def _skin(self, cx, cy, hw, r):
        """Base points + outward normals around a stadium of half width hw."""
        a = max(0.0, hw - r)
        per = 4 * a + 2 * math.pi * r
        pts = []
        for i in range(N_PTS):
            u = per * i / N_PTS
            if u < a:                                   # top, centre -> right
                p, n = (cx + u, cy - r), (0.0, -1.0)
            elif u < a + math.pi * r:                   # right cap
                t = (u - a) / r - math.pi / 2
                p, n = (cx + a + r * math.cos(t), cy + r * math.sin(t)), (math.cos(t), math.sin(t))
            elif u < 3 * a + math.pi * r:               # bottom, right -> left
                p, n = (cx + a - (u - a - math.pi * r), cy + r), (0.0, 1.0)
            elif u < 3 * a + 2 * math.pi * r:           # left cap
                t = (u - 3 * a - math.pi * r) / r + math.pi / 2
                p, n = (cx - a + r * math.cos(t), cy + r * math.sin(t)), (math.cos(t), math.sin(t))
            else:                                        # top, left -> centre
                p, n = (cx - a + (u - 3 * a - 2 * math.pi * r), cy - r), (0.0, -1.0)
            pts.append((p, n))
        return pts

    def _poke(self, x, strength):
        """Push the skin outward (+) or inward (-) near screen x."""
        if not hasattr(self, "skin_x"):
            return
        for i, sx in enumerate(self.skin_x):
            w = math.exp(-((sx - x) / 46.0) ** 2)
            self.vel[i] += strength * w

    def _step_skin(self, dt):
        n = max(1, int(math.ceil(dt / 0.004)))
        h = dt / n
        k, c, kc = 230.0, 7.5, 700.0
        o, v = self.off, self.vel
        for _ in range(n):
            for i in range(N_PTS):
                lap = o[i - 1] + o[(i + 1) % N_PTS] - 2 * o[i]
                v[i] += (-k * o[i] - c * v[i] + kc * lap) * h
            for i in range(N_PTS):
                o[i] += v[i] * h
        return any(abs(x) > 0.05 for x in o) or any(abs(x) > 0.5 for x in v)

    # ---- animation ----------------------------------------------------------------

    def wake(self):
        if self.tick_id is None:
            self.last_t = None
            self.tick_id = self.area.add_tick_callback(self.on_tick)

    def _start_blink(self):
        if self.blink_id is None:
            self.blink_id = GLib.timeout_add(530, self._blink)

    def _blink(self):
        if not self.visible_now:
            self.blink_id = None
            return False
        self.area.queue_draw()
        return True

    def on_tick(self, _w, _clock):
        now = time.monotonic()
        dt = 0.016 if self.last_t is None else min(now - self.last_t, 0.05)
        self.last_t = now
        tgt = 1.0 if self.visible_now else 0.0
        self.appear.step(tgt, dt, 300 if self.visible_now else 500, 17 if self.visible_now else 40)
        self.hw.step(getattr(self, "hw_target", BODY_H / 2), dt, 260, 17)
        self.squash.step(0.0, dt, 300, 9)
        self.cx.step(getattr(self, "caret_target", 0.0), dt, 520, 46)
        moving = self._step_skin(dt)
        moving |= not (self.appear.near(tgt, 0.002, 0.01) and self.hw.near(self.hw.x, 0.1, 0.2)
                       and abs(self.hw.v) < 0.3 and self.squash.near(0.0, 0.002, 0.01)
                       and self.cx.near(getattr(self, "caret_target", 0.0), 0.05, 0.2))
        self.area.queue_draw()
        if not self.visible_now and self.appear.x < 0.02:
            self.hide()
            self.tick_id = None
            return GLib.SOURCE_REMOVE
        if moving:
            return GLib.SOURCE_CONTINUE
        self.tick_id = None
        return GLib.SOURCE_REMOVE

    # ---- drawing ------------------------------------------------------------------

    def _path(self, cr, pts):
        """Closed Catmull-Rom spline through the skin points."""
        n = len(pts)
        cr.new_path()
        cr.move_to(*pts[0])
        for i in range(n):
            p0, p1, p2, p3 = pts[i - 1], pts[i], pts[(i + 1) % n], pts[(i + 2) % n]
            cr.curve_to(p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6,
                        p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6,
                        p2[0], p2[1])
        cr.close_path()

    def on_draw(self, widget, cr):
        W = widget.get_allocated_width()
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        # Text layout first: it decides how wide the body wants to be.
        self.total0 = sum(self._glyph(cr, ch)[1] for ch in self.text)
        self.caret_target = self._caret_target(cr)
        letters, (x0, x1), caret_rel = self._measure(cr)
        span = max(abs(x0), abs(x1))
        self.hw_target = max(BODY_H / 2, span + PAD_X) if self.text else BODY_H / 2

        ap = max(0.0, self.appear.x)
        if ap < 0.01:
            return
        cx, cy = W / 2, TOP + BODY_H / 2
        r = BODY_H / 2 * (1 - 0.18 * self.squash.x)
        hw = max(self.hw.x, r) * (1 + 0.10 * self.squash.x)
        skin = self._skin(cx, cy, hw, r)
        self.skin_x = [p[0] for p, _n in skin]
        pts = [(p[0] + nx * o, p[1] + ny * o) for (p, (nx, ny)), o in zip(skin, self.off)]

        cr.save()
        cr.translate(cx, cy)
        cr.scale(ap, ap)
        cr.translate(-cx, -cy)

        # soft shadow
        for i, a in enumerate((0.10, 0.07, 0.05, 0.03)):
            cr.save()
            cr.translate(cx, cy + 5)
            k = 1 + (i + 1) * 0.035
            cr.scale(k, k)
            cr.translate(-cx, -cy)
            self._path(cr, pts)
            cr.restore()
            cr.set_source_rgba(0, 0, 0, a)
            cr.fill()

        self._path(cr, pts)
        path = cr.copy_path()
        top, bot = cy - r - 8, cy + r + 8
        grad = cairo.LinearGradient(0, top, 0, bot)
        grad.add_color_stop_rgba(0.0, 1, 1, 1, 0.97)
        grad.add_color_stop_rgba(0.55, 0.93, 0.94, 0.97, 0.94)
        grad.add_color_stop_rgba(1.0, 0.84, 0.86, 0.91, 0.94)
        cr.set_source(grad)
        cr.fill_preserve()
        cr.save()
        cr.clip()
        cr.set_source(self.stripes)
        cr.paint()
        cr.save()                                       # gloss across the top half
        cr.translate(cx, cy - r * 0.45)
        cr.scale(max(hw - r * 0.35, r * 0.6), r * 0.55)
        cr.arc(0, 0, 1, 0, 2 * math.pi)
        cr.restore()
        gg = cairo.LinearGradient(0, cy - r, 0, cy)
        gg.add_color_stop_rgba(0.0, 1, 1, 1, 0.75)
        gg.add_color_stop_rgba(1.0, 1, 1, 1, 0.0)
        cr.set_source(gg)
        cr.fill()
        cr.append_path(path)
        cr.set_line_width(3)
        cr.set_source_rgba(1, 1, 1, 0.75)
        cr.stroke()
        cr.restore()
        cr.append_path(path)
        cr.set_line_width(1)
        cr.set_source_rgba(0, 0, 0, 0.30)
        cr.stroke()

        # contents
        if not self.text:
            self._draw_magnifier(cr, cx, cy)
        else:
            for ch, x, s in letters:
                lay, _w, base = self._glyph(cr, ch)
                cr.save()
                cr.translate(cx + x, cy + FONT_PX * 0.47)
                cr.scale(s, s)
                cr.move_to(0, -base)
                cr.set_source_rgba(0.08, 0.08, 0.10, 0.92)
                PangoCairo.show_layout(cr, lay)
                cr.restore()
        # caret: steady while typing, blinks when idle
        idle = time.monotonic() - self.last_key_t
        on = idle < 0.6 or int(idle / 0.53) % 2 == 0
        self.caret_sx = cx + caret_rel
        if self.visible_now and on and self.text:
            ch = FONT_PX * 1.25 * TEXT_MAG * 0.62
            x = cx + caret_rel
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_width(2.4)
            cr.set_source_rgba(0.20, 0.46, 0.88, 0.95)
            cr.move_to(x, cy - ch / 2 + 3)
            cr.line_to(x, cy + ch / 2 + 3)
            cr.stroke()
        cr.restore()

        # hint under the bubble
        n = self.dock.bubble.search_count() if self.text else 0
        if self.visible_now and self.text and ap > 0.9:
            if n == 0:
                hint = "No matches"
            elif n == 1:
                hint = "Return to open"
            else:
                hint = f"{n} matches  ·  Tab for next  ·  Return to open"
            lay = PangoCairo.create_layout(cr)
            lay.set_font_description(self.hint_font)
            lay.set_text(hint, -1)
            _i, lg = lay.get_pixel_extents()
            hx, hy = cx - lg.width / 2, cy + r + 16
            cr.set_source_rgba(0, 0, 0, 0.45)
            cr.move_to(hx, hy + 1)
            PangoCairo.show_layout(cr, lay)
            cr.set_source_rgba(1, 1, 1, 0.92)
            cr.move_to(hx, hy)
            PangoCairo.show_layout(cr, lay)

    @staticmethod
    def _draw_magnifier(cr, cx, cy):
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_width(3.0)
        cr.set_source_rgba(0.35, 0.37, 0.42, 0.85)
        cr.arc(cx - 3, cy - 3, 9, 0, 2 * math.pi)
        cr.stroke()
        cr.move_to(cx + 4, cy + 4)
        cr.line_to(cx + 11, cy + 11)
        cr.stroke()
