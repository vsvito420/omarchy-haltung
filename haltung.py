#!/usr/bin/env python3
"""haltung: posture monitor fed by the AirPods' head-tracking IMU.

Reads the accelerometer frames the AirPods stream through librepods ("headtrack" verb),
measures the head's tilt against a calibrated upright pose and plots it in a
layer-shell panel on the portrait monitor. Runs at SCHED_IDLE with software
rendering so it stays out of a game's way.
"""
import json, math, os, signal, socket, sys, time, tomllib, traceback
from collections import deque
from pathlib import Path

# `haltung --calibrate` asks the running panel to calibrate, e.g. from a keybinding.
if "--calibrate" in sys.argv[1:]:
    os.execvp("systemctl", ["systemctl", "--user", "kill", "-s", "USR1", "haltung.service"])

# gtk4-layer-shell has to be loaded before libwayland-client, so re-exec with it preloaded.
LAYER_LIB = "/usr/lib/libgtk4-layer-shell.so"
if LAYER_LIB not in os.environ.get("LD_PRELOAD", ""):
    os.environ["LD_PRELOAD"] = (LAYER_LIB + " " + os.environ.get("LD_PRELOAD", "")).strip()
    os.execv(sys.executable, [sys.executable] + sys.argv)

# Cairo instead of GL: the panel never competes with the game for the GPU.
os.environ.setdefault("GSK_RENDERER", "cairo")

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GLib, Gtk, Gtk4LayerShell as Layer

import head3d

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "haltung"
CONFIG_FILE = CONFIG_DIR / "config.json"
SOCK = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "librepods.sock"
THEME = Path.home() / ".local/state/omarchy/current/theme/colors.toml"

DEFAULTS = {
    "monitor": "DP-1",
    "height": 380,
    "position": "top",       # top | bottom of the monitor
    "warn_deg": 10.0,        # tilt against the reference that counts as slouching
    "bad_deg": 18.0,
    "alert_after_s": 15,     # above warn_deg this long -> alert
    "reference": None,       # calibrated gravity unit vector (upright, looking ahead)
    "axis": None,            # ear-to-ear axis from the nod, oriented so looking down is +pitch
    "calibrated_at": None,
}

ACC_OFFSET = 67        # int16 x/y/z accelerometer in an 81-byte frame, 1024 = 1 g
TAU = 0.5              # smoothing time constant, seconds
SHORT_WINDOW = 120     # seconds in the left plot
LONG_WINDOW = 1800     # seconds in the right plot, as 10 s means
Y_MIN, Y_MAX = -10.0, 30.0   # degrees, plot range
REDRAW_MS = 200
HEAD_W = 250           # px, the 3D head on the left
HEAD_FPS = 30          # cap while the head is moving; nothing is drawn while it rests
HEAD_TAU = 0.15        # seconds the head takes to follow a change


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n")


def theme_colors():
    c = {"background": "#1c1c1e", "foreground": "#f5f5f7", "muted": "#636366",
         "dark_foreground": "#8e8e93", "lighter_background": "#2c2c2e",
         "green": "#30d158", "yellow": "#ffd60a", "red": "#ff453a", "accent": "#0a84ff"}
    try:
        c.update({k: v for k, v in tomllib.loads(THEME.read_text()).items() if isinstance(v, str)})
    except (OSError, ValueError):
        pass
    return {k: tuple(int(v[i:i + 2], 16) / 255 for i in (1, 3, 5))
            for k, v in c.items() if v.startswith("#") and len(v) == 7}


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def angle_between(a, b):
    return math.degrees(math.acos(max(-1.0, min(1.0, dot(unit(a), unit(b))))))


