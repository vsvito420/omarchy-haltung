#!/usr/bin/env python3
"""haltung: posture monitor fed by the AirPods' head-tracking IMU.

Reads the accelerometer frames the AirPods stream through librepods ("headtrack" verb),
measures the head's tilt against a calibrated upright pose and plots it in a
layer-shell panel on the portrait monitor. Runs at SCHED_IDLE with software
rendering so it stays out of a game's way.
"""
import json, math, os, signal, socket, struct, subprocess, sys, time, tomllib, traceback, wave
from collections import deque
from pathlib import Path

# `haltung --calibrate` asks the running panel to calibrate, e.g. from a keybinding.
if "--calibrate" in sys.argv[1:]:
    os.execvp("systemctl", ["systemctl", "--user", "kill", "-s", "USR1", "haltung.service"])
# `haltung --mute` switches the beeps off and on again.
if "--mute" in sys.argv[1:]:
    os.execvp("systemctl", ["systemctl", "--user", "kill", "-s", "USR2", "haltung.service"])

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
    "limit_hi": 10.0,        # pitch above this (head forward) is outside the upright band
    "limit_lo": -8.0,        # pitch below this (head back) is outside too; both draggable in the plot
    "alert_after_s": 15,     # outside the band this long -> alert
    "beep": True,            # parking-sensor blips, faster the further you lean forward
    "beep_near_deg": 3.0,    # start blipping this far inside a limit
    "beep_volume": 0.25,     # 0..1
    "reference": None,       # calibrated gravity unit vector (upright, looking ahead)
    "axis": None,            # ear-to-ear axis from the nod, oriented so looking down is +pitch
    "calibrated_at": None,
}

ACC_OFFSET = 67        # int16 x/y/z accelerometer in an 81-byte frame, 1024 = 1 g
TAU = 0.5              # smoothing time constant, seconds
SHORT_WINDOW = 120     # seconds in the left plot
LONG_WINDOW = 1800     # seconds in the right plot, as 10 s means
Y_MIN, Y_MAX = -20.0, 30.0   # degrees, plot range
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
    # Older configs had one forward threshold.
    if "warn_deg" in cfg:
        cfg["limit_hi"] = cfg.pop("warn_deg")
    cfg.pop("bad_deg", None)
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
        self.cue = lambda name: None                 # sound hook, set by the panel
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
        if self.calib and self.calib["steps"][self.calib["i"]] in ("upright", "down", "up"):
            self.calib["samples"].append(self.g)
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
        if not self.cfg["limit_lo"] <= theta <= self.cfg["limit_hi"]:
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

    # Calibration: a list of timed steps. The ear axis depends only on how the pods sit,
    # so it is kept between calibrations and the nod is needed only once (or on request).
    STEPS = {
        "ready":   (3.0, "Gerade hinsetzen", "Rücken an die Lehne, geradeaus schauen"),
        "upright": (2.0, "Still halten", "misst deine aufrechte Haltung"),
        "down":    (3.0, "Nach unten schauen", "Kopf langsam runter, Blick auf den Tisch"),
        "up":      (3.0, "Nach oben schauen", "Kopf langsam hoch, Blick an die Decke"),
    }

    def state(self):
        now = time.monotonic()
        if self.calib:
            c = self.calib
            if now - c["t0"] >= self.STEPS[c["steps"][c["i"]]][0]:
                self.end_step(c)
            return "calib" if self.calib else self.state()
        if not self.has_data():
            self.over_since = None
            return "nodata"
        if not self.cfg.get("reference"):
            return "uncalibrated"
        if self.over_since is None:
            return "ok"
        return "alert" if now - self.over_since >= self.cfg["alert_after_s"] else "warn"

    def end_step(self, c):
        step, samples = c["steps"][c["i"]], c["samples"]
        if step == "upright":
            if len(samples) < 10:
                return self.finish_calibration("zu wenig Daten – AirPods im Ohr? Nochmal klicken", ok=False)
            c["ref"] = unit(tuple(sum(v[i] for v in samples) / len(samples) for i in range(3)))
        elif step in ("down", "up"):
            # The furthest point of the movement, as long as it went far enough.
            peak = max(samples, key=lambda v: angle_between(v, c["ref"]), default=None)
            c[step] = peak if peak and angle_between(peak, c["ref"]) > 8 else None
        c["i"] += 1
        c["t0"] = time.monotonic()
        c["samples"] = []
        if c["i"] < len(c["steps"]):
            self.cue("step")
        else:
            self.apply_calibration(c)

    def apply_calibration(self, c):
        ref = c["ref"]
        self.cfg["reference"] = list(ref)
        self.cfg["calibrated_at"] = time.strftime("%Y-%m-%d %H:%M")
        proj = lambda v: tuple(x - dot(v, ref) * r for x, r in zip(v, ref))
        if "down" in c or "up" in c:
            if c.get("down") is None and c.get("up") is None:
                return self.finish_calibration("Kopfbewegung nicht erkannt – Rechtsklick zum Wiederholen", ok=False)
            fwd = (0.0, 0.0, 0.0)
            if c.get("down"):
                fwd = tuple(f + x for f, x in zip(fwd, unit(proj(c["down"]))))
            if c.get("up"):
                fwd = tuple(f - x for f, x in zip(fwd, unit(proj(c["up"]))))
            # angles() takes forward as cross(axis, ref); this axis makes that the look down.
            self.cfg["axis"] = list(unit(cross(ref, unit(fwd))))
        elif self.cfg.get("axis"):
            # Keep the learned axis, squared up against the new reference.
            self.cfg["axis"] = list(unit(proj(self.cfg["axis"])))
        self.finish_calibration("")

    def finish_calibration(self, note, ok=True):
        self.calib_note = note
        save_config(self.cfg)
        self.cue("done" if ok else "fail")
        self.calib = None
        self.over_since = None
        self.short.clear()

    def start_calibration(self, full=False):
        """Quick (sit up straight) by default; with the nod when asked or when no axis is known yet."""
        steps = ["ready", "upright"]
        if full or not self.cfg.get("axis"):
            steps += ["down", "up"]
        self.calib_note = ""
        self.calib = {"steps": steps, "i": 0, "t0": time.monotonic(), "samples": []}
        self.cue("step")


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


