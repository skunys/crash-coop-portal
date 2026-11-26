"use strict";
/**
 * Crash co-op relay -- the fixed meeting point for Player 1 (host) and Player 2.
 *
 * The portal link is a cloudflared quick tunnel: it changes on every restart and
 * nothing can accept an inbound connection on a home PC behind NAT. This relay
 * holds one stable public address and a room. BOTH sides dial OUT to it:
 *
 *   you (Player 1 / host)  p2_host.py   -> wss://<relay>/relay?room=crash&role=host
 *   her (Player 2)         p2_client.py -> wss://<relay>/relay?room=crash&role=client
 *
 * It forwards the same tiny JSON messages the LAN path already uses
 * (`p2_ready`, `ready`, `pad_input`, `disconnect`) byte for byte, so the game
 * side does not care which route won. It carries *input only* -- never video,
 * never audio, never the game itself. The stream stays on the portal's tunnel.
 *
 * Nothing here reduces anyone's ping: a relay is one extra hop. What it buys is
 * a permanent address (no new link to send her after every restart), reachability
 * without port forwarding, and a seat that survives either side reconnecting.
 * `GET /ping` exists so you can measure each Cloudways datacenter from your PC
 * and deploy to the one closest to a fair midpoint between Serbia and the US.
 *
 * Deploy (Cloudways, "Other" -> Node.js 22 or 24):
 *   Root directory : portal/relay
 *   Build/Start    : npm start        (npm install runs automatically)
 *   Port           : the PORT env var Cloudways sets
 * Optional env:
 *   RELAY_KEY         shared room key; required or the relay accepts anyone
 *   RELAY_ROOM        default room name            (crash)
 *   RELAY_LOCATION    label shown in /ping         (e.g. "Frankfurt")
 *   STATIC_DIR        static site to serve at /    (default ../site/build)
 *
 * Endpoints:
 *   GET  /            landing page from STATIC_DIR, else the live status page
 *   GET  /status      JSON: rooms, who is seated, measured round trip times
 *   GET  /ping        JSON with the server clock -- measure this from your PC
 *   GET  /healthz     "ok"
 *   WSS  /relay?room=&role=host|client
 */

const crypto = require("node:crypto");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const { WebSocketServer } = require("ws");

const PORT = Number(process.env.PORT || 8080);
const BIND = process.env.BIND || "0.0.0.0";
const DEFAULT_ROOM = process.env.RELAY_ROOM || "crash";
// Shared room key. Set RELAY_KEY on the relay; both players pass it in the URL.
// Without a key anyone who finds the public URL could take a seat in the room,
// kick the real host out and feed the game fake input.
const RELAY_KEY = (process.env.RELAY_KEY || "").trim();
const LOCATION = process.env.RELAY_LOCATION || "";
const STATIC_DIR = process.env.STATIC_DIR || path.join(__dirname, "..", "site", "build");
const MAX_QUEUE = 300;
const TICK_MS = 5000;
const OPEN = 1;

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".webmanifest": "application/manifest+json",
};

/** room name -> { name, host, client, queue[], rtt{host,client}, since } */
const rooms = new Map();
const now = () => Date.now();

function getRoom(name) {
  let room = rooms.get(name);
  if (!room) {
    room = { name, host: null, client: null, queue: [], rtt: { host: null, client: null }, since: now() };
    rooms.set(name, room);
  }
  return room;
}

const other = (role) => (role === "host" ? "client" : "host");
const peerOf = (room, role) => (role === "host" ? room.client : room.host);
const roleOf = (room, ws) => (room.host === ws ? "host" : room.client === ws ? "client" : null);

