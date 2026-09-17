"""Turn active links into detections, detections into tracks, tracks into tags.

Localisation is deliberately coarse. A link senses a region around the line
between its two ends, so each active link votes for its midpoint, and where
two active links cross the crossing point gets extra weight. Votes are
clustered and each cluster becomes one detection with an uncertainty radius.

CSI cannot say *who* someone is. Track IDs are continuity labels only: two
people meeting or crossing can merge or swap. "Known" means a checked-in
responder was standing where the detection is.
"""
from __future__ import annotations

import math
import time

import numpy as np
from dataclasses import dataclass, field, asdict

R_EARTH = 6371000.0


# ------------------------------------------------------------------ geometry
class Proj:
    """Equirectangular projection around a reference point, metres."""

    def __init__(self, lat0: float, lon0: float):
        self.lat0, self.lon0 = lat0, lon0
        self.k = math.cos(math.radians(lat0))

    def xy(self, lat, lon):
        return (math.radians(lon - self.lon0) * R_EARTH * self.k,
                math.radians(lat - self.lat0) * R_EARTH)

    def ll(self, x, y):
        return (self.lat0 + math.degrees(y / R_EARTH),
                self.lon0 + math.degrees(x / (R_EARTH * self.k)))


def dist_m(a, b) -> float:
    p = Proj(a[0], a[1])
    x, y = p.xy(b[0], b[1])
    return math.hypot(x, y)


def point_in_poly(lat, lon, poly) -> bool:
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        yi, xi = poly[i]
        yj, xj = poly[j]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def seg_intersect(p1, p2, p3, p4):
    d = (p2[0] - p1[0]) * (p4[1] - p3[1]) - (p2[1] - p1[1]) * (p4[0] - p3[0])
    if abs(d) < 1e-9:
        return None
    t = ((p3[0] - p1[0]) * (p4[1] - p3[1]) - (p3[1] - p1[1]) * (p4[0] - p3[0])) / d
    u = ((p3[0] - p1[0]) * (p2[1] - p1[1]) - (p3[1] - p1[1]) * (p2[0] - p1[0])) / d
    if 0 <= t <= 1 and 0 <= u <= 1:
        return (p1[0] + t * (p2[0] - p1[0]), p1[1] + t * (p2[1] - p1[1]))
    return None


# ------------------------------------------------------------------ records
@dataclass
class Track:
    id: str
    kind: str                 # unknown | known | untagged
    label: str
    lat: float
    lon: float
    radius: float
    first_seen: float
    last_seen: float
    zone: str | None = None
    moving: bool = True
    breathing: bool = False
    bpm: float | None = None
    confidence: float = 0.0
    status: str = "active"    # active | lost | found | dismissed | merged
    responder_nearby: str | None = None
    person: str | None = None
    name: str = ""            # operator-given name for a missing person
    note: str = ""
    hub: str = ""
    updates: int = 0
    resp_hits: int = 0
    resp_logged: float = 0.0
    trail: list = field(default_factory=list)

    def public(self):
        d = asdict(self)
        d["trail"] = self.trail[-30:]
        return d


