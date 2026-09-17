#!/usr/bin/env python3
"""Stream synthetic CSI to a SARSense hub so the whole system can be tried
without any ESP32 hardware.

It invents one router and a ring of sensor nodes, walks some people around,
parks one still (breathing) person near a link, and models each link as a set
of static paths plus one reflected path per person. With --setup it also places
the devices, draws a tagging zone, and registers the first walker as a
responder whose phone position follows them.

    python tools/simulate.py --setup --pin 1234
"""
from __future__ import annotations

import argparse
import json
import math
import random
import socket
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sarsense import protocol  # noqa: E402
from sarsense.tracker import Proj  # noqa: E402

C = 299_792_458.0
FREQS = 2.437e9 + (np.arange(64) - 32) * 312.5e3
NULLS = np.array([0, 1, 2, 3, 4, 5, 32, 59, 60, 61, 62, 63])


def api(base, method, path, body, pin):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Pin": pin})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


class Walker:
    def __init__(self, radius, speed, rng):
        self.r, self.speed, self.rng = radius, speed, rng
        self.p = np.array(rng.uniform(-radius, radius, 2))
        self.goal = self.p.copy()
        self.pause = 0.0

    def step(self, dt):
        if self.pause > 0:
            self.pause -= dt
            return False
        d = self.goal - self.p
        n = np.linalg.norm(d)
        if n < 0.5:
            self.goal = np.array(self.rng.uniform(-self.r, self.r, 2))
            self.pause = self.rng.uniform(0, 6)
            return False
        self.p = self.p + d / n * min(self.speed * dt, n)
        return True



