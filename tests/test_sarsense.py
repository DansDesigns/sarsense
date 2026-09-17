"""python -m unittest discover -s tests   (no extra packages needed)"""
import asyncio
import json
import struct
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scene  # noqa: E402
from sarsense import protocol  # noqa: E402
from sarsense.store import Settings  # noqa: E402
from sarsense.tracker import point_in_poly  # noqa: E402


class Protocol(unittest.TestCase):
    def test_esp_roundtrip(self):
        iq = np.zeros(128, dtype=np.int8)
        iq[2 * 10] = 3   # imag of subcarrier 10
        iq[2 * 10 + 1] = 4  # real
        f = protocol.parse(protocol.build_csi("aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02", -50, 6, iq, 1))
        self.assertEqual((f.node, f.src, f.rssi, f.channel), ("aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02", -50, 6))
        self.assertAlmostEqual(float(f.amp[10]), 5.0)

    def test_hello(self):
        h = protocol.parse(protocol.build_hello("aa:bb:cc:dd:ee:01", "11:22:33:44:55:66", 11, -60, 99, "v1"))
        self.assertEqual((h.bssid, h.channel, h.uptime, h.firmware), ("11:22:33:44:55:66", 11, 99, "v1"))

    def test_nexmon_wrapped(self):
        csi = np.zeros(256 * 2, dtype="<i2")
        csi[2 * 100] = 30
        csi[2 * 100 + 1] = 40
        raw = struct.pack("<HbB6sHHHH", 0x1111, -42, 0, bytes(6), 0, 0, 0xE09B, 0x4345) + csi.tobytes()
        pkt = protocol.HDR.pack(protocol.MAGIC, protocol.T_NEXMON, 1, bytes.fromhex("0102030405ff")) + raw
        f = protocol.parse(pkt)
        self.assertEqual(f.rssi, -42)
        self.assertEqual(f.amp.shape, (64,))
        self.assertGreater(float(f.amp[25]), 0)

    def test_garbage(self):
        self.assertIsNone(protocol.parse(b"hello world"))
        self.assertIsNone(protocol.parse(protocol.MAGIC + b"\x01"))

    def test_peers(self):
        p = protocol.build_peers(["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"], "Sensor 03")
        self.assertEqual(p[12], 2)
        self.assertEqual(len(p), 13 + 12 + 1 + len("Sensor 03"))
        self.assertTrue(p.endswith(b"Sensor 03"))
        self.assertEqual(len(protocol.build_peers([])), 14)


class Stations(unittest.TestCase):
    def test_names_are_handed_out(self):
        from sarsense.store import Store
        st = Store(tempfile.mkdtemp())
        st.seen_device("aa:bb:cc:dd:ee:01", "sensor")
        st.seen_device("aa:bb:cc:dd:ee:02", "sensor")
        st.seen_device("11:22:33:44:55:66", "ap")
        names = [d["name"] for d in st.devices.values()]
        self.assertEqual(names, ["Sensor 01", "Sensor 02", "Router 01"])
        st.devices["aa:bb:cc:dd:ee:01"]["name"] = "Front door"
        st.seen_device("aa:bb:cc:dd:ee:01", "sensor")
        self.assertEqual(st.devices["aa:bb:cc:dd:ee:01"]["name"], "Front door")

    def test_blank_mac_is_ignored(self):
        from sarsense.hub import Hub
        cfg = Settings(data_dir=tempfile.mkdtemp())
        hub = Hub(cfg)
        pkt = protocol.build_hello("00:00:00:00:00:00", "11:22:33:44:55:66", 6, -50, 1)
        hub.on_packet(pkt, ("10.0.0.5", 5566), None)
        self.assertEqual(hub.store.devices, {})
        self.assertEqual(hub.packets, 0)


class Alerts(unittest.TestCase):
    def test_alert_lifecycle(self):
        from sarsense.store import Store
        st = Store(tempfile.mkdtemp())
        pid = st.put_person({"name": "Sam"})["id"]
        a = st.add_alert("help", 50.1, -3.5, "leg trapped", pid, "A")
        self.assertEqual((a["who"], a["status"]), ("Sam", "new"))
        self.assertEqual(len(st.live_alerts()), 1)
        st.set_alert(a["id"], "cleared")
        self.assertEqual(st.live_alerts(), [])