class Tracker:
    def __init__(self, store, cfg):
        self.store = store
        self.cfg = cfg
        self.tracks: dict[str, Track] = {}
        self.pending: list[dict] = []   # detections waiting for confirmation
        self._kcache: dict = {}
        self._kcache_bytes = 0
        self.events: list[dict] = []

    # -------------------------------------------------------------- helpers
    def _device_pos(self, mac):
        d = self.store.devices.get(mac)
        if d and d.get("lat") is not None:
            return (d["lat"], d["lon"])
        return None

    def _zone_for(self, lat, lon):
        tag = None
        for z in self.store.zones.values():
            if point_in_poly(lat, lon, z["polygon"]):
                if z["type"] == "ignore":
                    return z, True
                tag = tag or z
        return tag, False

    def _log(self, kind, track, text):
        ev = {"t": time.time(), "kind": kind, "track": track.id, "label": track.label,
              "lat": round(track.lat, 6), "lon": round(track.lon, 6), "text": text}
        self.events.append(ev)
        del self.events[:-500]
        self.store.append_log(ev)

    # ------------------------------------------------------------ detections
    def detections(self, links, now=None):
        """Radio-tomography-lite on a local metre grid.

        Every placed link paints an ellipse-shaped weight (excess path length
        through a cell) onto the grid. Active links add positive evidence and
        quiet links add negative evidence, so a cell crossed by one busy link
        and three quiet ones scores low, while the crossing of two busy links
        scores high. Peaks of the resulting image become detections. Links
        whose transmitter is not placed paint a disc around the sensor instead.
        """
        cfg = self.cfg
        now = now or time.time()
        items = []   # (a_xy, b_xy, motion score, still score, bpm)
        placed = [l for l in links if not l.stale(now) and self._device_pos(l.node)]
        if not any(l.active or l.breathing for l in placed):
            return []
        ref = self._device_pos(placed[0].node)
        pj = Proj(*ref)
        for l in placed:
            a = pj.xy(*self._device_pos(l.node))
            bp = self._device_pos(l.src)
            b = pj.xy(*bp) if bp else None
            if l.muted or l.calib_until > now:
                continue
            if l.active:
                # every busy link counts about once, so one long busy link on
                # its own cannot outvote the crossing of several
                m = 1.0 + min(0.5, 0.25 * math.log10(max(l.z / cfg.z_on, 1.0)))
            elif l.z < cfg.z_off:
                m = -cfg.quiet_weight
            else:
                m = 0.0  # between thresholds: no evidence either way
            if l.breathing and not l.active:
                st = 1.0 + min(0.5, 0.25 * math.log10(max(l.breath_snr / cfg.breath_snr, 1.0)))
            elif l.active:
                st = 0.0
            else:
                st = -cfg.quiet_weight
            items.append((a, b, m, st, l.breath_bpm if st > 0 else None))

        out = []
        for group in self._groups(items):
            out.extend(self._image_peaks(group, pj))
        return out

    def _groups(self, items):
        """Split into clusters around active links so a city-wide hub never
        builds one enormous grid."""
        hot = [it for it in items if it[2] > 0 or it[3] > 0]
        boxes = []
        pad = self.cfg.cluster_radius
        for it in hot:
            pts = [it[0]] + ([it[1]] if it[1] else [])
            bx = [min(p[0] for p in pts) - pad, min(p[1] for p in pts) - pad,
                  max(p[0] for p in pts) + pad, max(p[1] for p in pts) + pad]
            for b in boxes:
                if not (bx[0] > b[2] or bx[2] < b[0] or bx[1] > b[3] or bx[3] < b[1]):
                    b[0], b[1] = min(b[0], bx[0]), min(b[1], bx[1])
                    b[2], b[3] = max(b[2], bx[2]), max(b[3], bx[3])
                    break
            else:
                boxes.append(bx)
        groups = []
        for b in boxes:
            members = []
            for it in items:
                pts = [it[0]] + ([it[1]] if it[1] else [])
                if any(b[0] <= p[0] <= b[2] and b[1] <= p[1] <= b[3] for p in pts):
                    members.append(it)
            groups.append((b, members))
        return groups

    def _kernel(self, grid_key, gx, gy, a, b):
        key = (grid_key, round(a[0], 2), round(a[1], 2), None if b is None else (round(b[0], 2), round(b[1], 2)))
        k = self._kcache.get(key)
        if k is not None:
            return k
        cfg = self.cfg
        da = np.hypot(gx - a[0], gy - a[1])
        if b is None:
            k = np.exp(-da / cfg.single_end_radius)
        else:
            d = math.hypot(b[0] - a[0], b[1] - a[1])
            excess = da + np.hypot(gx - b[0], gy - b[1]) - d
            k = np.exp(-excess / max(cfg.ellipse_width, 0.04 * d))
        k = k.astype(np.float32)
        self._kcache_bytes += k.nbytes
        while self._kcache and self._kcache_bytes > 32 * 1024 * 1024:
            old = self._kcache.pop(next(iter(self._kcache)))
            self._kcache_bytes -= old.nbytes
        self._kcache[key] = k
        return k

    def _image_peaks(self, group, pj):
        """Greedy CLEAN: find the best-supported point, record it, remove the
        busy links that pass through it, repeat. Removing explained links is
        what stops one person producing ghost detections where their links
        happen to cross elsewhere."""
        cfg = self.cfg
        (x0, y0, x1, y1), items = group
        cell = max(cfg.grid_cell, max(x1 - x0, y1 - y0) / 200.0)
        snap = cell * 8   # snapped grid origins repeat, so kernels can be cached
        x0, y0 = math.floor(x0 / snap) * snap, math.floor(y0 / snap) * snap
        x1, y1 = math.ceil(x1 / snap) * snap, math.ceil(y1 / snap) * snap
        xs = np.arange(x0, x1 + cell / 2, cell, dtype=np.float32)
        ys = np.arange(y0, y1 + cell / 2, cell, dtype=np.float32)
        gx, gy = np.meshgrid(xs, ys)
        grid_key = (round(x0, 2), round(y0, 2), round(cell, 3), gx.shape)
        quiet_m = np.zeros_like(gx)
        quiet_s = np.zeros_like(gx)
        hot = []   # [kernel, motion weight, still weight, bpm]
        for a, b, m, st, bpm in items:
            k = self._kernel(grid_key, gx, gy, a, b)
            if m < 0:
                quiet_m += m * k
            if st < 0:
                quiet_s += st * k
            if m > 0 or st > 0:
                hot.append([k, max(m, 0.0), max(st, 0.0), bpm])

        placed_links = sum(1 for it in items if it[1] is not None)
        threshold = cfg.peak_threshold if placed_links >= 3 else 0.8
        dets = []
        for _ in range(12):
            if not hot:
                break
            img_m = quiet_m.copy()
            img_s = quiet_s.copy()
            any_still = False
            for h in hot:
                if h[1]:
                    img_m += h[0] * np.float32(h[1])
                if h[2]:
                    img_s += h[0] * np.float32(h[2])
                    any_still = True
            img_s = img_s * np.float32(cfg.still_gain) if any_still else np.full_like(gx, -1e9)
            img = np.maximum(img_m, img_s)
            iy, ix = np.unravel_index(int(np.argmax(img)), img.shape)
            peak = float(img[iy, ix])
            if peak < threshold:
                break
            still = bool(img_s[iy, ix] >= img_m[iy, ix])
            src = img_s if still else img_m
            w = np.where(src >= 0.6 * peak, src, 0.0)
            near = np.hypot(gx - gx[iy, ix], gy - gy[iy, ix]) <= cfg.peak_separation
            w = np.where(near, w, 0.0)
            cx = float((gx * w).sum() / w.sum())
            cy = float((gy * w).sum() / w.sum())
            radius = math.sqrt(max(int((w > 0).sum()), 1) * cell * cell / math.pi)
            bpm = None
            keep = []
            for h in hot:
                explained = h[0][iy, ix] > 0.35 and (h[2] if still else h[1]) > 0
                if explained and still and h[3] and bpm is None:
                    bpm = h[3]
                if not explained:
                    keep.append(h)
            if len(keep) == len(hot):
                break
            hot = keep
            lat, lon = pj.ll(cx, cy)
            dets.append({"lat": lat, "lon": lon, "radius": round(max(radius, cell), 1),
                         "moving": not still, "breathing": still, "bpm": bpm,
                         "confidence": round(min(1.0, peak / (2 * cfg.peak_threshold)), 2)})
        return dets

    # --------------------------------------------------------------- tracks
    def _nearest_responder(self, lat, lon, now):
        best, bd = None, 1e9
        for p in self.store.people.values():
            if p.get("lat") is None or now - p.get("updated", 0) > self.cfg.responder_fresh:
                continue
            d = dist_m((lat, lon), (p["lat"], p["lon"]))
            if d < bd:
                best, bd = p, d
        if best and bd <= self.cfg.responder_radius:
            return best
        return None

    def step(self, links, now=None):
        now = now or time.time()
        dets = self.detections(links, now)
        live = [t for t in self.tracks.values() if t.status == "active"]
        used = set()
        for d in dets:
            zone, ignored = self._zone_for(d["lat"], d["lon"])
            if ignored:
                continue
            best, bd = None, 1e9
            for t in live:
                if t.id in used or now - t.last_seen > self.cfg.track_timeout:
                    continue
                dd = dist_m((t.lat, t.lon), (d["lat"], d["lon"]))
                if dd < bd:
                    best, bd = t, dd
            if best is None or bd > self.cfg.assoc_radius:
                best = self._reacquire(d, now)
                if best is None:
                    d = self._confirm(d, now)
                    if d is None:
                        continue
                    best = self._new_track(d, zone, now)
                if best not in live:
                    live.append(best)
            else:
                a = self.cfg.smoothing
                best.lat += a * (d["lat"] - best.lat)
                best.lon += a * (d["lon"] - best.lon)
                best.radius = d["radius"]
                best.last_seen = now
                if zone and not best.zone:
                    best.zone = zone["name"]
                    if best.kind == "untagged":
                        self._retag(best, zone, now)
            used.add(best.id)
            best.moving, best.breathing = d["moving"], d["breathing"]
            best.bpm, best.confidence = d["bpm"], d["confidence"]
            if not best.trail or dist_m(best.trail[-1][:2], (best.lat, best.lon)) > 2:
                best.trail.append((round(best.lat, 6), round(best.lon, 6), round(now)))
                del best.trail[:-200]
            r = self._nearest_responder(best.lat, best.lon, now)
            name = r["name"] if r else None
            best.updates += 1
            best.resp_hits += 1 if r else 0
            if (r and best.kind == "unknown" and self.cfg.absorb_seconds > 0
                    and now - best.first_seen <= self.cfg.absorb_seconds
                    and best.updates >= 3 and best.resp_hits == best.updates):
                self._log("absorbed", best, f"{best.label} was only ever seen beside {name}, now shown as {name}. Reopen from the log if that is wrong.")
                best.kind, best.label, best.person = "known", name, r["id"]
            elif best.kind == "unknown" and name and now - best.resp_logged > 300 \
                    and now - best.first_seen > self.cfg.absorb_seconds:
                best.resp_logged = now
                self._log("responder", best, f"{name} is with {best.label}")
            best.responder_nearby = name if best.kind == "unknown" else None

        self._merge_known(now)
        self.pending = [p for p in self.pending if now - p["last"] <= 2.5 / self.cfg.tick_hz]
        for t in live:
            if t.status == "active" and now - t.last_seen > self.cfg.track_timeout:
                t.status = "lost"
                self._log("lost", t, f"{t.label} not detected for {int(self.cfg.track_timeout)} s, last known position kept")
        # forget old non-unknown tracks, never silently drop an unknown one
        for tid, t in list(self.tracks.items()):
            if t.status != "active" and t.kind != "unknown" and now - t.last_seen > 3600:
                del self.tracks[tid]

    def _merge_known(self, now):
        """One responder should be one marker. Keep the track nearest their
        shared position (or the freshest) and fold the rest into it."""
        by_person = {}
        for t in self.tracks.values():
            if t.status == "active" and t.kind == "known" and t.person:
                by_person.setdefault(t.person, []).append(t)
        for pid, ts in by_person.items():
            if len(ts) < 2:
                continue
            p = self.store.people.get(pid) or {}
            if p.get("lat") is not None:
                ts.sort(key=lambda t: dist_m((t.lat, t.lon), (p["lat"], p["lon"])))
            else:
                ts.sort(key=lambda t: -t.last_seen)
            for t in ts[1:]:
                t.status = "merged"

    def _confirm(self, d, now):
        """Only promote a detection once it has been seen on several ticks."""
        best, bd = None, 1e9
        for p in self.pending:
            dd = dist_m((p["lat"], p["lon"]), (d["lat"], d["lon"]))
            if dd < bd and p["last"] < now:
                best, bd = p, dd
        if best is None or bd > self.cfg.assoc_radius:
            self.pending.append(dict(d, hits=1, last=now))
            del self.pending[:-64]
            return None if self.cfg.confirm_ticks > 1 else d
        best.update({k: v for k, v in d.items()})
        best["hits"] += 1
        best["last"] = now
        if best["hits"] >= self.cfg.confirm_ticks:
            self.pending.remove(best)
            return d
        return None

    def _reacquire(self, d, now):
        """A lost unknown that shows up again near where it was keeps its ID."""
        best, bd = None, 1e9
        for t in self.tracks.values():
            if t.status != "lost" or t.kind != "unknown" or now - t.last_seen > self.cfg.reacquire_window:
                continue
            dd = dist_m((t.lat, t.lon), (d["lat"], d["lon"]))
            if dd < bd:
                best, bd = t, dd
        if best is None or bd > self.cfg.assoc_radius:
            return None
        best.status = "active"
        best.last_seen = now
        best.lat, best.lon = d["lat"], d["lon"]
        self._log("reacquired", best, f"{best.label} detected again")
        return best

    def _new_track(self, d, zone, now):
        t = Track(id="", kind="untagged", label="", lat=d["lat"], lon=d["lon"],
                  radius=d["radius"], first_seen=now, last_seen=now, hub=self.cfg.hub_id)
        if zone:
            self._retag(t, zone, now)
        else:
            n = self.store.next_counter("motion")
            t.id = f"{self.cfg.hub_id}-M{n}"
            t.label = f"Motion {n}"
        self.tracks[t.id] = t
        if t.kind != "untagged":
            self._log("new", t, f"{t.label} detected in {t.zone}")
        return t

    def _retag(self, t, zone, now):
        old = t.id
        t.zone = zone["name"]
        r = self._nearest_responder(t.lat, t.lon, now)
        if r:
            t.kind, t.label, t.person = "known", r["name"], r["id"]
            t.id = t.id or f"{self.cfg.hub_id}-K{self.store.next_counter('known')}"
        else:
            n = self.store.next_counter("unknown")
            t.kind, t.label = "unknown", f"U{n:03d}"
            t.id = f"{self.cfg.hub_id}-U{n:03d}"
        if old and old != t.id and old in self.tracks:
            self.tracks[t.id] = self.tracks.pop(old)
            self._log("new", t, f"{t.label} entered {t.zone}")

    # -------------------------------------------------------- operator actions
    def act(self, tid, action, payload):
        t = self.tracks.get(tid)
        if not t:
            return None
        if action == "found":
            t.status = "found"
            self._log("found", t, f"{t.label} marked found. {payload.get('note', '')}".strip())
        elif action == "dismiss":
            t.status = "dismissed"
            self._log("dismissed", t, f"{t.label} dismissed as false alarm")
        elif action == "reopen":
            t.status = "active"
            t.last_seen = time.time()
        elif action == "identify":
            # a named missing person is still someone being searched for, so the
            # track stays an unknown (not a responder) and keeps its ID
            name = (payload.get("name") or "").strip()[:60]
            t.name = name
            if name:
                self._log("identified", t, f"{t.label} identified as {name}")
        elif action == "note":
            t.note = (payload.get("note") or "")[:280]
        return t