class Beeper:
    """Parking sensor: one soft blip whose rate follows how close the pitch is to a limit,
    slow just inside it, fastest 8° beyond it. Played with pw-play, so it goes wherever the default
    output is (the AirPods, usually)."""

    RATE = 48000
    SLOWEST, FASTEST = 1.3, 0.15     # seconds between blips

    def __init__(self, cfg):
        self.cfg = cfg
        self.next_at = 0.0
        self.procs = []
        d = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "haltung"
        d.mkdir(exist_ok=True)
        vol = max(0.0, min(1.0, float(cfg["beep_volume"])))
        self.sound = self.write(d / "blip.wav", vol)
        self.cues = {"step": self.write(d / "step.wav", vol, [(660, 0.12)]),
                     "done": self.write(d / "done.wav", vol, [(520, 0.11), (780, 0.18)]),
                     "fail": self.write(d / "fail.wav", vol, [(392, 0.12), (330, 0.2)])}

    def write(self, path, vol, notes=((520, 0.09),)):
        # Each note: a little octave on top, soft attack, exponential tail. A "blip", not a beep.
        frames = bytearray()
        for freq, dur in notes:
            for i in range(int(self.RATE * dur)):
                t = i / self.RATE
                env = min(1.0, i / 480) * math.exp(-t * 38 * 0.09 / dur)
                v = (math.sin(2 * math.pi * freq * t) + 0.25 * math.sin(2 * math.pi * 2 * freq * t)) / 1.25
                frames += struct.pack("<h", int(v * env * vol * 32767))
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.RATE)
            w.writeframes(bytes(frames))
        return str(path)

    def interval(self, pitch, state):
        """Seconds between blips, or None for silence."""
        if pitch is None or state not in ("ok", "warn", "alert"):
            return None
        near = self.cfg["beep_near_deg"]
        # Distance to the nearer limit: positive inside the band, negative outside.
        d = min(self.cfg["limit_hi"] - pitch, pitch - self.cfg["limit_lo"])
        if d >= near:
            return None
        x = min(1.0, (near - d) / (near + 8.0))
        # Geometric, so each degree feels like the same step faster.
        return self.SLOWEST * (self.FASTEST / self.SLOWEST) ** x

    def tick(self, pitch, state):
        self.procs = [p for p in self.procs if p.poll() is None]
        if not self.cfg["beep"]:
            return
        gap = self.interval(pitch, state)
        now = time.monotonic()
        if gap is None:
            self.next_at = 0.0
            return
        # Coming closer shortens the wait that is already running, like a parking sensor.
        self.next_at = min(self.next_at, now + gap) if self.next_at else now
        if now >= self.next_at and len(self.procs) < 3:
            self.procs.append(subprocess.Popen(["pw-play", self.sound], stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL))
            self.next_at = now + gap

    def cue(self, name):
        if self.cfg["beep"] and name in self.cues:
            self.procs.append(subprocess.Popen(["pw-play", self.cues[name]], stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL))

    def toggle(self):
        self.cfg["beep"] = not self.cfg["beep"]
        save_config(self.cfg)