class Geometry(unittest.TestCase):
    def test_point_in_poly(self):
        sq = [[0, 0], [0, 1], [1, 1], [1, 0]]
        self.assertTrue(point_in_poly(0.5, 0.5, sq))
        self.assertFalse(point_in_poly(1.5, 0.5, sq))


class Tagging(unittest.TestCase):
    def test_named_missing_person_stays_unknown(self):
        from sarsense.store import Store
        from sarsense.tracker import Track, Tracker
        cfg = Settings(data_dir=tempfile.mkdtemp())
        tr = Tracker(Store(cfg.data_dir), cfg)
        tr.tracks["A-U001"] = Track(id="A-U001", kind="unknown", label="U001", lat=0, lon=0,
                                    radius=5, first_seen=0, last_seen=0)
        t = tr.act("A-U001", "identify", {"name": "Jane Doe"})
        self.assertEqual((t.kind, t.label, t.name), ("unknown", "U001", "Jane Doe"))


class Scenes(unittest.TestCase):
    def test_empty_area_raises_nothing(self):
        r = scene.run(seconds=90, walkers=0, still=0, responder=False, seed=1)
        self.assertEqual(r["unknown_tracks"], 0)

    def test_still_person_found_with_breathing(self):
        r = scene.run(seconds=100, walkers=0, still=1, responder=False, seed=4)
        self.assertEqual(r["unknown_tracks"], 1)
        self.assertTrue(r["tracks"][0][3], "track should be flagged as breathing")
        self.assertLess(r["median_err_m"], 8)

    def test_walker_does_not_fragment_badly(self):
        r = scene.run(seconds=90, walkers=1, still=0, responder=False, seed=3)
        self.assertGreaterEqual(r["unknown_tracks"], 1)
        self.assertLessEqual(r["unknown_tracks"], 3)


class Api(unittest.TestCase):
    def test_http_roundtrip(self):
        from sarsense.hub import Hub
        from sarsense.web import WebServer

        cfg = Settings(data_dir=tempfile.mkdtemp(), http_host="127.0.0.1", http_port=18080,
                       pin="42", tiles_offline=True)

        async def go():
            hub = Hub(cfg)
            hub.build_snapshot(0)
            web = WebServer(hub)
            srv = asyncio.create_task(web.serve())
            await asyncio.sleep(0.3)
            loop = asyncio.get_running_loop()

            def call(method, path, body=None, pin=""):
                req = urllib.request.Request(f"http://127.0.0.1:18080{path}", method=method,
                                             data=json.dumps(body).encode() if body is not None else None,
                                             headers={"X-Pin": pin, "Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=3) as r:
                        return r.status, r.read()
                except urllib.error.HTTPError as e:
                    return e.code, e.read()

            poly = {"name": "A", "polygon": [[0, 0], [0, 1], [1, 1]]}
            st, _ = await loop.run_in_executor(None, call, "POST", "/api/zones", poly)
            self.assertEqual(st, 401)
            st, body = await loop.run_in_executor(None, lambda: call("POST", "/api/zones", poly, "42"))
            self.assertEqual(st, 200)
            self.assertEqual(json.loads(body)["type"], "tagging")
            st, body = await loop.run_in_executor(None, call, "POST", "/api/people", {"name": "Jo"})
            pid = json.loads(body)["id"]
            st, _ = await loop.run_in_executor(None, call, "POST", f"/api/people/{pid}/checkin", {"lat": 1, "lon": 2})
            self.assertEqual(st, 200)
            st, body = await loop.run_in_executor(None, call, "GET", "/")
            self.assertIn(b"SARSense", body)
            st, _ = await loop.run_in_executor(None, call, "GET", "/../store.py")
            self.assertEqual(st, 404)
            st, _ = await loop.run_in_executor(None, call, "POST", "/api/federate", {"hub": "B"})
            self.assertEqual(st, 403)
            srv.cancel()

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
