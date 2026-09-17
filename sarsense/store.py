"""Settings and persistent state (devices, zones, people, counters, event log).

State is one JSON file written atomically, plus an append-only CSV event log.
Everything fits comfortably in memory on a 512 MB board.
"""
from __future__ import annotations

import csv
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass


@dataclass
class Settings:
    hub_id: str = "A"
    data_dir: str = "./sarsense-data"
    http_host: str = "0.0.0.0"
    http_port: int = 8080
    udp_port: int = 5566
    pin: str = ""
    upstream: str = ""
    fed_key: str = ""
    tiles: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    tiles_sat: str = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
    tiles_offline: bool = False
    tls_cert: str = ""
    tls_key: str = ""
    max_links: int = 128
    # detection
    z_on: float = 4.0
    z_off: float = 2.0
    on_ticks: int = 2
    baseline_frames: int = 600
    breathing: bool = True
    breath_snr: float = 8.0
    # tracking
    cluster_radius: float = 20.0      # padding around active links when building a grid
    grid_cell: float = 2.0            # metres; localisation is only good to a few metres anyway
    ellipse_width: float = 1.5        # metres of excess path where a link is sensitive
    quiet_weight: float = 1.0         # negative evidence from quiet links
    still_gain: float = 1.0
    peak_threshold: float = 1.6       # roughly two busy links crossing
    confirm_ticks: int = 3            # sightings needed before a detection becomes a track
    peak_separation: float = 7.0      # metres between separate detections
    absorb_seconds: float = 20.0      # new unknowns only ever seen beside a responder become that responder
    assoc_radius: float = 25.0
    single_end_radius: float = 15.0
    track_timeout: float = 20.0
    reacquire_window: float = 900.0
    smoothing: float = 0.35
    responder_radius: float = 15.0
    responder_fresh: float = 180.0
    tick_hz: float = 2.0


class Store:
    def __init__(self, data_dir: str):
        self.dir = data_dir
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "state.json")
        self.log_path = os.path.join(self.dir, "events.csv")
        self.lock = threading.Lock()
        self.devices: dict[str, dict] = {}
        self.zones: dict[str, dict] = {}
        self.people: dict[str, dict] = {}
        self.counters: dict[str, int] = {}
        self.alerts: list[dict] = []
        self.view: dict = {}
        self._dirty = False
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8") as f:
            d = json.load(f)
        self.devices = d.get("devices", {})
        self.zones = d.get("zones", {})
        self.people = d.get("people", {})
        self.counters = d.get("counters", {})
        self.alerts = d.get("alerts", [])
        self.view = d.get("view", {})

    def mark(self):
        self._dirty = True

    def save(self, force=False):
        if not (self._dirty or force):
            return
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"devices": self.devices, "zones": self.zones, "people": self.people,
                           "counters": self.counters, "view": self.view,
                           "alerts": self.alerts[-200:]}, f, indent=1)
            os.replace(tmp, self.path)
            self._dirty = False

    def next_counter(self, name: str) -> int:
        self.counters[name] = self.counters.get(name, 0) + 1
        self.mark()
        return self.counters[name]

    # ------------------------------------------------------------ devices
    def auto_name(self, kind: str) -> str:
        """Hand out the next free name for a station, the way a DHCP server
        hands out addresses. Operators can rename a station afterwards."""
        stem = {"sensor": "Sensor", "ap": "Router"}.get(kind)
        if not stem:
            return ""
        taken = {d.get("name") for d in self.devices.values()}
        while True:
            n = self.next_counter("name_" + kind)
            name = f"{stem} {n:02d}"
            if name not in taken:
                return name

    def seen_device(self, mac: str, kind: str, **info):
        d = self.devices.get(mac)
        if d is None:
            d = self.devices[mac] = {"mac": mac, "kind": kind, "name": "", "lat": None, "lon": None}
            self.mark()
        if kind == "sensor" and d["kind"] != "sensor":
            d["kind"] = "sensor"
            self.mark()
        if not d.get("name") and d["kind"] in ("sensor", "ap"):
            d["name"] = self.auto_name(d["kind"])
            self.mark()
        d.update(info)
        d["last_seen"] = time.time()
        return d

    # ------------------------------------------------------------- zones
    def put_zone(self, z: dict) -> dict:
        poly = [[float(a), float(b)] for a, b in z["polygon"]][:200]
        if len(poly) < 3:
            raise ValueError("A zone needs at least 3 points")
        zid = z.get("id") or uuid.uuid4().hex[:8]
        self.zones[zid] = {"id": zid, "name": str(z.get("name") or "Zone")[:40],
                           "type": "ignore" if z.get("type") == "ignore" else "tagging",
                           "polygon": poly, "origin": z.get("origin", "local")}
        self.mark()
        return self.zones[zid]

    # ------------------------------------------------------------ people
    def put_person(self, p: dict) -> dict:
        pid = p.get("id") or uuid.uuid4().hex[:8]
        cur = self.people.get(pid, {"id": pid, "lat": None, "lon": None, "updated": 0})
        cur["name"] = str(p.get("name") or cur.get("name") or "Responder")[:40]
        cur["team"] = str(p.get("team") or cur.get("team") or "")[:40]
        self.people[pid] = cur
        self.mark()
        return cur

    def checkin(self, pid: str, lat: float, lon: float, acc: float | None = None):
        p = self.people.get(pid)
        if not p:
            return None
        p.update(lat=float(lat), lon=float(lon), acc=acc, updated=time.time())
        self.mark()
        return p

    # -------------------------------------------------------------- alerts
    ALERT_KINDS = {"found": "found someone", "help": "needs help",
                   "hazard": "hazard", "note": "marked a spot"}

    def add_alert(self, kind: str, lat: float, lon: float, text: str, person: str | None, hub: str) -> dict:
        kind = kind if kind in self.ALERT_KINDS else "note"
        who = self.people.get(person or "", {}).get("name", "Someone")
        a = {"id": uuid.uuid4().hex[:8], "t": time.time(), "kind": kind,
             "lat": float(lat), "lon": float(lon), "text": str(text or "")[:200],
             "person": person, "who": who, "hub": hub, "status": "new"}
        self.alerts.append(a)
        del self.alerts[:-200]
        self.mark()
        return a

    def set_alert(self, aid: str, status: str):
        for a in self.alerts:
            if a["id"] == aid:
                a["status"] = status
                self.mark()
                return a
        return None

    def live_alerts(self, max_age: float = 6 * 3600) -> list:
        now = time.time()
        return [a for a in self.alerts if a["status"] != "cleared" and now - a["t"] < max_age]

    # --------------------------------------------------------------- log
    def append_log(self, ev: dict):
        new = not os.path.exists(self.log_path)
        with open(self.log_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time_utc", "kind", "track", "label", "lat", "lon", "text"])
            w.writerow([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ev["t"])), ev["kind"],
                        ev["track"], ev["label"], ev["lat"], ev["lon"], ev["text"]])
