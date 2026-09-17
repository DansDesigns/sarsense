"""The hub: receives node packets, runs detection, keeps state, talks to peers."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request

from . import protocol
from .dsp import LinkTable
from .store import Store
from .tracker import Tracker

log = logging.getLogger("sarsense")


class NodeProtocol(asyncio.DatagramProtocol):
    def __init__(self, hub: "Hub"):
        self.hub = hub
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.hub.on_packet(data, addr, self.transport)


class Hub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = Store(cfg.data_dir)
        self.links = LinkTable(cfg.max_links)
        self.tracker = Tracker(self.store, cfg)
        self.remote: dict[str, dict] = {}  # hub_id -> last federation payload
        self.upstream_ok = None
        self.packets = 0
        self.bad_packets = 0
        self.started = time.time()
        self.snapshot = {}
        self.snapshot_json = b"{}"
        self.subscribers: set[asyncio.Queue] = set()

    # --------------------------------------------------------------- intake
    def on_packet(self, data, addr, transport):
        msg = protocol.parse(data)
        if msg is None:
            self.bad_packets += 1
            return
        self.packets += 1
        if isinstance(msg, protocol.CsiFrame):
            self.links.push(msg)
            dev = self.store.devices.get(msg.node)
            if dev is None or time.time() - dev.get("last_seen", 0) > 2:
                self.store.seen_device(msg.node, "sensor", addr=addr[0])
            if msg.src not in self.store.devices:
                # unknown transmitters appear on the map list so they can be
                # placed (a router, say); they are never named automatically
                self.store.seen_device(msg.src, "transmitter")
        elif isinstance(msg, protocol.Hello):
            self.store.seen_device(msg.node, "sensor", addr=addr[0], bssid=msg.bssid,
                                   channel=msg.channel, ap_rssi=msg.rssi,
                                   uptime=msg.uptime, firmware=msg.firmware)
            if msg.bssid != "00:00:00:00:00:00":
                ap = self.store.seen_device(msg.bssid, "ap", channel=msg.channel)
                ap["kind"] = "ap" if ap["kind"] == "transmitter" else ap["kind"]
            peers = [msg.bssid] + [m for m, d in self.store.devices.items()
                                   if d["kind"] == "sensor" and d.get("bssid") == msg.bssid
                                   and m != msg.node]
            transport.sendto(protocol.build_peers(peers), addr)

    # ------------------------------------------------------------ processing
    async def run(self):
        period = 1.0 / self.cfg.tick_hz
        last_save = 0.0
        while True:
            t0 = time.time()
            try:
                self.links.update(t0, self.cfg)
                self.tracker.step(list(self.links.links.values()), t0)
                self.build_snapshot(t0)
                if t0 - last_save > 10:
                    self.store.save()
                    last_save = t0
            except Exception:  # keep the hub alive whatever happens
                log.exception("processing tick failed")
            await asyncio.sleep(max(0.05, period - (time.time() - t0)))

    def build_snapshot(self, now):
        links = [l.summary(now) for l in self.links.links.values()]
        tracks = [t.public() for t in self.tracker.tracks.values()
                  if t.status in ("active", "lost") or now - t.last_seen < 1800]
        remote_tracks, remote_devices = [], []
        for hid, r in list(self.remote.items()):
            if now - r["received"] > 60:
                continue
            for t in r.get("tracks", []):
                t = dict(t, remote=True)
                remote_tracks.append(t)
            for d in r.get("devices", []):
                remote_devices.append(dict(d, hub=hid))
        people = [dict(p, fresh=now - p.get("updated", 0) < self.cfg.responder_fresh)
                  for p in self.store.people.values()]
        counts = {"unknown": 0, "known": 0, "untagged": 0, "lost": 0}
        for t in tracks + remote_tracks:
            if t["status"] == "active":
                counts[t["kind"]] = counts.get(t["kind"], 0) + 1
            elif t["status"] == "lost" and t["kind"] == "unknown":
                counts["lost"] += 1
        self.snapshot = {
            "hub": self.cfg.hub_id, "now": now,
            "devices": list(self.store.devices.values()),
            "remote_devices": remote_devices,
            "links": links, "tracks": tracks, "remote_tracks": remote_tracks,
            "people": people, "zones": list(self.store.zones.values()),
            "events": self.tracker.events[-60:], "counts": counts,
            "view": self.store.view,
            "stats": {"packets": self.packets, "bad": self.bad_packets,
                      "links": len(links), "dropped_links": self.links.dropped,
                      "uptime": int(now - self.started), "upstream": self.upstream_ok,
                      "hubs": sorted(k for k, r in self.remote.items() if now - r["received"] < 60),
                      "pin_required": bool(self.cfg.pin)},
        }
        self.snapshot_json = json.dumps(self.snapshot, separators=(",", ":")).encode()
        for q in list(self.subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(self.snapshot_json)

    # ----------------------------------------------------------- federation
    def federation_payload(self):
        now = time.time()
        return {
            "hub": self.cfg.hub_id, "sent": now,
            "tracks": [t.public() for t in self.tracker.tracks.values()
                       if t.status in ("active", "lost")],
            "devices": [d for d in self.store.devices.values() if d.get("lat") is not None],
            "people": list(self.store.people.values()),
        }

    def _merge_people(self, people):
        for p in people:
            if not isinstance(p, dict) or "id" not in p:
                continue
            cur = self.store.people.get(p["id"])
            if cur is None or p.get("updated", 0) > cur.get("updated", 0):
                self.store.people[p["id"]] = p
                self.store.mark()

    def accept_federation(self, payload):
        hid = str(payload.get("hub", "?"))[:16]
        if hid == self.cfg.hub_id:
            raise ValueError("hub id clash, give each hub its own --hub-id")
        payload["received"] = time.time()
        self._merge_people(payload.pop("people", []))
        self.remote[hid] = payload
        return {
            "zones": [z for z in self.store.zones.values()],
            "people": list(self.store.people.values()),
        }

    def merge_from_upstream(self, reply):
        for zid in [k for k, z in self.store.zones.items() if z.get("origin") == "upstream"]:
            del self.store.zones[zid]
        for z in reply.get("zones", []):
            z = dict(z, origin="upstream")
            self.store.put_zone(z)
        self._merge_people(reply.get("people", []))
        self.store.mark()

    async def federation_loop(self):
        if not self.cfg.upstream:
            return
        url = self.cfg.upstream.rstrip("/") + "/api/federate"
        loop = asyncio.get_running_loop()

        def post(body):
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "Content-Type": "application/json", "X-SARSense-Key": self.cfg.fed_key})
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())

        while True:
            try:
                body = json.dumps(self.federation_payload()).encode()
                reply = await loop.run_in_executor(None, post, body)
                self.merge_from_upstream(reply)
                self.upstream_ok = True
            except Exception as e:
                if self.upstream_ok is not False:
                    log.warning("upstream %s unreachable: %s", url, e)
                self.upstream_ok = False
            await asyncio.sleep(2.0)