class Panel(Gtk.ApplicationWindow):
    def __init__(self, app, cfg):
        super().__init__(application=app, title="haltung")
        self.cfg = cfg
        self.C = theme_colors()
        self.posture = Posture(cfg)
        self.feed = Feed(self.posture)
        self.beeper = Beeper(cfg)
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
        self.posture.cue = lambda name: self.beeper.cue(name)
        click = Gtk.GestureClick(button=0)
        # Tap / left click on the head: sit up straight and done. Right click: also relearn the head axis.
        click.connect("released", lambda g, *_: self.posture.start_calibration(full=g.get_current_button() == 3))
        self.head.add_controller(click)

        # The limit lines in the plots are dragged with the mouse or a finger.
        self.plot_geo = []           # (x, y, w, h) of each plot, from the last draw
        self.dragging = None         # "limit_hi" / "limit_lo"
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self.on_drag_begin)
        drag.connect("drag-update", self.on_drag_update)
        drag.connect("drag-end", self.on_drag_end)
        self.area.add_controller(drag)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, y: self.area.set_cursor_from_name(
            "ns-resize" if self.limit_at(x, y) else None))
        self.area.add_controller(motion)
        self.set_child(box)

        self.shown = [0.0, 0.0]      # pitch, roll the head currently displays
        self.target = [0.0, 0.0]
        self.head_anim = None
        self.head_last = 0.0
        GLib.timeout_add(REDRAW_MS, self.tick)
        GLib.timeout_add(50, self.beep_tick)   # fine enough for a 0.25 s beep gap

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

    # dragging the limits

    @staticmethod
    def deg_to_y(y, h, deg):
        return y + h - h * (min(max(deg, Y_MIN), Y_MAX) - Y_MIN) / (Y_MAX - Y_MIN)

    @staticmethod
    def y_to_deg(y, h, py):
        return Y_MIN + (y + h - py) / h * (Y_MAX - Y_MIN)

    def limit_at(self, px, py):
        """The limit line under the pointer, if any (generous for fingers)."""
        for x, y, w, h in self.plot_geo:
            if x - 10 <= px <= x + w + 10 and y - 20 <= py <= y + h + 20:
                key = min(("limit_hi", "limit_lo"), key=lambda k: abs(self.deg_to_y(y, h, self.cfg[k]) - py))
                if abs(self.deg_to_y(y, h, self.cfg[key]) - py) <= 22:
                    return key, (x, y, w, h)
        return None

    def on_drag_begin(self, gesture, px, py):
        hit = self.limit_at(px, py)
        self.dragging = hit
        self.drag_start = (px, py)
        if not hit:
            gesture.set_state(Gtk.EventSequenceState.DENIED)

    def on_drag_update(self, _g, dx, dy):
        if not self.dragging:
            return
        key, (x, y, w, h) = self.dragging
        deg = round(self.y_to_deg(y, h, self.drag_start[1] + dy) * 2) / 2   # 0.5° steps
        if key == "limit_hi":
            deg = max(self.cfg["limit_lo"] + 2, min(Y_MAX, deg))
        else:
            deg = min(self.cfg["limit_hi"] - 2, max(Y_MIN, deg))
        self.cfg[key] = deg
        self.area.queue_draw()

    def on_drag_end(self, *_):
        if self.dragging:
            save_config(self.cfg)
        self.dragging = None

    def beep_tick(self):
        try:
            if self.state != "calib":
                self.beeper.tick(self.posture.theta(), self.state)
        except Exception:
            traceback.print_exc()
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
            ("außerhalb", f"{100 * p.n_over / p.n_total:4.0f} %" if p.n_total else "  —"),
            ("über seit", f"{time.monotonic() - p.over_since:4.0f} s" if p.over_since else "  —"),
        ]
        x = pad
        col_w = (w - 2 * pad) / len(cells)
        for i, (label, value) in enumerate(cells):
            self.text(cr, x, 34, label, 17, dim)
            color = status_col if i == 0 and theta is not None else fg
            self.text(cr, x, 78, value, 40 if i == 0 else 30, color, bold=i == 0)
            x += col_w

        if self.state == "calib" and p.calib:
            return self.draw_calibration(cr, w, h)
        msg = self.message()
        if msg:
            self.text(cr, pad, 114, msg, 18, C["red"] if self.state == "alert" else C["yellow"], bold=True)

        # plots
        self.plot_geo = []
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
        conn = "" if self.feed.status == "verbunden" else f"  ·  {self.feed.status}"
        beep = "Piep an" if cfg["beep"] else "Piep aus"
        foot = f"{beep}{conn} · gelbe Linien ziehen = Grenzen · Kopf antippen = kalibrieren"
        self.text(cr, pad, h - 12, foot, 15, C["muted"])

    @staticmethod
    def fmt(v):
        return f"{v:5.1f}°" if v is not None else "  —"

    def draw_calibration(self, cr, w, h):
        C, c = self.C, self.posture.calib
        step = c["steps"][c["i"]]
        dur, title, sub = Posture.STEPS[step]
        frac = min(1.0, (time.monotonic() - c["t0"]) / dur)
        cr.set_source_rgb(*C["background"])
        cr.paint()
        pad = 28
        self.text(cr, pad, 40, f"KALIBRIERUNG  ·  Schritt {c['i'] + 1} von {len(c['steps'])}", 17, C["dark_foreground"])
        self.text(cr, pad, 120, title, 52, C["foreground"], bold=True)
        self.text(cr, pad, 168, sub, 22, C["dark_foreground"])
        # one bar per step: done, running, to come
        bw = (w - 2 * pad - 12 * (len(c["steps"]) - 1)) / len(c["steps"])
        for i in range(len(c["steps"])):
            x = pad + i * (bw + 12)
            cr.set_source_rgb(*C["lighter_background"])
            cr.rectangle(x, 210, bw, 10)
            cr.fill()
            done = 1.0 if i < c["i"] else frac if i == c["i"] else 0.0
            if done:
                cr.set_source_rgb(*C["accent"])
                cr.rectangle(x, 210, bw * done, 10)
                cr.fill()
        hint = "Ein Ton pro Schritt, zwei Töne = fertig" if self.cfg["beep"] else ""
        self.text(cr, pad, h - 12, hint, 15, C["muted"])

    def message(self):
        p = self.posture
        if self.state == "nodata":
            return "keine Daten: AirPods im Ohr und verbunden?"
        if self.state == "uncalibrated":
            return "nicht kalibriert: aufrecht hinsetzen und den Kopf links antippen"
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
        # upright band between the two draggable limits
        self.plot_geo.append((x, y, w, h))
        hi, lo = Y(cfg["limit_hi"]), Y(cfg["limit_lo"])
        cr.set_source_rgba(*C["green"], 0.08)
        cr.rectangle(x, hi, w, lo - hi)
        cr.fill()
        for key, name, yy in (("limit_hi", "oben", hi), ("limit_lo", "unten", lo)):
            active = self.dragging and self.dragging[0] == key
            cr.set_source_rgba(*C["yellow"], 1.0 if active else 0.75)
            cr.set_line_width(3 if active else 2)
            cr.move_to(x, yy)
            cr.line_to(x + w, yy)
            cr.stroke()
            if y_labels:   # a handle with the value, so it is obvious the line can be grabbed
                label = f"{name} {cfg[key]:+.1f}°"
                cr.select_font_face("monospace", 0, 1)
                cr.set_font_size(14)
                tw = cr.text_extents(label).x_advance
                bx, by = x + 8, yy - 11
                cr.set_source_rgb(*C["yellow"])
                cr.rectangle(bx, by, tw + 14, 22)
                cr.fill()
                self.text(cr, bx + 7, yy + 5, label, 14, C["background"], bold=True)
        cr.set_line_width(1)
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
        signal.signal(signal.SIGUSR2, lambda *_: GLib.idle_add(panel.beeper.toggle))
    app.connect("activate", activate)
    app.run([])


if __name__ == "__main__":
    main()