function keyOk(given) {
  if (!RELAY_KEY) return true;
  const a = Buffer.from(String(given || ""), "utf8");
  const b = Buffer.from(RELAY_KEY, "utf8");
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function sendJson(ws, obj) {
  if (!ws || ws.readyState !== OPEN) return;
  try {
    ws.send(JSON.stringify(obj));
  } catch {
    /* socket died mid-send; the close handler cleans up */
  }
}

function forward(target, data, isBinary) {
  if (!target || target.readyState !== OPEN) return;
  try {
    target.send(data, { binary: isBinary });
  } catch {
    /* ignore */
  }
}

// ---------------------------------------------------------------------------
// WebSocket relay
// ---------------------------------------------------------------------------

const wss = new WebSocketServer({ noServer: true, clientTracking: true });

wss.on("connection", (ws) => {
  const role = ws.role === "host" ? "host" : "client";
  const name = ws.roomName || DEFAULT_ROOM;
  const room = getRoom(name);

  // A reconnect from the same side takes its own seat back instead of leaving a
  // zombie in the room: a laptop that sleeps would otherwise black-hole input.
  if (room[role] && room[role] !== ws) {
    const stale = room[role];
    room[role] = null;
    sendJson(stale, { t: "replaced", msg: `another ${role} took this room` });
    try {
      stale.close(4001, "replaced");
    } catch {
      /* ignore */
    }
  }
  room[role] = ws;
  ws.isAlive = true;

  sendJson(ws, { t: "relay_ready", role, room: name, location: LOCATION, ts: now() });

  const peer = peerOf(room, role);
  if (peer) {
    sendJson(ws, { t: "peer_present", role: other(role) });
    sendJson(peer, { t: "peer_joined", role });
    // Frames she sent before you arrived are still queued: replay them now.
    if (role === "host" && room.queue.length) {
      for (const item of room.queue.splice(0, room.queue.length)) forward(ws, item.data, item.binary);
    }
  } else {
    sendJson(ws, { t: "peer_absent", role: other(role), waiting: true });
  }

  ws.on("message", (data, isBinary) => {
    if (!isBinary) {
      let msg = null;
      try {
        msg = JSON.parse(data.toString());
      } catch {
        msg = null;
      }
      if (msg && msg.t === "ping") {
        sendJson(ws, { t: "relay_pong", ts: msg.ts, server_ts: now() });
        return;
      }
      if (msg && msg.t === "relay_stats") {
        sendJson(ws, {
          t: "relay_stats",
          room: name,
          rtt_ms: room.rtt[role],
          peer: Boolean(peerOf(room, role)),
        });
        return;
      }
    }
    const target = peerOf(room, role);
    if (target) {
      forward(target, data, isBinary);
      return;
    }
    // Hold her side's frames until the host shows up (a queued `p2_ready` is
    // what makes the handshake still work when you start the game second).
    if (role === "client") {
      room.queue.push({ data, binary: isBinary });
      if (room.queue.length > MAX_QUEUE) room.queue.shift();
    }
  });

  ws.on("pong", () => {
    ws.isAlive = true;
    if (ws.sentAt) {
      const rtt = now() - ws.sentAt;
      const r = rooms.get(ws.roomName);
      if (r) {
        const who = roleOf(r, ws);
        if (who) r.rtt[who] = rtt;
      }
    }
  });

  ws.on("close", () => {
    if (room[role] === ws) room[role] = null;
    const left = peerOf(room, role);
    if (left) sendJson(left, { t: "peer_left", role });
  });

  ws.on("error", () => {
    /* close follows */
  });
});

// Keeps both seats honest and reports the round trip each side actually sees.
setInterval(() => {
  for (const ws of wss.clients) {
    if (!ws.isAlive) {
      try {
        ws.terminate();
      } catch {
        /* ignore */
      }
      continue;
    }
    ws.isAlive = false;
    ws.sentAt = now();
    try {
      ws.ping();
    } catch {
      /* ignore */
    }
    const room = rooms.get(ws.roomName);
    const role = room ? roleOf(room, ws) : null;
    if (room && role) {
      sendJson(ws, {
        t: "link_stats",
        room: room.name,
        rtt_ms: room.rtt[role],
        peer: Boolean(peerOf(room, role)),
      });
    }
  }
}, TICK_MS).unref();

// ---------------------------------------------------------------------------
// HTTP: status page, static landing page, /ping
// ---------------------------------------------------------------------------

function writeJson(res, obj, code = 200) {
  const body = JSON.stringify(obj);
  res.writeHead(code, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "content-length": Buffer.byteLength(body),
  });
  res.end(body);
}

function snapshot() {
  const out = [];
  for (const room of [...rooms.values()]) {
    if (!room.host && !room.client) {
      rooms.delete(room.name);
      continue;
    }
    out.push({
      room: room.name,
      host: Boolean(room.host),
      client: Boolean(room.client),
      paired: Boolean(room.host && room.client),
      rtt_ms: { ...room.rtt },
      queued: room.queue.length,
      up_s: Math.round((now() - room.since) / 1000),
    });
  }
  return out;
}

