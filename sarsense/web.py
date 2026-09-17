"""A small asyncio HTTP/1.1 server. No third-party packages.

Serves the web app, a JSON API, a server-sent-events stream of live state, and
a caching proxy for map tiles so a map viewed once still works offline.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import mimetypes
import os
import re
import time
import urllib.parse
import urllib.request

log = logging.getLogger("sarsense.web")
STATIC = os.path.join(os.path.dirname(__file__), "static")
MAX_BODY = 256 * 1024
REASONS = {200: "OK", 204: "No Content", 304: "Not Modified", 400: "Bad Request", 401: "Unauthorized",
           403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large",
           500: "Internal Server Error", 502: "Bad Gateway"}
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/manifest+json", ".webmanifest")


class HttpError(Exception):
    def __init__(self, status, msg=""):
        super().__init__(msg)
        self.status, self.msg = status, msg


class Request:
    def __init__(self, method, path, query, headers, body, peer):
        self.method, self.path, self.query = method, path, query
        self.headers, self.body, self.peer = headers, body, peer

    def json(self):
        try:
            return json.loads(self.body or b"{}")
        except ValueError:
            raise HttpError(400, "Body is not valid JSON")


class WebServer:
    def __init__(self, hub):
        self.hub = hub
        self.cfg = hub.cfg
        self.tile_dir = os.path.join(self.cfg.data_dir, "tiles")
        self.routes = [
            ("GET", r"/api/state", self.get_state),
            ("GET", r"/api/events", None),  # handled as a stream
            ("GET", r"/api/log\.csv", self.get_log),
            ("GET", r"/api/links/(?P<node>[0-9a-f:]{17})/(?P<src>[0-9a-f:]{17})", self.get_link),
            ("POST", r"/api/auth", self.post_auth),
            ("POST", r"/api/devices/(?P<mac>[0-9a-f:]{17})", self.post_device),
            ("POST", r"/api/zones", self.post_zone),
            ("DELETE", r"/api/zones/(?P<zid>[\w-]+)", self.delete_zone),
            ("POST", r"/api/people", self.post_person),
            ("DELETE", r"/api/people/(?P<pid>[\w-]+)", self.delete_person),
            ("POST", r"/api/people/(?P<pid>[\w-]+)/checkin", self.post_checkin),
            ("POST", r"/api/tracks/(?P<tid>[\w-]+)", self.post_track),
            ("POST", r"/api/calibrate", self.post_calibrate),
            ("POST", r"/api/links/mute", self.post_mute),
            ("POST", r"/api/view", self.post_view),
            ("POST", r"/api/federate", self.post_federate),
            ("GET", r"/tiles/(?P<z>\d+)/(?P<x>\d+)/(?P<y>\d+)\.png", self.get_tile),
        ]
        self.routes = [(m, re.compile(p + "$"), h) for m, p, h in self.routes]

    # ------------------------------------------------------------ plumbing
    async def serve(self):
        ssl_ctx = None
        if self.cfg.tls_cert and self.cfg.tls_key:
            import ssl
            ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_ctx.load_cert_chain(self.cfg.tls_cert, self.cfg.tls_key)
        server = await asyncio.start_server(self.handle, self.cfg.http_host, self.cfg.http_port, ssl=ssl_ctx)
        scheme = "https" if ssl_ctx else "http"
        log.info("web app on %s://%s:%d", scheme, self.cfg.http_host, self.cfg.http_port)
        async with server:
            await server.serve_forever()

    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername")
        try:
            line = await asyncio.wait_for(reader.readline(), 15)
            if not line:
                return
            try:
                method, target, _ = line.decode("latin-1").split(" ", 2)
            except ValueError:
                raise HttpError(400, "Malformed request line")
            headers = {}
            while True:
                h = await asyncio.wait_for(reader.readline(), 15)
                if h in (b"\r\n", b"\n", b""):
                    break
                k, _, v = h.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()
            length = int(headers.get("content-length", "0") or 0)
            if length > MAX_BODY:
                raise HttpError(413, "Request body too large")
            body = await asyncio.wait_for(reader.readexactly(length), 15) if length else b""
            u = urllib.parse.urlsplit(target)
            req = Request(method.upper(), urllib.parse.unquote(u.path),
                          urllib.parse.parse_qs(u.query), headers, body, peer)
            if req.path == "/api/events" and req.method == "GET":
                await self.stream(req, writer)
                return
            status, ctype, payload, extra = await self.dispatch(req)
            await self.respond(writer, status, ctype, payload, extra)
        except HttpError as e:
            await self.respond(writer, e.status, "application/json",
                               json.dumps({"error": e.msg or REASONS.get(e.status, "")}).encode())
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception:
            log.exception("request failed")
            try:
                await self.respond(writer, 500, "application/json", b'{"error":"Internal error, see hub log"}')
            except Exception:
                pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def respond(self, writer, status, ctype, payload=b"", extra=None):
        head = [f"HTTP/1.1 {status} {REASONS.get(status, '')}",
                f"Content-Type: {ctype}", f"Content-Length: {len(payload)}",
                "Connection: close", "X-Content-Type-Options: nosniff",
                "Referrer-Policy: no-referrer"]
        for k, v in (extra or {}).items():
            head.append(f"{k}: {v}")
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + payload)
        await writer.drain()

    async def dispatch(self, req):
        for method, rx, handler in self.routes:
            m = rx.match(req.path)
            if m and handler:
                if method != req.method:
                    continue
                result = handler(req, **m.groupdict())
                if asyncio.iscoroutine(result):
                    result = await result
                if isinstance(result, tuple):
                    return result
                return 200, "application/json", json.dumps(result).encode(), None
        if req.method != "GET":
            raise HttpError(404, "No such endpoint")
        return self.static(req.path)

    def static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        full = os.path.realpath(os.path.join(STATIC, path.lstrip("/")))
        if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
            raise HttpError(404, "Not found")
        with open(full, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        cache = "public, max-age=86400" if "/vendor/" in full else "no-cache"
        return 200, ctype, data, {"Cache-Control": cache}

    async def stream(self, req, writer):
        q = asyncio.Queue(maxsize=2)
        self.hub.subscribers.add(q)
        try:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         b"Cache-Control: no-cache\r\nConnection: keep-alive\r\n"
                         b"X-Accel-Buffering: no\r\n\r\nretry: 2000\n\n")
            writer.write(b"data: " + self.hub.snapshot_json + b"\n\n")
            await writer.drain()
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), 15)
                    writer.write(b"data: " + data + b"\n\n")
                except asyncio.TimeoutError:
                    writer.write(b": keepalive\n\n")
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.hub.subscribers.discard(q)

    # ---------------------------------------------------------------- auth
    def require_pin(self, req):
        if not self.cfg.pin:
            return
        given = req.headers.get("x-pin", "")
        if not hmac.compare_digest(given.encode(), self.cfg.pin.encode()):
            raise HttpError(401, "Operator PIN required for this action")

    def post_auth(self, req):
        self.require_pin(req)
        return {"ok": True}

    # ------------------------------------------------------------ handlers
    def get_state(self, req):
        return 200, "application/json", self.hub.snapshot_json, {"Cache-Control": "no-store"}

    def get_log(self, req):
        path = self.hub.store.log_path
        data = open(path, "rb").read() if os.path.exists(path) else b"time_utc,kind,track,label,lat,lon,text\n"
        return 200, "text/csv", data, {"Content-Disposition": 'attachment; filename="sarsense-events.csv"'}

    def get_link(self, req, node, src):
        l = self.hub.links.links.get((node, src))
        if not l:
            raise HttpError(404, "Link not found")
        return dict(l.summary(time.time()), history=list(l.history),
                    baseline={"mu": l.base_mu, "sd": l.base_sd})

    def post_device(self, req, mac):
        self.require_pin(req)
        body = req.json()
        d = self.hub.store.devices.get(mac)
        if d is None:
            raise HttpError(404, "Unknown device")
        if "name" in body:
            d["name"] = str(body["name"])[:40]
        if "lat" in body:
            if body["lat"] is None:
                d["lat"] = d["lon"] = None
            else:
                d["lat"], d["lon"] = float(body["lat"]), float(body["lon"])
        if body.get("kind") in ("sensor", "ap", "transmitter"):
            d["kind"] = body["kind"]
        self.hub.store.mark()
        return d

    def post_zone(self, req):
        self.require_pin(req)
        try:
            return self.hub.store.put_zone(req.json())
        except (KeyError, TypeError, ValueError) as e:
            raise HttpError(400, str(e) or "Invalid zone")

    def delete_zone(self, req, zid):
        self.require_pin(req)
        if self.hub.store.zones.pop(zid, None) is None:
            raise HttpError(404, "Zone not found")
        self.hub.store.mark()
        return {"ok": True}

    def post_person(self, req):
        # anyone may register themselves as a responder; editing others needs the PIN
        body = req.json()
        if body.get("id") and body["id"] in self.hub.store.people:
            self.require_pin(req)
        return self.hub.store.put_person(body)

    def delete_person(self, req, pid):
        self.require_pin(req)
        self.hub.store.people.pop(pid, None)
        self.hub.store.mark()
        return {"ok": True}

    def post_checkin(self, req, pid):
        b = req.json()
        try:
            p = self.hub.store.checkin(pid, float(b["lat"]), float(b["lon"]), b.get("acc"))
        except (KeyError, TypeError, ValueError):
            raise HttpError(400, "lat and lon are required")
        if p is None:
            raise HttpError(404, "Responder not found, register again")
        return p

    def post_track(self, req, tid):
        self.require_pin(req)
        b = req.json()
        t = self.hub.tracker.act(tid, b.get("action", ""), b)
        if t is None:
            raise HttpError(404, "Track not found")
        return t.public()

    def post_calibrate(self, req):
        self.require_pin(req)
        b = req.json()
        secs = min(max(float(b.get("seconds", 30)), 5), 300)
        nodes = set(b.get("nodes") or [])
        now = time.time()
        n = 0
        for (node, _src), l in self.hub.links.links.items():
            if not nodes or node in nodes:
                l.calibrate(secs, now)
                n += 1
        return {"links": n, "seconds": secs}

    def post_mute(self, req):
        self.require_pin(req)
        b = req.json()
        l = self.hub.links.links.get((b.get("node"), b.get("src")))
        if not l:
            raise HttpError(404, "Link not found")
        l.muted = bool(b.get("muted", True))
        return l.summary(time.time())

    def post_view(self, req):
        self.require_pin(req)
        b = req.json()
        self.hub.store.view = {"lat": float(b["lat"]), "lon": float(b["lon"]), "zoom": int(b["zoom"])}
        self.hub.store.mark()
        return self.hub.store.view

    def post_federate(self, req):
        key = req.headers.get("x-sarsense-key", "")
        if not self.cfg.fed_key or not hmac.compare_digest(key.encode(), self.cfg.fed_key.encode()):
            raise HttpError(403, "Federation key missing or wrong")
        try:
            return self.hub.accept_federation(req.json())
        except ValueError as e:
            raise HttpError(400, str(e))

    async def get_tile(self, req, z, x, y):
        z, x, y = int(z), int(x), int(y)
        if z > 19 or x >= 2 ** z or y >= 2 ** z:
            raise HttpError(404, "Tile out of range")
        path = os.path.join(self.tile_dir, str(z), str(x), f"{y}.png")
        if os.path.exists(path):
            with open(path, "rb") as f:
                return 200, "image/png", f.read(), {"Cache-Control": "public, max-age=604800"}
        if self.cfg.tiles_offline or not self.cfg.tiles:
            raise HttpError(404, "Tile not cached and hub is offline")
        url = self.cfg.tiles.format(z=z, x=x, y=y)

        def fetch():
            r = urllib.request.Request(url, headers={
                "User-Agent": "SARSense/0.1 (search and rescue hub; self-hosted)"})
            with urllib.request.urlopen(r, timeout=8) as resp:
                return resp.read()

        try:
            data = await asyncio.get_running_loop().run_in_executor(None, fetch)
        except Exception as e:
            raise HttpError(502, f"Tile server unreachable: {e}")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return 200, "image/png", data, {"Cache-Control": "public, max-age=604800"}
