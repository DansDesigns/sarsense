"""Per-link CSI processing. NumPy only, sized for a 512 MB board.

A *link* is one (receiving node, transmitter MAC) pair. Each link keeps a ring
buffer of amplitude vectors and produces two outputs:

motion     How much the multipath pattern is changing, from the correlation
           between consecutive frames (the metric used in the Raspberry Pi
           Nexmon write-up), turned into a z-score against a baseline learnt
           while the link is quiet.
breathing  A periodic 0.15 to 0.6 Hz component across the most variable
           subcarriers, only checked while motion is low. This is what lets a
           still (for example trapped or unconscious) person show up at all,
           and it only works at short range on a stable link.
"""
from __future__ import annotations

import time
from collections import deque

import numpy as np

from .protocol import NBINS

CAPACITY = 1024          # frames kept per link (about 50 s at 20 Hz)
MOTION_WIN = 2.0         # seconds
BREATH_WIN = 24.0        # seconds
BREATH_FS = 10.0         # resample rate for breathing analysis
STALE_AFTER = 5.0


class Link:
    __slots__ = ("node", "src", "amp", "ts", "head", "count", "rssi", "channel",
                 "base_mu", "base_sd", "base_n", "z", "motion", "active", "on_ticks",
                 "breath_bpm", "breath_snr", "breathing", "history", "last_breath_check",
                 "muted", "calib_until", "calib_vals", "last_active", "fmt", "fmt_other")

    def __init__(self, node: str, src: str):
        self.node, self.src = node, src
        self.amp = np.zeros((CAPACITY, NBINS), dtype=np.float32)
        self.ts = np.zeros(CAPACITY, dtype=np.float64)
        self.head = 0
        self.count = 0
        self.rssi = 0
        self.channel = 0
        self.base_mu = None
        self.base_sd = None
        self.base_n = 0
        self.z = 0.0
        self.motion = 0.0
        self.active = False
        self.on_ticks = 0
        self.breath_bpm = None
        self.breath_snr = 0.0
        self.breathing = False
        self.history = deque(maxlen=120)  # (t, z) for the sparkline
        self.last_breath_check = 0.0
        self.muted = False
        self.calib_until = 0.0
        self.calib_vals = []
        self.last_active = 0.0
        self.fmt = None
        self.fmt_other = {}

    # ------------------------------------------------------------------ input
    def accept_format(self, fmt: int) -> bool:
        """Lock onto one CSI format per link. Mixing, say, 11g and HE frames
        would look like constant movement. If another format clearly becomes
        the common one (router settings changed), switch and start again."""
        c = self.fmt_other
        c[fmt] = c.get(fmt, 0) + 1
        if sum(c.values()) >= 256:
            for k in list(c):
                c[k] //= 2
        if self.fmt is None:
            self.fmt = fmt
        if fmt != self.fmt and c[fmt] > 50 and c[fmt] > 3 * c.get(self.fmt, 0):
            self.fmt, self.count, self.head, self.base_mu = fmt, 0, 0, None
        return fmt == self.fmt

    def push(self, t: float, amp: np.ndarray, rssi: int, channel: int):
        self.amp[self.head] = amp
        self.ts[self.head] = t
        self.head = (self.head + 1) % CAPACITY
        self.count = min(self.count + 1, CAPACITY)
        self.rssi, self.channel = rssi, channel

    def _window(self, seconds: float, now: float):
        if self.count == 0:
            return None, None
        idx = (self.head - 1 - np.arange(self.count)) % CAPACITY  # newest first
        ts = self.ts[idx]
        keep = ts >= now - seconds
        if not keep.any():
            return None, None
        idx = idx[keep][::-1]  # oldest first
        return self.ts[idx], self.amp[idx]

    @property
    def last_t(self) -> float:
        return float(self.ts[(self.head - 1) % CAPACITY]) if self.count else 0.0

    def rate(self, now: float) -> float:
        ts, _ = self._window(5.0, now)
        if ts is None or len(ts) < 2:
            return 0.0
        return (len(ts) - 1) / max(ts[-1] - ts[0], 1e-3)

    def stale(self, now: float) -> bool:
        return now - self.last_t > STALE_AFTER

    # ------------------------------------------------------------- features
    @staticmethod
    def _valid_bins(a: np.ndarray) -> np.ndarray:
        m = a.mean(axis=0)
        return m > max(1.0, 0.05 * float(m.max(initial=0.0)))

    def _motion_metric(self, a: np.ndarray) -> float:
        a = a[:, self._valid_bins(a)]
        if a.shape[1] < 8 or a.shape[0] < 4:
            return 0.0
        a = a - a.mean(axis=1, keepdims=True)
        n = np.linalg.norm(a, axis=1, keepdims=True)
        a = a / np.maximum(n, 1e-6)
        r = np.sum(a[1:] * a[:-1], axis=1)  # Pearson r of consecutive frames
        return float(np.median(1.0 - r * r) * 100.0)

    def calibrate(self, seconds: float, now: float):
        self.calib_until = now + seconds
        self.calib_vals = []

    def update(self, now: float, cfg) -> None:
        ts, a = self._window(MOTION_WIN, now)
        if ts is None or len(ts) < 4:
            self.z, self.active, self.on_ticks = 0.0, False, 0
            return
        m = self._motion_metric(a)
        self.motion = m

        if now < self.calib_until:
            self.calib_vals.append(m)
        elif self.calib_vals:
            v = np.array(self.calib_vals)
            self.base_mu = float(np.median(v))
            self.base_sd = float(max(1.4826 * np.median(np.abs(v - self.base_mu)), 0.02))
            self.base_n = 1000
            self.calib_vals = []

        if self.base_mu is None:
            self.base_mu, self.base_sd, self.base_n = m, max(m * 0.5, 0.05), 1
        z = (m - self.base_mu) / max(self.base_sd, 1e-3)
        self.z = float(z)

        if self.active:
            self.active = z > cfg.z_off
        else:
            self.on_ticks = self.on_ticks + 1 if z > cfg.z_on else 0
            self.active = self.on_ticks >= cfg.on_ticks
        if self.muted or now < self.calib_until:
            self.active = False
        if self.active or z > cfg.z_on:
            self.last_active = now

        # adapt the baseline only while quiet, so a moving person is not learnt
        if not self.active and z < cfg.z_off and now >= self.calib_until:
            alpha = 1.0 / min(self.base_n + 1, cfg.baseline_frames)
            self.base_mu += alpha * (m - self.base_mu)
            self.base_sd += alpha * (max(abs(m - self.base_mu), 0.02) - self.base_sd)
            self.base_n += 1

        self.history.append((round(now, 1), round(max(min(z, 99.0), -9.0), 2)))

        if cfg.breathing and now - self.last_breath_check > 5.0:
            self.last_breath_check = now
            self._breathing(now, cfg)

    def _breathing(self, now: float, cfg) -> None:
        self.breathing = False
        ts, a = self._window(BREATH_WIN, now)
        if ts is None or len(ts) < 60 or ts[-1] - ts[0] < BREATH_WIN * 0.75:
            self.breath_bpm = None
            return
        # a person who just walked away leaves a slow drift that looks like
        # breathing, so the whole window must be free of movement
        if self.muted or now - self.last_active < BREATH_WIN:
            return
        grid = np.arange(ts[0], ts[-1], 1.0 / BREATH_FS)
        a = a[:, self._valid_bins(a)]
        if a.shape[1] < 8:
            return
        a = a / np.maximum(a.mean(axis=1, keepdims=True), 1e-6)  # remove AGC steps
        var = a.var(axis=0)
        pick = np.argsort(var)[-8:]
        spec = None
        x = np.arange(grid.size)
        win = np.hanning(grid.size)
        for k in pick:
            s = np.interp(grid, ts, a[:, k])
            s = s - np.polyval(np.polyfit(x, s, 1), x)
            p = np.abs(np.fft.rfft(s * win)) ** 2
            spec = p if spec is None else spec + p
        f = np.fft.rfftfreq(grid.size, 1.0 / BREATH_FS)
        band = (f >= 0.15) & (f <= 0.6)
        ref = (f >= 0.05) & (f <= 2.0)
        if not band.any():
            return
        i = int(np.argmax(np.where(band, spec, 0)))
        snr = float(spec[i] / max(np.median(spec[ref]), 1e-12))
        self.breath_snr = snr
        self.breath_bpm = round(float(f[i]) * 60.0, 1)
        self.breathing = snr >= cfg.breath_snr

    # ---------------------------------------------------------------- output
    def summary(self, now: float) -> dict:
        return {
            "node": self.node, "src": self.src, "rssi": self.rssi, "channel": self.channel,
            "rate": round(self.rate(now), 1), "z": round(self.z, 2),
            "motion": round(self.motion, 3), "active": self.active,
            "breathing": self.breathing, "bpm": self.breath_bpm,
            "breath_snr": round(self.breath_snr, 1), "stale": bool(self.stale(now)),
            "muted": self.muted, "calibrating": now < self.calib_until,
            "calibrated": self.base_n >= 1000,
        }


class LinkTable:
    def __init__(self, max_links: int = 256):
        self.links: dict[tuple[str, str], Link] = {}
        self.max_links = max_links
        self.dropped = 0

    def push(self, frame, t: float | None = None):
        key = (frame.node, frame.src)
        link = self.links.get(key)
        if link is None:
            if len(self.links) >= self.max_links:
                self._evict()
                if len(self.links) >= self.max_links:
                    self.dropped += 1
                    return
            link = self.links[key] = Link(frame.node, frame.src)
        if link.accept_format(frame.fmt):
            link.push(t if t is not None else time.time(), frame.amp, frame.rssi, frame.channel)

    def _evict(self):
        now = time.time()
        for k, l in list(self.links.items()):
            if now - l.last_t > 300:
                del self.links[k]

    def update(self, now: float, cfg):
        for l in self.links.values():
            if not l.stale(now):
                l.update(now, cfg)
            else:
                l.active = False
                l.breathing = False
