/* SARSense web app. Plain JS + Leaflet, served by the hub, works on phones. */
(() => {
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch {} },
};

let S = null;                    // latest hub snapshot
let pin = store.get("sar.pin") || "";
let sessionOk = false;
let me = JSON.parse(store.get("sar.me") || "null");
let tab = "unknowns";
let focus = null;                // {tab, key} of the row to highlight
let placing = null;              // {kind: "device"|"user", id}
let selectedLink = null;
let draw = null;                 // {type, points}
let fitted = false;

const ONLINE_S = 15;
const QUIET_S = 60;

/* ------------------------------------------------------------------ map */
const map = L.map("map", {zoomControl: false, attributionControl: true, maxZoom: 21});
L.control.zoom({position: "bottomleft"}).addTo(map);
L.tileLayer("/tiles/{z}/{x}/{y}.png", {
  maxZoom: 21, maxNativeZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);
map.setView([50.68, -3.47], 5);

const layers = {
  zones: L.layerGroup().addTo(map),
  links: L.layerGroup().addTo(map),
  stations: L.layerGroup().addTo(map),
  users: L.layerGroup().addTo(map),
  tracks: L.layerGroup().addTo(map),
  draw: L.layerGroup().addTo(map),
};
const markers = new Map();       // key -> marker, reused between frames

function divIcon(html, size) {
  return L.divIcon({className: "", html, iconSize: [size, size], iconAnchor: [size / 2, size / 2]});
}
function upsertMarker(group, key, latlng, html, size, onClick, seen) {
  seen.add(key);
  let m = markers.get(key);
  const icon = divIcon(html, size);
  if (!m) {
    m = L.marker(latlng, {icon, keyboard: true}).addTo(group);
    m._html = html;
    if (onClick) m.on("click", onClick);
    markers.set(key, m);
  } else {
    m.setLatLng(latlng);
    if (m._html !== html) { m.setIcon(icon); m._html = html; }
  }
  return m;
}
function sweepMarkers(prefix, seen) {
  for (const [k, m] of markers) {
    if (k.startsWith(prefix) && !seen.has(k)) { m.remove(); markers.delete(k); }
  }
}

/* ------------------------------------------------------------------ api */
async function api(method, path, body) {
  const r = await fetch(path, {
    method, headers: {"Content-Type": "application/json", "X-Pin": pin},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `Hub returned ${r.status}`);
  return data;
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 3500);
}
async function run(fn) { try { return await fn(); } catch (e) { toast(e.message); } }

function ask(title, label, value = "") {
  const d = $("#dlg");
  $("#dlg-title").textContent = title;
  $("#dlg-label").textContent = label;
  $("#dlg-input").value = value;
  d.showModal();
  $("#dlg-input").focus();
  return new Promise((res) => {
    d.addEventListener("close", () => res(d.returnValue === "ok" ? $("#dlg-input").value.trim() : null), {once: true});
  });
}

/* --------------------------------------------------------------- stream */
function connect() {
  const es = new EventSource("/api/events");
  es.onmessage = (e) => { S = JSON.parse(e.data); render(); };
  es.onopen = () => setStatus(true);
  es.onerror = () => setStatus(false);
}
function setStatus(ok) {
  const el = $("#hubstatus");
  el.className = "status " + (ok ? "ok" : "bad");
  el.textContent = ok ? (S ? `Hub ${S.hub} live` : "Live") : "Reconnecting to hub";
}

/* -------------------------------------------------------------- helpers */
const ago = (t) => {
  const s = Math.max(0, Math.round(S.now - t));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${(s / 3600).toFixed(1)} h ago`;
};
const clock = (t) => new Date(t * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", hourCycle: "h23"});
const closedStatus = (t) => t.status === "found" || t.status === "dismissed" || t.status === "merged";
const shownLabel = (t) => (t.remote ? t.hub + "/" : "") + t.label;
const kindName = {ap: "Router", sensor: "Sensor", transmitter: "Other transmitter"};
const devName = (d) => d.name || `${kindName[d.kind] || "Station"} ${d.mac.slice(-5)}`;
const allTracks = () => S.tracks.concat(S.remote_tracks || []);
const isOp = () => !S || !S.stats.pin_required || sessionOk;

function stationState(d) {
  const linkUp = S.links.some((l) => !l.stale && (l.node === d.mac || l.src === d.mac));
  const age = d.last_seen ? S.now - d.last_seen : Infinity;
  if (linkUp || (d.kind === "sensor" && age < ONLINE_S)) return "online";
  if (age < QUIET_S) return "quiet";
  return "offline";
}

function focusRow(tabName, key) {
  focus = {tab: tabName, key};
  showTab(tabName);
  render();
  requestAnimationFrame(() => {
    const el = document.querySelector(`[data-key="${CSS.escape(key)}"]`);
    if (el) el.scrollIntoView({block: "nearest", behavior: "smooth"});
  });
}
const focused = (tabName, key) => focus && focus.tab === tabName && focus.key === key;

function centre(lat, lon) {
  map.setView([lat, lon], Math.max(map.getZoom(), 18));
  collapsePanelOnPhone();
}

/* --------------------------------------------------------------- render */
function render() {
  if (!S) return;
  setStatus(true);
  document.body.classList.toggle("op", isOp());
  if (!fitted) fitView();

  const tracks = allTracks();
  const unknownActive = tracks.filter((t) => t.kind === "unknown" && t.status === "active").length;
  const unknownLost = tracks.filter((t) => t.kind === "unknown" && t.status === "lost").length;
  const usersActive = S.people.filter((p) => p.fresh).length;
  const stations = S.devices.filter((d) => d.kind !== "transmitter" || d.lat != null);
  const stationsUp = stations.filter((d) => stationState(d) === "online").length;

  $("#c-unknown").textContent = unknownActive;
  $("#c-lost").textContent = unknownLost;
  $("#c-users").textContent = usersActive;
  $("#c-stations").textContent = `${stationsUp}/${stations.length}`;
  $("#n-unknowns").textContent = unknownActive + unknownLost || "";
  $("#n-users").textContent = S.people.length || "";
  const down = stations.length - stationsUp;
  const ns = $("#n-stations");
  ns.textContent = down ? down : stations.length || "";
  ns.classList.toggle("warn", down > 0);
  ns.title = down ? `${down} station${down === 1 ? "" : "s"} not responding` : "";

  drawZones();
  drawStations();
  drawUsers();
  drawTracks();

  // only the visible tab builds its list, which keeps old phones responsive
  if (tab === "unknowns") renderUnknowns();
  if (tab === "users") renderUsers();
  if (tab === "stations") renderStations();
  if (tab === "zones") renderZones();
  if (tab === "hub") { renderStats(); renderEvents(); }
}

function fitView() {
  fitted = true;
  if (S.view && S.view.lat != null) { map.setView([S.view.lat, S.view.lon], S.view.zoom); return; }
  const pts = S.devices.filter((d) => d.lat != null).map((d) => [d.lat, d.lon]);
  if (pts.length) map.fitBounds(pts, {padding: [60, 60], maxZoom: 18});
  else fitted = false;
}

/* ------------------------------------------------------------ map layers */
let zoneSig = "";
function drawZones() {
  const sig = JSON.stringify(S.zones);
  if (sig === zoneSig) return;
  zoneSig = sig;
  layers.zones.clearLayers();
  for (const z of S.zones) {
    const tag = z.type === "tagging";
    L.polygon(z.polygon, {
      color: tag ? "#F25C05" : "#6A7887", weight: 2, dashArray: tag ? "6 5" : null,
      fillOpacity: tag ? 0.06 : 0.18, interactive: false,
    }).addTo(layers.zones);
  }
  if (tab === "zones") renderZones(true);
}

function drawStations() {
  const seen = new Set();
  const pos = {};
  for (const d of S.devices) if (d.lat != null) pos[d.mac] = [d.lat, d.lon];
  for (const d of S.devices.concat(S.remote_devices || [])) {
    if (d.lat == null) continue;
    const st = d.hub ? "online" : stationState(d);
    const cls = d.kind === "ap" ? "mk-ap" : "mk-sensor";
    const key = "s:" + (d.hub || "") + d.mac;
    upsertMarker(layers.stations, key, [d.lat, d.lon],
      `<div class="mk ${cls} st-${st} ${d.hub ? "mk-remote" : ""}" title="${esc(devName(d))}"></div>`,
      d.kind === "ap" ? 16 : 12, d.hub ? null : () => focusRow("stations", d.mac), seen);
  }
  sweepMarkers("s:", seen);

  layers.links.clearLayers();
  for (const l of S.links) {
    const a = pos[l.node], b = pos[l.src];
    if (!a || !b || l.stale) continue;
    const hot = l.active || l.breathing;
    L.polyline([a, b], {
      color: l.active ? "#F25C05" : l.breathing ? "#B3137A" : "#2459A8",
      weight: hot ? 4 : 1.5, opacity: l.muted ? 0.15 : hot ? 0.9 : 0.35,
      dashArray: l.muted ? "2 6" : null,
    }).on("click", () => selectLink(l)).addTo(layers.links);
  }
}

function drawUsers() {
  // a user's own shared position, shown faintly; the sensed position is a track
  const seen = new Set();
  for (const p of S.people) {
    if (p.lat == null || !p.fresh) continue;
    const mine = me && me.id === p.id;
    upsertMarker(layers.users, "u:" + p.id, [p.lat, p.lon],
      `<div class="mk-wrap" style="width:18px;height:18px"><div class="mk ${mine ? "mk-me" : "mk-user"}"></div><span class="mk-label user">${esc(p.name)}</span></div>`,
      18, () => focusRow("users", p.id), seen);
  }
  sweepMarkers("u:", seen);
}

function drawTracks() {
  const showMotion = $("#f-motion").checked;
  const seen = new Set();
  for (const t of allTracks()) {
    if (closedStatus(t) || (t.kind === "untagged" && !showMotion)) continue;
    let label = shownLabel(t);
    if (t.kind === "unknown" && t.name) label += ` ${t.name}`;
    const cls = ["mk", "mk-" + t.kind, t.status === "lost" ? "mk-lost" : "", t.breathing && !t.moving ? "mk-still" : "", t.remote ? "mk-remote" : ""].join(" ");
    const size = t.kind === "untagged" ? 12 : t.kind === "known" ? 22 : 26;
    // a sensed user already has a name label from their shared position if it is close; keep the track unlabelled then
    const labelHtml = t.kind === "unknown" ? `<span class="mk-label unknown">${esc(label)}</span>` : "";
    const html = `<div class="mk-wrap" style="width:${size}px;height:${size}px"><div class="${cls}"></div>${labelHtml}</div>`;
    const onClick = t.kind === "known" && t.person ? () => focusRow("users", t.person) : () => focusRow("unknowns", t.id);
    const m = upsertMarker(layers.tracks, "t:" + t.id, [t.lat, t.lon], html, size, onClick, seen);
    const ck = "c:" + t.id;
    seen.add(ck);
    let c = markers.get(ck);
    if (!c) { c = L.circle([t.lat, t.lon], {radius: t.radius, weight: 1, fillOpacity: 0.08, interactive: false}).addTo(layers.tracks); markers.set(ck, c); }
    c.setLatLng([t.lat, t.lon]); c.setRadius(Math.max(t.radius, 2));
    c.setStyle({color: t.kind === "unknown" ? "#F25C05" : t.kind === "known" ? "#0F7B5F" : "#6A7887"});
    m.setZIndexOffset(t.kind === "unknown" ? 1000 : 0);
  }
  sweepMarkers("t:", seen);
  sweepMarkers("c:", seen);
}

/* ------------------------------------------------------------ Unknowns tab */
function trackState(t) {
  if (t.status === "lost") return `lost contact ${ago(t.last_seen)}`;
  if (t.status === "found") return "found";
  if (t.status === "dismissed") return "dismissed";
  if (t.status === "merged") return "merged";
  const idle = Math.round(S.now - t.last_seen);
  if (idle > 5) return `not detected for ${idle} s`;
  return t.moving ? "moving" : "still";
}

function renderUnknowns() {
  const showMotion = $("#f-motion").checked;
  const showClosed = $("#f-closed").checked;
  const stat = {active: 0, lost: 1, found: 2, dismissed: 3, merged: 4};
  const rows = allTracks()
    .filter((t) => t.kind === "unknown" || (showMotion && t.kind === "untagged"))
    .filter((t) => showClosed || t.status === "active" || t.status === "lost")
    .sort((a, b) => stat[a.status] - stat[b.status] || (a.kind === "unknown" ? 0 : 1) - (b.kind === "unknown" ? 0 : 1) || b.last_seen - a.last_seen);
  const ol = $("#unknownlist");
  ol.innerHTML = "";
  $("#unknowns-empty").hidden = rows.length > 0;
  for (const t of rows) {
    const li = document.createElement("li");
    const closed = closedStatus(t);
    li.dataset.key = t.id;
    li.className = `card ${t.kind} ${t.status === "lost" ? "lost" : ""} ${closed ? "closed" : ""} ${focused("unknowns", t.id) ? "focus" : ""}`;
    const flags = [];
    if (t.breathing && !t.moving) flags.push(`<span class="flag still">Still, possible breathing${t.bpm ? ` about ${Math.round(t.bpm)}/min` : ""}</span>`);
    if (t.responder_nearby) flags.push(`<span class="flag with">${esc(t.responder_nearby)} is here</span>`);
    const title = t.kind === "untagged" ? `Motion ${esc(t.label.replace(/^Motion /, ""))}` : esc(shownLabel(t));
    const where = t.zone ? esc(t.zone) : "outside tagging zones";
    li.innerHTML = `<div class="top"><span class="id">${title}</span>${t.name ? `<span class="pname">${esc(t.name)}</span>` : ""}<span class="state">${trackState(t)}</span></div>
      <div class="meta">${where}, first seen ${clock(t.first_seen)}, accuracy about ${Math.round(t.radius)} m${t.note ? `<br>${esc(t.note)}` : ""}</div>
      ${flags.join("")}
      <div class="row">
        <button type="button" data-a="centre">Show on map</button>
        ${t.remote ? `<span class="hint">Managed by hub ${esc(t.hub)}</span>` : closed ? `<button type="button" data-op data-a="reopen">Reopen</button>` : `
        <button type="button" data-op data-a="found">Mark found</button>
        <button type="button" data-op data-a="identify">${t.name ? "Rename" : "Name"}</button>
        <button type="button" data-op data-a="note">Note</button>
        <button type="button" data-op data-a="dismiss" class="danger">False alarm</button>`}
      </div>`;
    li.querySelectorAll("button").forEach((b) => (b.onclick = () => trackAction(t, b.dataset.a)));
    ol.appendChild(li);
  }
}

async function trackAction(t, a) {
  if (a === "centre") { centre(t.lat, t.lon); return; }
  const body = {action: a};
  if (a === "identify") {
    const n = await ask(`Name ${t.label}`, "Name of the missing person (they stay on this list)", t.name || "");
    if (n == null) return;
    body.name = n;
  }
  if (a === "note" || a === "found") {
    const n = await ask(a === "found" ? `Mark ${t.label} found` : `Note for ${t.label}`, a === "found" ? "Note (optional)" : "Note", t.note || "");
    if (n == null) return;
    body.note = n;
  }
  if (a === "dismiss" && !confirm(`Dismiss ${t.label} as a false alarm?`)) return;
  run(async () => { await api("POST", `/api/tracks/${t.id}`, body); toast(`${t.label} updated`); });
}

/* --------------------------------------------------------------- Users tab */
function renderMe() {
  $("#me-new").hidden = !!me;
  $("#me-reg").hidden = !me;
  if (!me) return;
  $("#me-label").textContent = me.name + (me.team ? `, ${me.team}` : "");
  if (!S) return;
  const p = S.people.find((x) => x.id === me.id);
  $("#me-pos").textContent = !p ? "This hub does not know you yet. Sign out and register again."
    : p.lat == null ? "Position not shared yet."
    : `Position shared ${ago(p.updated)}${p.fresh ? "" : ". It has expired, share it again."}`;
}

function renderUsers() {
  renderMe();
  const sensed = {};
  for (const t of allTracks()) {
    if (t.kind === "known" && t.person && t.status === "active") sensed[t.person] = t;
  }
  const withUnknown = {};
  for (const t of allTracks()) {
    if (t.kind === "unknown" && t.status === "active" && t.responder_nearby) {
      if (!withUnknown[t.responder_nearby]) withUnknown[t.responder_nearby] = [];
      withUnknown[t.responder_nearby].push(shownLabel(t));
    }
  }
  const people = [...S.people].sort((a, b) => (b.fresh - a.fresh) || a.name.localeCompare(b.name));
  const ul = $("#userlist");
  ul.innerHTML = people.length ? "" : `<li class="hint">Nobody has registered yet.</li>`;
  for (const p of people) {
    const li = document.createElement("li");
    li.dataset.key = p.id;
    if (focused("users", p.id)) li.className = "focus";
    const t = sensed[p.id];
    const mine = me && me.id === p.id;
    const posTxt = p.lat == null ? "no position shared" : p.fresh ? `position ${ago(p.updated)}` : `position expired, ${ago(p.updated)}`;
    const bits = [posTxt];
    if (t) bits.push(`seen by stations ${ago(t.last_seen)}`);
    if (withUnknown[p.name]) bits.push(`with ${withUnknown[p.name].join(", ")}`);
    li.innerHTML = `<span class="dot ${p.fresh ? "on" : "off"}"></span>
      <span class="grow"><strong>${esc(p.name)}</strong>${mine ? " (you)" : ""}${p.team ? ` <span class="team">${esc(p.team)}</span>` : ""}
      <small>${esc(bits.join(", "))}</small></span>
      <span class="actions">
        ${p.lat != null ? `<button type="button" data-a="centre">Map</button>` : ""}
        <button type="button" data-op data-a="place">Set position</button>
        <button type="button" data-op data-a="remove" class="danger">Remove</button>
      </span>`;
    li.querySelectorAll("button").forEach((b) => (b.onclick = () => userAction(p, b.dataset.a)));
    ul.appendChild(li);
  }
}

function userAction(p, a) {
  if (a === "centre") { centre(p.lat, p.lon); return; }
  if (a === "place") {
    placing = {kind: "user", id: p.id};
    toast(`Tap the map where ${p.name} is`);
    collapsePanelOnPhone();
    return;
  }
  if (a === "remove") {
    if (!confirm(`Remove ${p.name}? Their phone will need to register again.`)) return;
    run(async () => { await api("DELETE", `/api/people/${p.id}`); toast(`${p.name} removed`); });
  }
}

$("#btn-register").onclick = () => run(async () => {
  const name = $("#me-name").value.trim();
  if (!name) { toast("Enter your name"); return; }
  const p = await api("POST", "/api/people", {name, team: $("#me-team").value.trim()});
  me = {id: p.id, name: p.name, team: p.team};
  store.set("sar.me", JSON.stringify(me));
  renderMe();
});
$("#btn-signout").onclick = () => { me = null; store.set("sar.me", null); stopWatch(); renderMe(); };
$("#btn-tap").onclick = () => {
  if (!me) return;
  placing = {kind: "user", id: me.id, self: true};
  toast("Tap your position on the map");
  collapsePanelOnPhone();
};

let watchId = null;
let lastSent = 0;
function stopWatch() { if (watchId != null) navigator.geolocation.clearWatch(watchId); watchId = null; }
$("#btn-gps").onclick = () => {
  if (!("geolocation" in navigator) || !window.isSecureContext) {
    toast("Location needs https. Use Tap map to set position instead.");
    return;
  }
  stopWatch();
  watchId = navigator.geolocation.watchPosition((pos) => {
    const now = Date.now();
    if (now - lastSent < 15000) return;
    lastSent = now;
    const c = pos.coords;
    api("POST", `/api/people/${me.id}/checkin`, {lat: c.latitude, lon: c.longitude, acc: c.accuracy})
      .catch((e) => toast(e.message));
  }, (err) => toast(`Location unavailable: ${err.message}`), {enableHighAccuracy: true, maximumAge: 10000});
  toast("Sharing location while this page is open");
};

/* ------------------------------------------------------------ Stations tab */
function renderStations() {
  const list = $("#stationlist");
  const byMac = Object.fromEntries(S.devices.map((d) => [d.mac, d]));
  const heard = {}, hearing = {}, busy = {};
  for (const l of S.links) {
    if (l.stale) continue;
    hearing[l.node] = (hearing[l.node] || 0) + 1;
    heard[l.src] = (heard[l.src] || 0) + 1;
    if (l.active || l.breathing) { busy[l.node] = true; busy[l.src] = true; }
  }

  // group sensors under the router they are joined to
  const routers = S.devices.filter((d) => d.kind === "ap");
  const groups = routers.map((r) => ({head: r, items: S.devices.filter((d) => d.kind === "sensor" && d.bssid === r.mac)}));
  const grouped = new Set(groups.flatMap((g) => [g.head.mac, ...g.items.map((d) => d.mac)]));
  const loose = S.devices.filter((d) => d.kind === "sensor" && !grouped.has(d.mac));
  if (loose.length) groups.push({head: null, title: "Sensors on an unknown router", items: loose});
  const others = S.devices.filter((d) => d.kind === "transmitter");
  if (others.length) groups.push({head: null, title: "Other transmitters", items: others, note: "Heard by sensors but not a SARSense station. Place one if it is a fixed device you know, such as a second router."});

  const all = S.devices.filter((d) => d.kind !== "transmitter");
  const up = all.filter((d) => stationState(d) === "online").length;
  const unplaced = all.filter((d) => d.lat == null).length;
  $("#station-summary").innerHTML = all.length
    ? `${all.length} station${all.length === 1 ? "" : "s"}, <b>${up} online</b>${all.length - up ? `, <b class="bad">${all.length - up} not responding</b>` : ""}${unplaced ? `, <b class="bad">${unplaced} not placed</b>` : ""}`
    : "No stations have reported in. Power a sensor node and check its Wi-Fi settings.";

  const row = (d) => {
    const st = stationState(d);
    const bits = [];
    bits.push(st === "online" ? "online" : d.last_seen ? `last heard ${ago(d.last_seen)}` : "not heard");
    if (d.lat == null) bits.push("not placed");
    if (d.kind === "sensor") {
      bits.push(`hears ${hearing[d.mac] || 0}, heard by ${heard[d.mac] || 0}`);
      if (d.ap_rssi != null) bits.push(`${d.ap_rssi} dBm to router`);
      if (d.firmware) bits.push(`firmware ${d.firmware}`);
      if (d.addr) bits.push(d.addr);
    } else {
      bits.push(`heard by ${heard[d.mac] || 0} links`);
      if (d.channel) bits.push(`channel ${d.channel}`);
    }
    const sel = placing && placing.kind === "device" && placing.id === d.mac;
    return `<li data-key="${d.mac}" class="${focused("stations", d.mac) ? "focus" : ""}">
      <span class="dot st-${st} ${busy[d.mac] ? "busy" : ""}" title="${st}"></span>
      <span class="grow"><strong>${esc(devName(d))}</strong>${devName(d).startsWith(kindName[d.kind] || "~") ? "" : ` <span class="kind">${kindName[d.kind] || d.kind}</span>`}
      <small>${esc(bits.join(", "))}</small><small class="mac">${d.mac}</small></span>
      <span class="actions">
        ${d.lat != null ? `<button type="button" data-a="centre">Map</button>` : ""}
        <button type="button" data-op data-a="place">${sel ? "Tap map" : d.lat == null ? "Place" : "Move"}</button>
        <button type="button" data-op data-a="rename">Rename</button>
      </span></li>`;
  };

  list.innerHTML = groups.map((g) => {
    const head = g.head ? `<h4>${esc(devName(g.head))} <span class="kind">router, channel ${g.head.channel || "?"}</span></h4>` : `<h4>${esc(g.title)}</h4>`;
    const items = (g.head ? [g.head] : []).concat(g.items);
    return `<section class="group">${head}${g.note ? `<p class="hint">${esc(g.note)}</p>` : ""}<ul class="plain rows">${items.map(row).join("")}</ul></section>`;
  }).join("");

  const remote = S.remote_devices || [];
  if (remote.length) {
    const hubs = [...new Set(remote.map((d) => d.hub))];
    list.insertAdjacentHTML("beforeend", `<section class="group"><h4>Other hubs</h4><p class="hint">${hubs.map((h) => `Hub ${esc(h)}: ${remote.filter((d) => d.hub === h).length} placed stations`).join("<br>")}</p></section>`);
  }

  list.querySelectorAll("li[data-key] button").forEach((b) => {
    const d = byMac[b.closest("li").dataset.key];
    b.onclick = () => stationAction(d, b.dataset.a);
  });

  renderLinks(byMac);
}

function stationAction(d, a) {
  if (a === "centre") { centre(d.lat, d.lon); return; }
  if (a === "place") {
    const same = placing && placing.kind === "device" && placing.id === d.mac;
    placing = same ? null : {kind: "device", id: d.mac};
    if (placing) { toast(`Tap the map where ${devName(d)} is mounted`); collapsePanelOnPhone(); }
    render();
    return;
  }
  if (a === "rename") {
    run(async () => {
      const n = await ask("Rename station", "Name", d.name);
      if (n != null) await api("POST", `/api/devices/${d.mac}`, {name: n});
    });
  }
}

function renderLinks(byMac) {
  const ll = $("#linklist");
  ll.innerHTML = "";
  const nm = (m) => (byMac[m] ? devName(byMac[m]) : m.slice(-5));
  for (const l of [...S.links].sort((x, y) => y.z - x.z).slice(0, 60)) {
    const li = document.createElement("li");
    const col = l.stale ? "var(--rule)" : l.active ? "var(--unknown)" : l.breathing ? "var(--still)" : "var(--sensor)";
    const state = l.stale ? "no data" : l.calibrating ? "calibrating" : l.muted ? "muted"
      : l.active ? "movement" : l.breathing ? `possible breathing ${l.bpm} per min` : "quiet";
    li.innerHTML = `<span class="swatch" style="background:${col}"></span>
      <span class="grow">${esc(nm(l.node))} hears ${esc(nm(l.src))}
      <small>${state}, score ${l.z}, ${l.rate} packets/s, ${l.rssi} dBm</small></span>`;
    if (selectedLink && selectedLink.node === l.node && selectedLink.src === l.src) li.className = "focus";
    li.onclick = () => selectLink(l);
    ll.appendChild(li);
  }
  if (!S.links.length) ll.innerHTML = `<li class="hint">No CSI arriving yet.</li>`;
}

function selectLink(l) {
  selectedLink = {node: l.node, src: l.src};
  showTab("stations");
  $("#linkchart").hidden = false;
  refreshLinkChart();
  requestAnimationFrame(() => $("#linkchart").scrollIntoView({block: "nearest"}));
}
let chartBusy = false;
async function refreshLinkChart() {
  if (chartBusy || !selectedLink) return;
  chartBusy = true;
  try {
    const d = await api("GET", `/api/links/${selectedLink.node}/${selectedLink.src}`);
    const cv = $("#linkchart canvas");
    const ctx = cv.getContext("2d");
    const w = cv.width, h = cv.height;
    ctx.clearRect(0, 0, w, h);
    const zs = d.history.map((p) => p[1]);
    const max = Math.max(8, ...zs);
    const y = (v) => h - 4 - (Math.max(v, 0) / max) * (h - 8);
    ctx.strokeStyle = "#F25C05"; ctx.setLineDash([4, 4]); ctx.beginPath();
    ctx.moveTo(0, y(4)); ctx.lineTo(w, y(4)); ctx.stroke(); ctx.setLineDash([]);
    ctx.strokeStyle = getComputedStyle(document.body).color; ctx.lineWidth = 2; ctx.beginPath();
    zs.forEach((v, i) => { const x = (i / Math.max(zs.length - 1, 1)) * w; i ? ctx.lineTo(x, y(v)) : ctx.moveTo(x, y(v)); });
    ctx.stroke();
    $("#linkinfo").textContent = `Movement score over the last minute. The dashed line is the detection threshold. ${d.calibrated ? "Calibrated." : "Not calibrated yet."}`;
    $("#btn-mute").textContent = d.muted ? "Unmute this link" : "Mute this link";
    $("#btn-mute").onclick = () => run(() => api("POST", "/api/links/mute", {node: d.node, src: d.src, muted: !d.muted}));
  } catch (e) { $("#linkinfo").textContent = e.message; }
  finally { chartBusy = false; }
}
setInterval(() => { if (tab === "stations" && selectedLink) refreshLinkChart(); }, 2000);

$("#btn-calibrate").onclick = () => run(async () => {
  if (!confirm("Everyone should keep still or stay away from the stations for 30 seconds. Start?")) return;
  const r = await api("POST", "/api/calibrate", {seconds: 30});
  toast(`Calibrating ${r.links} links for 30 seconds`);
});

/* --------------------------------------------------------------- Zones tab */
let zoneListSig = "";
function renderZones(force) {
  const sig = JSON.stringify(S.zones) + isOp();
  if (!force && sig === zoneListSig) return;
  zoneListSig = sig;
  const list = $("#zonelist");
  list.innerHTML = S.zones.length ? "" : `<li class="hint">No zones yet. Draw a tagging zone around the search area.</li>`;
  for (const z of S.zones) {
    const tag = z.type === "tagging";
    const inside = allTracks().filter((t) => t.zone === z.name && t.kind === "unknown" && t.status === "active").length;
    const li = document.createElement("li");
    li.innerHTML = `<span class="swatch" style="background:${tag ? "var(--unknown)" : "var(--motion)"}"></span>
      <span class="grow"><strong>${esc(z.name)}</strong><small>${tag ? `Tagging zone${inside ? `, ${inside} unknown inside` : ""}` : "Ignore zone"}${z.origin === "upstream" ? ", from command hub" : ""}</small></span>
      <span class="actions"><button type="button" data-a="centre">Map</button>
      ${z.origin === "upstream" ? "" : `<button type="button" class="danger" data-op data-a="delete">Delete</button>`}</span>`;
    li.querySelectorAll("button").forEach((b) => (b.onclick = () => {
      if (b.dataset.a === "centre") { map.fitBounds(z.polygon, {padding: [40, 40]}); collapsePanelOnPhone(); return; }
      run(async () => {
        if (!confirm(`Delete zone "${z.name}"?`)) return;
        await api("DELETE", `/api/zones/${z.id}`);
        toast("Zone deleted");
      });
    }));
    list.appendChild(li);
  }
}

function startDraw(type) {
  draw = {type, points: []};
  placing = null;
  $("#drawbar").hidden = false;
  $("#drawhint").textContent = type === "tagging" ? "Tap the map to outline the tagging zone" : "Tap the map to outline the ignore zone";
  collapsePanelOnPhone();
}
function redrawDraft() {
  layers.draw.clearLayers();
  if (!draw || !draw.points.length) return;
  L.polyline(draw.points.concat(draw.points.length > 2 ? [draw.points[0]] : []),
    {color: "#F25C05", weight: 3, dashArray: "4 4"}).addTo(layers.draw);
  for (const p of draw.points) L.circleMarker(p, {radius: 5, color: "#F25C05", fillOpacity: 1}).addTo(layers.draw);
  $("#drawhint").textContent = `${draw.points.length} corner${draw.points.length === 1 ? "" : "s"}${draw.points.length < 3 ? ", at least 3 needed" : ""}`;
}
function endDraw() { draw = null; layers.draw.clearLayers(); $("#drawbar").hidden = true; }
$("#draw-undo").onclick = () => { if (draw) { draw.points.pop(); redrawDraft(); } };
$("#draw-cancel").onclick = endDraw;
$("#draw-finish").onclick = () => run(async () => {
  if (!draw || draw.points.length < 3) { toast("Add at least 3 corners"); return; }
  const n = await ask("Name this zone", "Zone name", draw.type === "tagging" ? "Search area" : "Ignore area");
  if (n == null) return;
  await api("POST", "/api/zones", {name: n, type: draw.type, polygon: draw.points});
  toast("Zone saved");
  endDraw();
});
$("#btn-zone-tag").onclick = () => startDraw("tagging");
$("#btn-zone-ignore").onclick = () => startDraw("ignore");

/* ----------------------------------------------------------------- Hub tab */
let evSig = "";
function renderEvents() {
  const sig = S.events.length ? S.events[S.events.length - 1].t + ":" + S.events.length : "";
  if (sig === evSig) return;
  evSig = sig;
  $("#events").innerHTML = [...S.events].reverse().slice(0, 40)
    .map((e) => `<li class="ev-${e.kind}"><time>${clock(e.t)}</time>${esc(e.text)}</li>`).join("") || `<li class="hint">Nothing yet.</li>`;
}

function renderStats() {
  const st = S.stats;
  const up = st.upstream === null ? "not used" : st.upstream ? "connected" : "unreachable";
  const rows = [
    ["Hub ID", S.hub], ["Uptime", `${Math.floor(st.uptime / 3600)} h ${Math.floor(st.uptime / 60) % 60} min`],
    ["Packets", st.packets.toLocaleString()], ["Unreadable packets", st.bad],
    ["Links", st.links + (st.dropped_links ? ` (${st.dropped_links} refused, raise max_links)` : "")],
    ["Command hub", up], ["Hubs reporting here", st.hubs.join(", ") || "none"],
    ["Operator PIN", st.pin_required ? "required" : "not set, anyone can edit"],
  ];
  $("#stats").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
}

async function tryPin(p) {
  pin = p;
  try { await api("POST", "/api/auth", {}); sessionOk = true; store.set("sar.pin", p); }
  catch { sessionOk = false; pin = ""; store.set("sar.pin", null); }
  $("#op-lock").hidden = sessionOk;
  $("#op-unlocked").hidden = !sessionOk;
  zoneListSig = "";
  render();
  return sessionOk;
}
$("#btn-unlock").onclick = async () => {
  if (await tryPin($("#pin").value)) toast("Operator mode on"); else toast("That PIN is not right");
  $("#pin").value = "";
};
$("#btn-lock").onclick = () => {
  pin = ""; sessionOk = false; store.set("sar.pin", null);
  $("#op-lock").hidden = false; $("#op-unlocked").hidden = true;
  zoneListSig = "";
  render();
};
$("#btn-saveview").onclick = () => run(async () => {
  const c = map.getCenter();
  await api("POST", "/api/view", {lat: c.lat, lon: c.lng, zoom: map.getZoom()});
  toast("Default view saved");
});

/* ------------------------------------------------------------ map clicks */
map.on("click", (e) => {
  const {lat, lng} = e.latlng;
  if (draw) { draw.points.push([lat, lng]); redrawDraft(); return; }
  if (!placing) { focus = null; return; }
  const p = placing;
  placing = null;
  if (p.kind === "device") {
    run(async () => { await api("POST", `/api/devices/${p.id}`, {lat, lon: lng}); toast("Station placed"); });
  } else if (p.kind === "user") {
    run(async () => { await api("POST", `/api/people/${p.id}/checkin`, {lat, lon: lng}); toast("Position set"); });
  }
});

/* ---------------------------------------------------------- panel and tabs */
function showTab(name) {
  tab = name;
  document.querySelectorAll("nav [role=tab]").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((s) => (s.hidden = s.id !== "tab-" + name));
  if (name !== "stations") { selectedLink = null; $("#linkchart").hidden = true; }
  if (name === "zones") zoneListSig = "";
  if (name === "hub") evSig = "";
  $("#panel").dataset.open = "true";
  store.set("sar.tab", name);
}
document.querySelectorAll("nav [role=tab]").forEach((b) => (b.onclick = () => { showTab(b.dataset.tab); render(); }));
document.querySelectorAll("#tally [data-goto]").forEach((b) => (b.onclick = () => { showTab(b.dataset.goto); render(); }));
$("#grip").onclick = () => { const p = $("#panel"); p.dataset.open = p.dataset.open === "true" ? "false" : "true"; };
function collapsePanelOnPhone() { if (matchMedia("(max-width: 760px)").matches) $("#panel").dataset.open = "false"; }
$("#f-motion").onchange = () => render();
$("#f-closed").onchange = () => render();

renderMe();
const TABS = ["unknowns", "users", "stations", "zones", "hub"];
const savedTab = store.get("sar.tab");
showTab(TABS.includes(savedTab) ? savedTab : "unknowns");
if (pin) tryPin(pin); else $("#op-lock").hidden = false;
connect();
})();
