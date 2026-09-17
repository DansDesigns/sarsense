"""In-process scene harness: simulator -> DSP -> tracker, on a fake clock."""
import math
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from sarsense import protocol  # noqa: E402
from sarsense.dsp import LinkTable  # noqa: E402
from sarsense.store import Settings, Store  # noqa: E402
from sarsense.tracker import Proj, Tracker, dist_m  # noqa: E402
from simulate import World  # noqa: E402

LAT, LON = 50.6725, -3.4870


def run(seconds=120, quiet=20, walkers=2, still=1, seed=7, responder=True, verbose=False, **over):
    cfg = Settings(data_dir=tempfile.mkdtemp())
    for k, v in over.items():
        setattr(cfg, k, v)
    store = Store(cfg.data_dir)
    links = LinkTable(cfg.max_links)
    tr = Tracker(store, cfg)
    w = World(walkers=walkers, still=still, seed=seed)
    pj = Proj(LAT, LON)
    for mac, p in w.devices.items():
        lat, lon = pj.ll(*p)
        store.devices[mac] = {"mac": mac, "kind": "ap" if mac == w.ap_mac else "sensor", "lat": lat, "lon": lon, "name": ""}
    r = 40
    store.put_zone({"name": "Z", "type": "tagging",
                    "polygon": [list(pj.ll(r * math.cos(t), r * math.sin(t))) for t in np.linspace(0, 6.28, 8, endpoint=False)]})
    pid = store.put_person({"name": "Sam"})["id"] if responder and walkers else None
    t0 = 1_000_000.0
    steps = int(seconds / w.dt)
    tick_every = int(round(1 / (cfg.tick_hz * w.dt)))
    errs, ndet = [], []
    for i in range(steps):
        now = t0 + i * w.dt
        el = i * w.dt
        present = el > quiet
        people = w.people(el, present)
        for rx, tx, rssi, iq in w.frames(people):
            links.push(protocol.parse(protocol.build_csi(rx, tx, rssi, 6, iq, i)), now)
        if pid and present and i % int(3 / w.dt) == 0:
            store.checkin(pid, *pj.ll(*w.walkers[0].p))
            store.people[pid]["updated"] = now
        if i % tick_every == 0:
            links.update(now, cfg)
            dets = tr.detections(list(links.links.values()), now)
            tr.step(list(links.links.values()), now)
            if present:
                truth = [pj.ll(*p) for p, _ in people]
                ndet.append(len(dets))
                for d in dets:
                    errs.append(min(dist_m((d["lat"], d["lon"]), t) for t in truth))
            if verbose and i % (tick_every * 10) == 0:
                tp = [tuple(np.round(p, 1)) for p, _ in people]
                dp = [tuple(np.round(pj.xy(d["lat"], d["lon"]), 1)) + (("S" if d["breathing"] else "M"),) for d in dets]
                print(f"{el:6.1f} truth={tp} dets={dp}")
    tracks = list(tr.tracks.values())
    return {
        "unknown_tracks": sum(t.kind == "unknown" for t in tracks),
        "known_tracks": sum(t.kind == "known" for t in tracks),
        "active_unknown_end": sum(t.kind == "unknown" and t.status == "active" for t in tracks),
        "mean_dets": float(np.mean(ndet)) if ndet else 0,
        "median_err_m": float(np.median(errs)) if errs else None,
        "p90_err_m": float(np.percentile(errs, 90)) if errs else None,
        "tracks": [(t.label, t.status, t.moving, t.breathing, round(t.radius, 1), *np.round(pj.xy(t.lat, t.lon), 1)) for t in tracks],
    }


if __name__ == "__main__":
    import json
    res = run(verbose=True)
    print(json.dumps({k: v for k, v in res.items() if k != "tracks"}, indent=1))
    for t in res["tracks"]:
        print(t)