function statusPage() {
  const rows = snapshot()
    .map(
      (r) =>
        `<tr><td>${r.room}</td><td>${r.host ? "in" : "-"}</td><td>${r.client ? "in" : "-"}</td>` +
        `<td>${r.rtt_ms.host ?? "-"}</td><td>${r.rtt_ms.client ?? "-"}</td><td>${r.queued}</td></tr>`
    )
    .join("");
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="3">
<title>Crash co-op relay</title>
<style>
  :root{color-scheme:dark}
  body{margin:0;padding:32px;background:#12100e;color:#efe7d6;font:15px/1.5 ui-sans-serif,system-ui,sans-serif}
  h1{font-size:20px;margin:0 0 4px}
  p.sub{margin:0 0 24px;color:#a89e8c}
  table{border-collapse:collapse;width:100%;max-width:720px}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #2b2621}
  th{color:#a89e8c;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.06em}
  code{background:#1d1a16;padding:2px 6px;border-radius:4px}
</style></head><body>
<h1>Crash co-op relay</h1>
<p class="sub">location: <code>${LOCATION || "unset"}</code> &middot; rooms: ${snapshot().length} &middot; up: ${Math.round(process.uptime())}s</p>
<table><thead><tr><th>room</th><th>host</th><th>player 2</th><th>host ms</th><th>p2 ms</th><th>queued</th></tr></thead>
<tbody>${rows || `<tr><td colspan="6">no one connected yet</td></tr>`}</tbody></table>
</body></html>`;
}

function serveStatic(pathname, res) {
  const root = path.resolve(STATIC_DIR);
  let rel;
  try {
    rel = decodeURIComponent(pathname === "/" ? "/index.html" : pathname);
  } catch {
    return writeJson(res, { ok: false, error: "bad path" }, 400);
  }
  const file = path.resolve(path.join(root, rel));
  if (!file.startsWith(root + path.sep)) return writeJson(res, { ok: false, error: "not found" }, 404);

  fs.readFile(file, (err, buf) => {
    if (err) {
      if (pathname === "/") return html(res, statusPage());
      return writeJson(res, { ok: false, error: "not found" }, 404);
    }
    res.writeHead(200, {
      "content-type": MIME[path.extname(file).toLowerCase()] || "application/octet-stream",
      "cache-control": "no-cache",
    });
    res.end(buf);
  });
}

function html(res, body) {
  res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
  res.end(body);
}

const server = http.createServer((req, res) => {
  let url;
  try {
    url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  } catch {
    return writeJson(res, { ok: false, error: "bad request" }, 400);
  }
  const p = url.pathname;

  if (p === "/ping") {
    return writeJson(res, { ok: true, relay: "crash-coop", location: LOCATION || "unset", keyed: Boolean(RELAY_KEY), ts: now() });
  }
  if (p === "/status") {
    return writeJson(res, { ok: true, location: LOCATION || "unset", keyed: Boolean(RELAY_KEY), ts: now(), rooms: snapshot() });
  }
  if (p === "/healthz") {
    return writeJson(res, { ok: true });
  }
  if (p === "/relay") {
    return writeJson(res, { ok: false, error: "websocket upgrade required" }, 426);
  }
  return serveStatic(p, res);
});

server.on("upgrade", (req, socket, head) => {
  let url;
  try {
    url = new URL(req.url, "http://relay");
  } catch {
    socket.destroy();
    return;
  }
  if (url.pathname !== "/relay") {
    socket.write("HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n");
    socket.destroy();
    return;
  }
  if (!keyOk(url.searchParams.get("key"))) {
    socket.write("HTTP/1.1 401 Unauthorized\r\nConnection: close\r\nContent-Length: 0\r\n\r\n");
    socket.destroy();
    return;
  }
  const role = (url.searchParams.get("role") || "").toLowerCase() === "host" ? "host" : "client";
  const room = (url.searchParams.get("room") || "").trim().slice(0, 48) || DEFAULT_ROOM;
  wss.handleUpgrade(req, socket, head, (ws) => {
    ws.role = role;
    ws.roomName = room;
    wss.emit("connection", ws, req);
  });
});

server.listen(PORT, BIND, () => {
  console.log(
    `[relay] crash-coop relay listening on ${BIND}:${PORT}` +
      (LOCATION ? ` (${LOCATION})` : "") +
      ` -- room "${DEFAULT_ROOM}", ` +
      (RELAY_KEY ? "room key required" : "NO ROOM KEY (open to anyone with the URL)") +
      `, static "${STATIC_DIR}"`
  );
});

function shutdown() {
  console.log("[relay] shutting down");
  for (const ws of wss.clients) {
    try {
      ws.close(1001, "server shutting down");
    } catch {
      /* ignore */
    }
  }
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 2000).unref();
}
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
