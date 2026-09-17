"""Wire formats.

All SARSense node packets are little-endian UDP datagrams:

    common header (12 bytes)
        4s  magic      b"SRS1"
        B   type       1=CSI  2=HELLO  3=NEXMON  4=TICK  5=PEERS (hub -> node)
        B   version    1
        6s  node_mac   MAC of the receiving/reporting node

    type 1 CSI (20 bytes + payload)
        6s  src_mac    transmitter MAC of the frame the CSI was measured on
        b   rssi       dBm
        b   noise      dBm
        B   channel
        B   flags      bit0 first_word_invalid
        I   seq        node-side counter
        I   ts_us      node rx timestamp (microseconds, wraps)
        H   length     bytes of CSI that follow
        ... int8 pairs [imag, real] as delivered by esp_wifi CSI (L-LTF first)

    type 2 HELLO (20 bytes)
        6s  bssid      AP the node is associated with
        B   channel
        b   rssi       RSSI of the AP
        I   uptime_s
        8s  firmware   ascii, NUL padded

    type 3 NEXMON
        raw nexmon_csi UDP payload (18 byte header + int16 CSI), wrapped by
        tools/nexmon_forward.py running on a Raspberry Pi.

    type 5 PEERS (hub -> node)
        B   count
        6s * count   MACs the node should forward CSI for
        B   name length (optional, may be absent in older hubs)
        ... the name the hub has given this node, utf-8
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

MAGIC = b"SRS1"
T_CSI, T_HELLO, T_NEXMON, T_TICK, T_PEERS = 1, 2, 3, 4, 5
HDR = struct.Struct("<4sBB6s")
CSI_HDR = struct.Struct("<6sbbBBIIH")
HELLO_BODY = struct.Struct("<6sBbI8s")
NEXMON_HDR = struct.Struct("<HbB6sHHHH")  # magic rssi fc mac seq css chanspec chip
NBINS = 64  # every CSI vector is reduced to this many amplitude bins


def mac_str(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def mac_bytes(s: str) -> bytes:
    return bytes(int(p, 16) for p in s.split(":"))


@dataclass
class CsiFrame:
    node: str
    src: str
    rssi: int
    channel: int
    amp: np.ndarray  # float32, shape (NBINS,)
    fmt: int = 0     # raw CSI length; frames of a different format are not comparable


@dataclass
class Hello:
    node: str
    bssid: str
    channel: int
    rssi: int
    uptime: int
    firmware: str


def _to_bins(amp: np.ndarray) -> np.ndarray:
    """Resample an amplitude vector to NBINS by block averaging."""
    n = amp.shape[0]
    if n == NBINS:
        return amp.astype(np.float32, copy=False)
    if n > NBINS and n % NBINS == 0:
        return amp.reshape(NBINS, -1).mean(axis=1).astype(np.float32)
    out = np.zeros(NBINS, dtype=np.float32)
    m = min(n, NBINS)
    out[:m] = amp[:m]
    return out


def esp_amplitude(raw: bytes, first_word_invalid: bool) -> np.ndarray:
    iq = np.frombuffer(raw, dtype=np.int8).astype(np.float32)
    if iq.size % 2:
        iq = iq[:-1]
    iq = iq.reshape(-1, 2)  # columns: imag, real
    if first_word_invalid:
        iq[:2] = 0.0
    amp = np.hypot(iq[:, 0], iq[:, 1])
    # L-LTF is the first 64 subcarriers; later blocks (HT-LTF) vary by frame
    # type, so only the L-LTF block is used to keep vectors comparable.
    return _to_bins(amp[:NBINS])


def nexmon_amplitude(payload: bytes):
    if len(payload) < NEXMON_HDR.size + 8:
        return None
    magic, rssi, _fc, mac, _seq, _css, chanspec, chip = NEXMON_HDR.unpack_from(payload)
    if magic != 0x1111:
        return None
    body = payload[NEXMON_HDR.size:]
    n = len(body) // 4
    if chip in (0x4358, 0x4366):
        # bcm4358 / bcm4366c0 use a packed float format that is not decoded here
        return None
    iq = np.frombuffer(body[: n * 4], dtype="<i2").astype(np.float32).reshape(-1, 2)
    amp = np.hypot(iq[:, 0], iq[:, 1])
    # null the guard bands and DC so they do not dominate
    if n in (64, 128, 256):
        k = n // 64
        amp[: 4 * k] = 0
        amp[-3 * k:] = 0
        amp[n // 2 - k: n // 2 + k] = 0
    return mac_str(mac), int(rssi), int(chanspec & 0xFF), _to_bins(amp), n


def parse(data: bytes):
    """Return CsiFrame, Hello, or None for anything unrecognised."""
    if len(data) < HDR.size or data[:4] != MAGIC:
        return None
    _, ptype, _ver, node = HDR.unpack_from(data)
    node_s = mac_str(node)
    body = data[HDR.size:]
    if ptype == T_CSI and len(body) >= CSI_HDR.size:
        src, rssi, _noise, ch, flags, _seq, _ts, ln = CSI_HDR.unpack_from(body)
        raw = body[CSI_HDR.size: CSI_HDR.size + ln]
        if len(raw) < 32:
            return None
        return CsiFrame(node_s, mac_str(src), rssi, ch, esp_amplitude(raw, bool(flags & 1)), len(raw))
    if ptype == T_HELLO and len(body) >= HELLO_BODY.size:
        bssid, ch, rssi, up, fw = HELLO_BODY.unpack_from(body)
        return Hello(node_s, mac_str(bssid), ch, rssi, up, fw.rstrip(b"\0").decode("ascii", "replace"))
    if ptype == T_NEXMON:
        r = nexmon_amplitude(body)
        if r is None:
            return None
        src, rssi, ch, amp, n = r
        return CsiFrame(node_s, src, rssi, ch, amp, n)
    return None


def build_peers(macs, name: str = "") -> bytes:
    """Peer list plus the name the hub has assigned this node, so a node can
    report itself by name instead of by MAC."""
    macs = list(macs)[:32]
    out = HDR.pack(MAGIC, T_PEERS, 1, b"\0" * 6) + bytes([len(macs)])
    for m in macs:
        out += mac_bytes(m)
    nb = name.encode("utf-8")[:31]
    return out + bytes([len(nb)]) + nb


def build_csi(node: str, src: str, rssi: int, channel: int, iq_int8: np.ndarray, seq: int) -> bytes:
    """Used by the simulator and tests. iq_int8: int8 array [imag, real, ...]."""
    raw = iq_int8.astype(np.int8).tobytes()
    return (HDR.pack(MAGIC, T_CSI, 1, mac_bytes(node))
            + CSI_HDR.pack(mac_bytes(src), rssi, -95, channel, 0, seq & 0xFFFFFFFF, 0, len(raw))
            + raw)


def build_hello(node: str, bssid: str, channel: int, rssi: int, uptime: int, fw: str = "sim") -> bytes:
    return (HDR.pack(MAGIC, T_HELLO, 1, mac_bytes(node))
            + HELLO_BODY.pack(mac_bytes(bssid), channel, rssi, uptime, fw.encode()[:8]))