def unit(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return tuple(x / n for x in v)


def lower_priority():
    os.nice(19)
    try:
        os.sched_setscheduler(0, os.SCHED_IDLE, os.sched_param(0))
    except (OSError, AttributeError):
        pass


class Posture:
    """Smoothed gravity vector, tilt against the reference, and the statistics around it."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.g = None
        self.mag = 0.0
        self.t_last = 0.0
        self.rate = deque(maxlen=50)                 # frame timestamps, for the Hz readout
        self.short = deque()                         # (t, θ) at full rate
        self.long = deque(maxlen=LONG_WINDOW // 10)  # (t, mean θ over 10 s)
        self.bucket = []
        self.bucket_t = 0.0
        self.calib = None                            # (phase, t0, samples)
        self.calib_note = ""
        self.over_since = None
        self.n_total = 0
        self.n_over = 0
        self.sum_total = 0.0

    def feed(self, acc):
        now = time.monotonic()
        self.rate.append(now)
        mag = math.sqrt(sum(a * a for a in acc))
        if not 700 < mag < 1350:     # head moving hard, not a posture reading
            return
        dt = min(now - self.t_last, 0.2) if self.t_last else 0.04
        a = 1 - math.exp(-dt / TAU)
        self.g = acc if self.g is None else tuple(g + a * (x - g) for g, x in zip(self.g, acc))
        self.mag = mag / 1024
        self.t_last = now
        if self.calib and self.calib[0] in ("upright", "nod"):
            self.calib[2].append(self.g)
        theta = self.theta()
        if theta is None or self.calib:
            return
        self.short.append((now, theta))
        while self.short and now - self.short[0][0] > SHORT_WINDOW:
            self.short.popleft()
        self.bucket.append(theta)
        if now - self.bucket_t >= 10:
            self.long.append((now, sum(self.bucket) / len(self.bucket)))
            self.bucket, self.bucket_t = [], now
        self.n_total += 1
        self.sum_total += theta
        if theta >= self.cfg["warn_deg"]:
            self.n_over += 1
            self.over_since = self.over_since or now
        else:
            self.over_since = None

    def angles(self):
        """(pitch, roll) in degrees against the reference; pitch > 0 is the head tipping forward.
        Without a nod-derived axis only the total tilt is known: (total, None)."""
        ref, axis = self.cfg.get("reference"), self.cfg.get("axis")
        if self.g is None or not ref:
            return None, None
        g = unit(self.g)
        if not axis:
            return angle_between(g, ref), None
        fwd = cross(axis, ref)
        in_plane = unit(tuple(x - dot(g, axis) * a for x, a in zip(g, axis)))
        pitch = math.degrees(math.atan2(dot(in_plane, fwd), dot(in_plane, ref)))
        roll = math.degrees(math.asin(max(-1.0, min(1.0, dot(g, axis)))))
        return pitch, roll

    def theta(self):
        return self.angles()[0]

    def hz(self):
        r = self.rate
        return (len(r) - 1) / (r[-1] - r[0]) if len(r) > 1 and r[-1] > r[0] else 0.0

    def has_data(self):
        return self.g is not None and time.monotonic() - self.t_last < 3

    def mean(self, seconds):
        now = time.monotonic()
        vals = [v for t, v in self.short if now - t <= seconds]
        return sum(vals) / len(vals) if vals else None

    def state(self):
        now = time.monotonic()
        if self.calib:
            phase, t0, samples = self.calib[:3]
            if phase == "countdown" and now - t0 >= 3:
                self.calib = ("upright", now, [])
            elif phase == "upright" and now - t0 >= 2:
                if len(samples) < 10:
                    self.finish_calibration("zu wenig Daten, nochmal klicken")
                else:
                    ref = unit(tuple(sum(s[i] for s in samples) / len(samples) for i in range(3)))
                    self.calib = ("nod", now, [], ref)
            elif phase == "nod" and now - t0 >= 5:
                self.compute_axis(self.calib[3], samples)
            return "calib"
            return "calib"
        if not self.has_data():
            self.over_since = None
            return "nodata"
        if not self.cfg.get("reference"):
            return "uncalibrated"
        if self.over_since is None:
            return "ok"
        return "alert" if now - self.over_since >= self.cfg["alert_after_s"] else "warn"

    def compute_axis(self, ref, samples):
        """The nod sweeps gravity through the sagittal plane; its normal is the ear-to-ear axis.
        The first clear excursion is the look down, which fixes the sign."""
        self.cfg["reference"] = list(ref)
        self.cfg["calibrated_at"] = time.strftime("%Y-%m-%d %H:%M")
        down = next((s for s in samples if angle_between(s, ref) > 10), None)
        if down is None:
            self.cfg["axis"] = None
            self.finish_calibration("kein Nicken erkannt: nur Gesamtwinkel, kein Pitch/Roll")
            return
        # Average the axis over every strong sample, flipping the look-up half onto the same side.
        acc = (0.0, 0.0, 0.0)
        first = unit(cross(ref, down))
        for s in samples:
            if angle_between(s, ref) > 7:
                c = unit(cross(ref, s))
                c = c if dot(c, first) >= 0 else tuple(-x for x in c)
                acc = tuple(a + x for a, x in zip(acc, c))
        axis = unit(acc)
        # Orient so that cross(axis, ref) points towards the look down: then looking down is +pitch.
        if dot(cross(axis, ref), down) < 0:
            axis = tuple(-x for x in axis)
        self.cfg["axis"] = list(axis)
        self.finish_calibration("")

    def finish_calibration(self, note):
        self.calib_note = note
        save_config(self.cfg)
        self.calib = None
        self.over_since = None
        self.short.clear()

    def start_calibration(self):
        self.calib_note = ""
        self.calib = ("countdown", time.monotonic(), [])


class Feed:
    """Line stream from the librepods control socket, reconnecting as needed."""

    def __init__(self, posture):
        self.posture = posture
        self.sock = None
        self.buf = b""
        self.next_try = 0.0
        self.status = "verbinde"

    def tick(self):
        if not self.sock and time.monotonic() >= self.next_try:
            self.open()

    def open(self):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(SOCK))
            s.sendall(b"headtrack")
            s.setblocking(False)
        except OSError:
            self.status = "librepods nicht erreichbar"
            self.next_try = time.monotonic() + 5
            return
        self.sock, self.buf, self.status = s, b"", "verbunden"
        GLib.io_add_watch(s.fileno(), GLib.PRIORITY_DEFAULT,
                          GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR, self.on_io)

    def on_io(self, _fd, _cond):
        try:
            chunk = self.sock.recv(65536)
        except BlockingIOError:
            return True
        except OSError:
            chunk = b""
        if not chunk:
            self.sock.close()
            self.sock = None
            self.status = "librepods getrennt"
            self.next_try = time.monotonic() + 2
            return False
        self.buf += chunk
        *lines, self.buf = self.buf.split(b"\n")
        for line in lines:
            try:
                frame = bytes.fromhex(line.decode())
            except ValueError:
                continue
            # Only the 81-byte frames carry the accelerometer at this offset.
            if len(frame) == 81:
                self.posture.feed(tuple(int.from_bytes(frame[i:i + 2], "little", signed=True)
                                        for i in (ACC_OFFSET, ACC_OFFSET + 2, ACC_OFFSET + 4)))
        return True


class Panel(Gtk.ApplicationWindow):
    def __init__(self, app, cfg):
        super().__init__(application=app, title="haltung")
        self.cfg = cfg
        self.C = theme_colors()
        self.posture = Posture(cfg)
        self.feed = Feed(self.posture)
        self.state = "nodata"

        Layer.init_for_window(self)
        Layer.set_namespace(self, "haltung")
        Layer.set_layer(self, Layer.Layer.BOTTOM)
        Layer.set_keyboard_mode(self, Layer.KeyboardMode.NONE)
        edge = Layer.Edge.BOTTOM if cfg["position"] == "bottom" else Layer.Edge.TOP
        for e in (edge, Layer.Edge.LEFT, Layer.Edge.RIGHT):
            Layer.set_anchor(self, e, True)
        Layer.set_exclusive_zone(self, cfg["height"])
        mon = self.find_monitor(cfg["monitor"])
        if mon:
            Layer.set_monitor(self, mon)
        self.set_default_size(-1, cfg["height"])

        self.head = Gtk.DrawingArea()
        self.head.set_size_request(HEAD_W, -1)
        self.head.set_draw_func(self.draw_head)
        self.area = Gtk.DrawingArea()
        self.area.set_hexpand(True)
        self.area.set_draw_func(self.draw)
        box = Gtk.Box()
        box.append(self.head)
        box.append(self.area)
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: self.posture.start_calibration())
        box.add_controller(click)
        self.set_child(box)

        self.shown = [0.0, 0.0]      # pitch, roll the head currently displays
        self.target = [0.0, 0.0]
        self.head_anim = None
        self.head_last = 0.0
        GLib.timeout_add(REDRAW_MS, self.tick)

    @staticmethod
    def find_monitor(name):
        mons = Gdk.Display.get_default().get_monitors()
        for i in range(mons.get_n_items()):
            if mons.get_item(i).get_connector() == name:
                return mons.get_item(i)
        return None

    def tick(self):
        # Never let an exception stop the timer: GLib drops a source whose callback raises.
        try:
            self.feed.tick()
            self.state = self.posture.state()
        except Exception:
            traceback.print_exc()
        self.area.queue_draw()
        self.update_head_target()
        return True

    # 3D head: follows the measured angles, animated only while it is still moving

    def update_head_target(self):
        pitch, roll = self.posture.angles() if self.state in ("ok", "warn", "alert") else (0.0, 0.0)
        target = [pitch or 0.0, roll or 0.0]
        # A small dead band keeps sensor noise from waking the animation forever.
        if max(abs(a - b) for a, b in zip(target, self.target)) > 0.3:
            self.target = target
        if self.head_anim is None and max(abs(a - b) for a, b in zip(self.target, self.shown)) > 0.05:
            self.head_last = time.monotonic()
            self.head_anim = GLib.timeout_add(1000 // HEAD_FPS, self.head_step)
        if self.state != getattr(self, "head_state", None):   # colour follows the state even at rest
            self.head_state = self.state
            self.head.queue_draw()

    def head_step(self):
        now = time.monotonic()
        k = 1 - math.exp(-(now - self.head_last) / HEAD_TAU)
        self.head_last = now
        self.shown = [s + k * (t - s) for s, t in zip(self.shown, self.target)]
        self.head.queue_draw()
        if max(abs(a - b) for a, b in zip(self.target, self.shown)) < 0.05:
            self.shown = list(self.target)
            self.head_anim = None
            return False
        return True

    def draw_head(self, _area, cr, w, h):
        C = self.C
        cr.set_source_rgb(*C["background"])
        cr.paint()
        status = {"ok": C["green"], "warn": C["yellow"], "alert": C["red"]}.get(self.state, C["muted"])
        cr.set_source_rgb(*status)
        cr.rectangle(0, 0, 8, h)
        cr.fill()
        mix = lambda a, b, t: tuple(x * (1 - t) + y * t for x, y in zip(a, b))
        skin = mix(C["foreground"], status, 0.35)
        colors = {"skin": skin, "ear": mix(skin, (0, 0, 0), 0.1), "eye": C["background"],
                  "pod": (1.0, 1.0, 1.0), "body": C["muted"]}
        try:
            head3d.draw(cr, w, h - 20, self.shown[0], self.shown[1], colors)
        except Exception:
            traceback.print_exc()

    # drawing ---------------------------------------------------------------

    def text(self, cr, x, y, s, size, color, bold=False, align="left"):
        cr.select_font_face("monospace", 0, 1 if bold else 0)
        cr.set_font_size(size)
        if align != "left":
            w = cr.text_extents(s).x_advance
            x -= w if align == "right" else w / 2
        cr.set_source_rgb(*color)
        cr.move_to(x, y)
        cr.show_text(s)

    def draw(self, _area, cr, w, h):
        C, p, cfg = self.C, self.posture, self.cfg
        fg, dim = C["foreground"], C["dark_foreground"]
        cr.set_source_rgb(*C["background"])
        cr.paint()
        pad = 28

        # status strip on the left edge
        status_col = {"ok": C["green"], "warn": C["yellow"], "alert": C["red"]}.get(self.state, C["muted"])

        # readouts
        pitch, roll = p.angles() if self.state in ("ok", "warn", "alert") else (None, None)
        theta = pitch
        cells = [
            ("Pitch" if cfg.get("axis") else "Neigung", (f"{pitch:+5.1f}°" if cfg.get("axis") else f"{pitch:5.1f}°") if pitch is not None else "  —  "),
            ("Roll", f"{roll:+5.1f}°" if roll is not None else "  —"),
            ("Ø 60 s", self.fmt(p.mean(60))),
            (f"> {cfg['warn_deg']:.0f}°", f"{100 * p.n_over / p.n_total:4.0f} %" if p.n_total else "  —"),
            ("über seit", f"{time.monotonic() - p.over_since:4.0f} s" if p.over_since else "  —"),
        ]
        x = pad
        col_w = (w - 2 * pad) / len(cells)
        for i, (label, value) in enumerate(cells):
            self.text(cr, x, 34, label, 17, dim)
            color = status_col if i == 0 and theta is not None else fg
            self.text(cr, x, 78, value, 40 if i == 0 else 30, color, bold=i == 0)
            x += col_w

        msg = self.message()
        if msg:
            self.text(cr, pad, 114, msg, 18, C["red"] if self.state == "alert" else C["yellow"], bold=True)

        # plots
        top, bottom = 150, h - 64
        split = pad + (w - 2 * pad) * 0.62
        now = time.monotonic()
        self.plot(cr, pad + 44, top, split - pad - 44 - 20, bottom - top,
                  [(t - now, v) for t, v in p.short], SHORT_WINDOW, ("Pitch" if cfg.get("axis") else "Neigung") + " [°]  ·  25 Hz, tau 0.5 s",
                  [-120, -90, -60, -30, 0], lambda s: f"{s:d} s")
        self.plot(cr, split + 30, top, w - pad - split - 30, bottom - top,
                  [(t - now, v) for t, v in p.long], LONG_WINDOW, "Mittel je 10 s [°]",
                  [-1800, -900, 0], lambda s: f"{s // 60:d} min", y_labels=False)

        # footer
        ref = f"Referenz {cfg['calibrated_at']}" if cfg.get("calibrated_at") else "keine Referenz"
        conn = "" if self.feed.status == "verbunden" else f"  ·  {self.feed.status}"
        foot = f"{ref}  ·  |g| {p.mag:.2f} g  ·  {p.hz():4.1f} Hz{conn}  ·  Klick: kalibrieren"
        self.text(cr, pad, h - 12, foot, 15, C["muted"])

    @staticmethod
    def fmt(v):
        return f"{v:5.1f}°" if v is not None else "  —"

    def message(self):
        p = self.posture
        if self.state == "calib" and p.calib:
            phase, t0 = p.calib[:2]
            if phase == "countdown":
                return f"KALIBRIERUNG: aufrecht hinsetzen, geradeaus schauen ... {3 - (time.monotonic() - t0):.0f}"
            if phase == "upright":
                return "KALIBRIERUNG: geradeaus schauen, still halten"
            return f"KALIBRIERUNG: jetzt erst nach UNTEN, dann nach OBEN schauen ... {5 - (time.monotonic() - t0):.0f}"
        if self.state == "nodata":
            return "keine Daten: AirPods im Ohr und verbunden?"
        if self.state == "uncalibrated":
            return "nicht kalibriert: aufrecht hinsetzen und klicken"
        if self.state == "alert":
            return "SITZ GERADE"
        if p.calib_note:
            return p.calib_note
        return ""

    def plot(self, cr, x, y, w, h, pts, window, title, xticks, xfmt, y_labels=True):
        C, cfg = self.C, self.cfg
        cr.set_line_width(1)
        # frame and grid
        cr.set_source_rgb(*C["lighter_background"])
        def Y(deg):
            return y + h - h * (min(max(deg, Y_MIN), Y_MAX) - Y_MIN) / (Y_MAX - Y_MIN)
        for deg in range(int(Y_MIN), int(Y_MAX) + 1, 5):
            yy = round(Y(deg)) + 0.5
            cr.move_to(x, yy)
            cr.line_to(x + w, yy)
        for s in xticks:
            xx = round(x + w + w * s / window) + 0.5
            cr.move_to(xx, y)
            cr.line_to(xx, y + h)
        cr.stroke()
        if y_labels:
            for deg in range(int(Y_MIN), int(Y_MAX) + 1, 10):
                self.text(cr, x - 8, Y(deg) + 5, f"{deg}", 14, C["muted"], align="right")
        for i, s in enumerate(xticks):
            align = "left" if i == 0 else "right" if i == len(xticks) - 1 else "center"
            self.text(cr, x + w + w * s / window, y + h + 20, xfmt(s), 14, C["muted"], align=align)
        self.text(cr, x, y - 8, title, 14, C["dark_foreground"])
        # thresholds, dashed
        cr.set_dash([6, 5])
        for deg, col in ((cfg["warn_deg"], C["yellow"]), (cfg["bad_deg"], C["red"])):
            yy = Y(deg)
            cr.set_source_rgba(*col, 0.7)
            cr.move_to(x, yy)
            cr.line_to(x + w, yy)
            cr.stroke()
        cr.set_dash([])
        # series
        cr.save()
        cr.rectangle(x, y, w, h)
        cr.clip()
        cr.set_line_width(2)
        cr.set_source_rgb(*C["accent"])
        prev_t = None
        for t, v in pts:
            px, py = x + w + w * t / window, Y(v)
            if prev_t is None or t - prev_t > window / 60:   # gap in the data
                cr.move_to(px, py)
            else:
                cr.line_to(px, py)
            prev_t = t
        cr.stroke()
        cr.restore()


def main():
    lower_priority()
    cfg = load_config()
    if not CONFIG_FILE.exists():
        save_config(cfg)
    app = Gtk.Application(application_id="dev.vito.haltung")
    def activate(a):
        panel = Panel(a, cfg)
        panel.present()
        # SIGUSR1 (sent by `haltung --calibrate`) starts a calibration.
        signal.signal(signal.SIGUSR1, lambda *_: GLib.idle_add(panel.posture.start_calibration))
    app.connect("activate", activate)
    app.run([])


if __name__ == "__main__":
    main()
