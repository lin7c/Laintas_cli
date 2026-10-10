// Development stand-in for the native window: serves ui/ in a browser and
// plays the part of the Rust host, so the page, the frame protocol and the
// terminal host can be exercised together on any POSIX machine.
//
//   node scripts/dev_bridge.mjs [port] -- program [args...]
import { spawn } from "node:child_process";
import { createReadStream, existsSync } from "node:fs";
import { createServer } from "node:http";
import { dirname, extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const ui = join(here, "..", "ui");
const host = join(here, "..", "..", "term_host.py");
const split = process.argv.indexOf("--");
const port = Number(process.argv[2]) || 1430;
const program = split > 0 ? process.argv.slice(split + 1) : ["bash"];
const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };

const frame = (kind, payload = Buffer.alloc(0)) => {
  const head = Buffer.alloc(5);
  head.write(kind, 0, "latin1");
  head.writeUInt32BE(payload.length, 1);
  return Buffer.concat([head, payload]);
};

const SHIM = `
window.__TAURI__ = (() => {
  const handlers = {};
  const events = new EventSource("/events");
  events.onmessage = (m) => { const e = JSON.parse(m.data); (handlers[e.event] || []).forEach((h) => h({ payload: e.payload })); };
  return {
    core: { invoke: (cmd, args) => fetch("/invoke/" + cmd, { method: "POST", body: JSON.stringify(args || {}) }).then((r) => r.ok ? r.json() : r.text().then((t) => Promise.reject(t))) },
    event: { listen: (name, handler) => { (handlers[name] = handlers[name] || []).push(handler); return Promise.resolve(() => {}); } },
    window: { getCurrentWindow: () => ({ setTitle: (t) => { document.title = t; return Promise.resolve(); }, close: () => { document.title = "closed"; return Promise.resolve(); } }) },
  };
})();`;

let child = null;
let clients = [];
const emit = (event, payload) => {
  const line = `data: ${JSON.stringify({ event, payload })}\n\n`;
  clients.forEach((res) => res.write(line));
};

const commands = {
  term_start({ rows, cols }) {
    if (child) return;
    child = spawn("python3", [host, "--rows", String(rows), "--cols", String(cols), "--", ...program], { stdio: ["pipe", "pipe", "inherit"] });
    let buffer = Buffer.alloc(0);
    let status = 1;
    child.stdout.on("data", (data) => {
      buffer = Buffer.concat([buffer, data]);
      while (buffer.length >= 5) {
        const length = buffer.readUInt32BE(1);
        if (buffer.length < 5 + length) break;
        const kind = String.fromCharCode(buffer[0]);
        const payload = buffer.subarray(5, 5 + length);
        buffer = buffer.subarray(5 + length);
        if (kind === "O") emit("term:output", payload.toString("base64"));
        if (kind === "E") status = payload.readInt32BE(0);
      }
    });
    child.on("close", () => { child = null; emit("term:exit", status); });
  },
  term_input({ data }) { child?.stdin.write(frame("I", Buffer.from(data))); },
  term_resize({ rows, cols }) {
    const payload = Buffer.alloc(4);
    payload.writeUInt16BE(rows, 0);
    payload.writeUInt16BE(cols, 2);
    child?.stdin.write(frame("R", payload));
  },
};

createServer((req, res) => {
  if (req.url === "/events") {
    res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
    clients.push(res);
    req.on("close", () => { clients = clients.filter((c) => c !== res); });
    return;
  }
  if (req.url === "/tauri-shim.js") {
    res.writeHead(200, { "content-type": "text/javascript" });
    return res.end(SHIM);
  }
  if (req.method === "POST" && req.url.startsWith("/invoke/")) {
    let body = "";
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", () => {
      const command = commands[req.url.slice("/invoke/".length)];
      if (!command) { res.writeHead(404); return res.end("unknown command"); }
      command(JSON.parse(body || "{}"));
      res.writeHead(200, { "content-type": "application/json" });
      res.end("null");
    });
    return;
  }
  const path = normalize(join(ui, req.url === "/" ? "index.html" : req.url.split("?")[0]));
  if (!path.startsWith(ui) || !existsSync(path)) { res.writeHead(404); return res.end(); }
  res.writeHead(200, { "content-type": types[extname(path)] || "application/octet-stream" });
  if (path.endsWith("index.html")) {
    let html = "";
    createReadStream(path).on("data", (c) => { html += c; }).on("end", () =>
      res.end(html.replace('<script src="vendor/xterm.js">', '<script src="tauri-shim.js"></script>\n  <script src="vendor/xterm.js">')));
    return;
  }
  createReadStream(path).pipe(res);
}).listen(port, "127.0.0.1", () => console.log(`http://127.0.0.1:${port}`));

process.on("SIGTERM", () => { child?.kill(); process.exit(0); });