class World:
    """The synthetic scene. Also used by tests/test_scene.py without a network."""

    def __init__(self, sensors=6, ring=30.0, walkers=2, still=1, seed=7, rate=20.0):
        self.rng = rng = np.random.default_rng(seed)
        self.dt = 1.0 / rate
        self.ap_mac = "02:5a:00:00:00:01"
        self.devices = {self.ap_mac: np.array([0.0, 0.0])}
        self.sensors = []
        for i in range(sensors):
            ang = 2 * math.pi * i / sensors
            mac = f"02:5a:00:00:01:{i + 1:02x}"
            self.devices[mac] = np.array([ring * math.cos(ang), ring * math.sin(ang)])
            self.sensors.append(mac)
        self.links = [(rx, tx) for rx in self.sensors for tx in self.devices if tx != rx]
        self.static = {}
        for l in self.links:
            g = rng.normal(size=6) + 1j * rng.normal(size=6)
            tau = rng.uniform(0, 80e-9, 6)
            self.static[l] = np.exp(-2j * np.pi * np.outer(FREQS, tau)) @ g
        self.walkers = [Walker(ring * 0.8, 1.1, rng) for _ in range(walkers)]
        self.still = []
        for i in range(still):
            # lie 1 m off the line between two neighbouring sensors
            p0 = self.devices[self.sensors[i % sensors]]
            p1 = self.devices[self.sensors[(i + 1) % sensors]]
            mid = (p0 + p1) / 2
            normal = np.array([-(p1 - p0)[1], (p1 - p0)[0]])
            self.still.append(mid + normal / np.linalg.norm(normal) * 1.0)

    def people(self, el, present):
        out = []
        if present:
            for w in self.walkers:
                w.step(self.dt)
                out.append((w.p, 0.0))
            for p in self.still:
                out.append((p, 0.006 * math.sin(2 * math.pi * 0.27 * el)))  # 16 breaths/min
        return out

    def frames(self, people):
        rng = self.rng
        for (rx, tx) in self.links:
            prx, ptx = self.devices[rx], self.devices[tx]
            direct = np.linalg.norm(prx - ptx)
            h = self.static[(rx, tx)].copy()
            ref = np.abs(h).mean()
            for pos, chest in people:
                L = np.linalg.norm(ptx - pos) + np.linalg.norm(pos - prx) + chest
                amp = 0.9 * ref * math.exp(-(L - direct) / 0.8)
                if amp > 1e-3 * ref:
                    h = h + amp * np.exp(-2j * np.pi * FREQS * L / C)
            h = h + (rng.normal(size=64) + 1j * rng.normal(size=64)) * 0.03 * ref
            h *= 28.0 / max(np.abs(h).mean(), 1e-9)
            h[NULLS] = 0
            iq = np.empty(128, dtype=np.int8)
            iq[0::2] = np.clip(np.round(h.imag), -127, 127)
            iq[1::2] = np.clip(np.round(h.real), -127, 127)
            rssi = int(-35 - 20 * math.log10(max(direct, 1)))
            yield rx, tx, rssi, iq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub", default="127.0.0.1")
    ap.add_argument("--udp-port", type=int, default=5566)
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--pin", default="")
    ap.add_argument("--lat", type=float, default=50.6725)
    ap.add_argument("--lon", type=float, default=-3.4870)
    ap.add_argument("--sensors", type=int, default=6)
    ap.add_argument("--ring", type=float, default=30.0, help="sensor ring radius, metres")
    ap.add_argument("--rate", type=float, default=20.0, help="CSI frames per link per second")
    ap.add_argument("--walkers", type=int, default=2)
    ap.add_argument("--still", type=int, default=1, help="people lying still near a link")
    ap.add_argument("--quiet-first", type=float, default=20.0,
                    help="seconds with nobody present so the hub learns a baseline")
    ap.add_argument("--setup", action="store_true", help="place devices, draw a zone, add a responder")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    random.seed(a.seed)
    w = World(a.sensors, a.ring, a.walkers, a.still, a.seed, a.rate)
    devices, sensors, ap_mac, walkers = w.devices, w.sensors, w.ap_mac, w.walkers
    proj = Proj(a.lat, a.lon)
    base = f"http://{a.hub}:{a.http_port}"

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = (a.hub, a.udp_port)

    responder = None
    if a.setup:
        # the hub only learns a device once a sensor has reported it
        for mac in sensors:
            sock.sendto(protocol.build_hello(mac, ap_mac, 6, -40, 0), dest)
        time.sleep(1.0)
        for mac, p in devices.items():
            lat, lon = proj.ll(*p)
            name = "Router" if mac == ap_mac else f"Sensor {mac[-2:]}"
            api(base, "POST", f"/api/devices/{mac}", {"lat": lat, "lon": lon, "name": name}, a.pin)
        r = a.ring * 1.25
        poly = [list(proj.ll(r * math.cos(t), r * math.sin(t))) for t in np.linspace(0, 2 * math.pi, 8, endpoint=False)]
        api(base, "POST", "/api/zones", {"name": "Collapsed block", "type": "tagging", "polygon": poly}, a.pin)
        api(base, "POST", "/api/view", {"lat": a.lat, "lon": a.lon, "zoom": 18}, a.pin)
        if walkers:
            responder = api(base, "POST", "/api/people", {"name": "Sam (sim)", "team": "Red"}, a.pin)["id"]
        print("placed devices, drew a tagging zone, registered a responder")

    print(f"streaming {len(w.links)} links at {a.rate:g} Hz to {dest[0]}:{dest[1]}; ctrl+c to stop")
    t0 = time.time()
    dt = 1.0 / a.rate
    seq = 0
    last_hello = 0.0
    last_checkin = 0.0
    nxt = time.time()
    while True:
        now = time.time()
        el = now - t0
        present = el > a.quiet_first
        if now - last_hello > 5:
            last_hello = now
            for s in sensors:
                sock.sendto(protocol.build_hello(s, ap_mac, 6, -45, int(el)), dest)
        people = w.people(el, present)
        if responder and present and now - last_checkin > 3:
            last_checkin = now
            lat, lon = proj.ll(*walkers[0].p)
            try:
                api(base, "POST", f"/api/people/{responder}/checkin", {"lat": lat, "lon": lon}, a.pin)
            except Exception as e:
                print("checkin failed:", e)

        for rx, tx, rssi, iq in w.frames(people):
            sock.sendto(protocol.build_csi(rx, tx, rssi, 6, iq, seq), dest)
            seq += 1
        nxt += dt
        time.sleep(max(0.0, nxt - time.time()))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
