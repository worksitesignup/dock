"""Procedural dock sound effects.

The sounds are synthesised in pure Python on first run and cached as WAV files
in ./sounds/ (delete the folder to regenerate). Playback goes through pw-play
(PipeWire) or paplay, spawned asynchronously so it never blocks a frame.
"""
import math
import os
import random
import shutil
import struct
import time
import wave

from gi.repository import Gio

RATE = 44100
HERE = os.path.dirname(os.path.abspath(__file__))
SOUND_DIR = os.path.join(HERE, "sounds")
VERSION = "6"                         # bump to regenerate after changing synthesis

# Major pentatonic, two octaves: sweeping the dock plays a little tune.
PENTA = [0, 2, 4, 7, 9, 12, 14, 16, 19, 21, 24, 26]


def _write(name, samples):
    path = os.path.join(SOUND_DIR, name + ".wav")
    fade = int(RATE * 0.012)              # short fade-out so the tail never clicks
    n = len(samples)
    samples = [x * min(1.0, (n - 1 - i) / fade) for i, x in enumerate(samples)]
    peak = max(1e-9, max(abs(x) for x in samples))
    k = 0.89 / peak
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(x * k * 32767)) for x in samples))


def _env(t, attack, decay):
    a = min(1.0, t / attack) if attack > 0 else 1.0
    return a * math.exp(-t / decay)


def _drop(base_hz, dur=0.09):
    """Water drop: a sine whose pitch flicks upward as it dies away."""
    out, ph = [], 0.0
    for i in range(int(RATE * dur)):
        t = i / RATE
        f = base_hz * (1 + 1.6 * (t / dur) ** 1.5)
        ph += 2 * math.pi * f / RATE
        out.append(math.sin(ph) * _env(t, 0.003, dur * 0.35)
                   + 0.18 * math.sin(2 * ph) * _env(t, 0.002, dur * 0.15))
    return out


def _bloop(f0, f1, dur, wobble):
    """Bubble: exponential pitch sweep with a soft liquid vibrato."""
    out, ph = [], 0.0
    for i in range(int(RATE * dur)):
        t = i / RATE
        u = t / dur
        f = f0 * (f1 / f0) ** min(u * 1.4, 1.0)
        f *= 1 + wobble * math.sin(2 * math.pi * 28 * t) * (1 - u)
        ph += 2 * math.pi * f / RATE
        e = _env(t, 0.004, dur * 0.32)
        out.append(math.sin(ph) * e + 0.25 * math.sin(2 * ph + 0.6) * e * (1 - u))
    return out


def _glint(hz, dur=0.055):
    """Hover tick: a tiny, soft glass glint (pure partials, fast decay)."""
    out = []
    for i in range(int(RATE * dur)):
        t = i / RATE
        out.append(math.sin(2 * math.pi * hz * t) * _env(t, 0.0015, dur * 0.28)
                   + 0.3 * math.sin(2 * math.pi * hz * 2.76 * t) * _env(t, 0.001, dur * 0.12))
    return out


def _key(hz, seed, dur=0.022):
    """Typing tick: a tiny soft-filtered click with a faint pitched body."""
    out, lp = [], 0.0
    rnd = random.Random(seed)
    for i in range(int(RATE * dur)):
        t = i / RATE
        lp += 0.35 * ((rnd.random() * 2 - 1) - lp)          # one-pole low-pass
        out.append(lp * _env(t, 0.0004, 0.003) * 0.9
                   + 0.5 * math.sin(2 * math.pi * hz * t) * _env(t, 0.0008, 0.006))
    return out


def _tink():
    """Glassy launch tick: two inharmonic partials with quick decay."""
    out = []
    rnd = random.Random(7)
    for i in range(int(RATE * 0.22)):
        t = i / RATE
        noise = (rnd.random() * 2 - 1) * _env(t, 0.0005, 0.004) * 0.3
        out.append(math.sin(2 * math.pi * 1568 * t) * _env(t, 0.001, 0.07)
                   + 0.45 * math.sin(2 * math.pi * 3951 * t) * _env(t, 0.001, 0.03)
                   + noise)
    return out


def generate():
    os.makedirs(SOUND_DIR, exist_ok=True)
    stamp = os.path.join(SOUND_DIR, ".version")
    try:
        with open(stamp) as f:
            if f.read().strip() == VERSION:
                return
    except OSError:
        pass
    for i, semis in enumerate(PENTA):
        _write(f"wave{i}", _drop(392.0 * 2 ** (semis / 12)))       # from G4
    # Each category's bloop lands on its own note (same note as its wave blip,
    # an octave below), so every category has a recognisable voice.
    for i, semis in enumerate(PENTA):
        r = 2 ** (semis / 12)
        _write(f"pop{i}", _bloop(92 * r, 330 * r, 0.16, 0.06))
        _write(f"unpop{i}", _bloop(300 * r, 104 * r, 0.11, 0.03))
    for i, semis in enumerate(PENTA):
        _write(f"hover{i}", _glint(1046.5 * 2 ** (semis / 12)))     # from C6
    for i, hz in enumerate((1850, 2050, 1950, 2200)):
        _write(f"key{i}", _key(hz, 11 + i))
    _write("keydel", _key(1350, 5, 0.026))
    _write("launch", _tink())
    with open(stamp, "w") as f:
        f.write(VERSION)


class Player:
    def __init__(self, enabled=True, volume=0.5):
        self.volume = max(0.0, min(volume, 1.0))
        self.cmd = None
        if enabled:
            if shutil.which("pw-play"):
                self.cmd = ["pw-play", "--volume", f"{self.volume:.2f}"]
            elif shutil.which("paplay"):
                self.cmd = ["paplay", f"--volume={int(self.volume * 65536)}"]
        if self.cmd:
            try:
                generate()
            except OSError as e:
                print("dock: sounds disabled:", e)
                self.cmd = None
        self.last = {}

    def play(self, name, min_gap=0.05, gain=1.0):
        """Fire and forget; repeated triggers inside min_gap are dropped."""
        if not self.cmd:
            return
        now = time.monotonic()
        if now - self.last.get(name, 0.0) < min_gap:
            return
        self.last[name] = now
        path = os.path.join(SOUND_DIR, name + ".wav")
        cmd = list(self.cmd)
        if gain != 1.0:
            cmd[-1] = (f"{self.volume * gain:.2f}" if cmd[0] == "pw-play"
                       else f"--volume={int(self.volume * gain * 65536)}")
        try:
            proc = Gio.Subprocess.new(cmd + [path], Gio.SubprocessFlags.STDOUT_SILENCE
                                      | Gio.SubprocessFlags.STDERR_SILENCE)
            proc.wait_async(None, None, None)          # reaped by GLib
        except Exception as e:                         # never let audio break the dock
            print("dock: sound failed:", e)

    def pop(self, index):
        self.play(f"pop{index % len(PENTA)}", min_gap=0.08)

    def unpop(self, index):
        self.play(f"unpop{index % len(PENTA)}", min_gap=0.08, gain=0.8)

    def hover(self, index):
        self.play(f"hover{index % len(PENTA)}", min_gap=0.035, gain=0.32)

    def key(self, delete=False):
        """Very quiet typing feedback; varies slightly so it never drones."""
        if delete:
            self.play("keydel", min_gap=0.02, gain=0.16)
        else:
            self.keyn = (getattr(self, "keyn", 0) + 1 + int(time.monotonic() * 1000) % 2) % 4
            self.play(f"key{self.keyn}", min_gap=0.02, gain=0.14)

    def wave(self, index):
        self.play(f"wave{index % len(PENTA)}", min_gap=0.06, gain=0.55)
