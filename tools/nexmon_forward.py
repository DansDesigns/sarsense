#!/usr/bin/env python3
"""Relay Nexmon CSI from a Raspberry Pi (3B+/4B/5, bcm43455c0) to a SARSense hub.

nexmon_csi emits its CSI as UDP broadcasts on the Pi itself (port 5500), which
never leave the Pi. This script picks them up and wraps each one as a SARSense
type-3 packet so the hub can treat the Pi like any other sensor node.

Set up nexmon_csi first (https://github.com/seemoo-lab/nexmon_csi), then e.g.:

    makecsiparams -c 36/20 -C 1 -N 1 -m <router mac> -b 0x88
    nexutil -Iwlan0 -s500 -b -l34 -v<params>
    iw phy `iw dev wlan0 info | gawk '/wiphy/ {printf "phy" $2}'` interface add mon0 type monitor
    ip link set mon0 up
    python3 nexmon_forward.py --hub 192.168.1.10

Use a 20 MHz channel and filter on your router's MAC so the Pi reports the
same link consistently. The Pi's on-board Wi-Fi is busy capturing, so reach
the hub over Ethernet. Standard library only.
"""
import argparse
import socket
import struct
import time
import uuid

MAGIC = b"SRS1"
T_HELLO, T_NEXMON = 2, 3


def node_mac(iface):
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            return bytes.fromhex(f.read().strip().replace(":", ""))
    except OSError:
        return uuid.getnode().to_bytes(6, "big")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub", required=True, help="hub IP address")
    ap.add_argument("--hub-port", type=int, default=5566)
    ap.add_argument("--listen-port", type=int, default=5500)
    ap.add_argument("--iface", default="eth0", help="interface whose MAC identifies this node")
    ap.add_argument("--router", default="", help="router MAC, reported to the hub so it can be placed")
    ap.add_argument("--channel", type=int, default=36)
    a = ap.parse_args()

    me = node_mac(a.iface)
    router = bytes.fromhex(a.router.replace(":", "")) if a.router else bytes(6)
    head = struct.pack("<4sBB6s", MAGIC, T_NEXMON, 1, me)

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.bind(("0.0.0.0", a.listen_port))
    rx.settimeout(1.0)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = (a.hub, a.hub_port)

    start = time.time()
    last_hello = 0.0
    count = 0
    print(f"forwarding Nexmon CSI from :{a.listen_port} to {dest[0]}:{dest[1]} as {me.hex(':')}")
    while True:
        now = time.time()
        if now - last_hello > 5:
            last_hello = now
            hello = struct.pack("<4sBB6s6sBbI8s", MAGIC, T_HELLO, 1, me, router, a.channel & 0xFF, 0,
                                int(now - start), b"nexmon")
            tx.sendto(hello, dest)
            if count:
                print(f"{count} CSI packets in the last 5 s")
            count = 0
        try:
            data, _ = rx.recvfrom(4096)
        except socket.timeout:
            continue
        if len(data) >= 18 and data[:2] == b"\x11\x11":
            tx.sendto(head + data, dest)
            count += 1


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
