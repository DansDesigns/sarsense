"""python -m sarsense [options]"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import signal
import socket
import sys

from .hub import Hub, NodeProtocol
from .store import Settings
from .web import WebServer


def parse_args(argv=None) -> Settings:
    s = Settings()
    p = argparse.ArgumentParser(prog="sarsense", description="SARSense Wi-Fi sensing hub")
    p.add_argument("--config", help="JSON file with any of the options below (CLI wins)")
    for f in dataclasses.fields(Settings):
        flag = "--" + f.name.replace("_", "-")
        if f.type in ("bool", bool):
            p.add_argument(flag, dest=f.name, action=argparse.BooleanOptionalAction, default=None)
        else:
            typ = {"int": int, "float": float}.get(str(f.type), str)
            p.add_argument(flag, dest=f.name, type=typ, default=None)
    a = p.parse_args(argv)
    if a.config:
        with open(a.config, encoding="utf-8") as fh:
            for k, v in json.load(fh).items():
                if hasattr(s, k):
                    setattr(s, k, v)
    for f in dataclasses.fields(Settings):
        v = getattr(a, f.name)
        if v is not None:
            setattr(s, f.name, v)
    s.pin = s.pin or os.environ.get("SARSENSE_PIN", "")
    s.fed_key = s.fed_key or os.environ.get("SARSENSE_FED_KEY", "")
    return s


def lan_addresses():
    out = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("192.0.2.1", 9))  # no packet is sent
            out.add(sk.getsockname()[0])
    except OSError:
        pass
    return sorted(out)


async def main_async(cfg: Settings):
    hub = Hub(cfg)
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: NodeProtocol(hub), local_addr=("0.0.0.0", cfg.udp_port))
    logging.info("listening for nodes on UDP %d", cfg.udp_port)
    for ip in lan_addresses():
        logging.info("phones on this network can open http://%s:%d", ip, cfg.http_port)
    if not cfg.pin:
        logging.warning("no operator PIN set (--pin): anyone on the network can edit zones and tracks")
    web = WebServer(hub)
    stop = asyncio.Event()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
    tasks = [asyncio.create_task(c) for c in (hub.run(), web.serve(), hub.federation_loop())]
    try:
        await stop.wait()
    finally:
        for t in tasks:
            t.cancel()
        hub.store.save(force=True)
        logging.info("state saved, bye")


def main(argv=None):
    cfg = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(main_async(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
