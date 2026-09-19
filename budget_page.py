"""`/prop budget N output`: a self-contained page for tuning the context budget.

The page is built from what one captured request actually held — the model,
its window, every block's size and text, every tool's schema cost — and
re-runs the budget arithmetic in the browser as the sliders move: which
blocks would be compressed, which tools kept, what the thread is left with.
It exports only the values that changed, as a `.config` file — one
`/config` line per setting, the format `/config import` reads for any
setting. The gateway's own block is never part of it.

One file, no network: it has to open from a server's disk as easily as from a
laptop, and a budget page that fetched a script from a CDN would stop working
exactly where it is most needed.
"""
from __future__ import annotations

import html
import json
import time
from pathlib import Path

_TEXT_LIMIT = 200_000


def page_data(rows: dict, window: dict, described: dict, *, newest_index: int) -> dict:
    """Everything the page needs, with the gateway left out."""
    nodes: dict = {}
    hysteresis = None
    scalars: dict = {}
    for key, meta in described.items():
        if not key.startswith("budget."):
            continue
        rest = key[len("budget."):]
        if rest == "hysteresis":
            hysteresis = {"value": meta["value"], "default": meta["default"]}
            continue
        if "." not in rest:
            # Other single values (assumed windows, chars per token).
            scalars[rest] = {"value": meta["value"], "default": meta["default"],
                             "description": meta.get("description", "")}
            continue
        path, leaf = rest.rsplit(".", 1)
        nodes.setdefault(path, {})[leaf] = {"value": meta["value"], "default": meta["default"]}
    blocks = {}
    for section in ("system", "tools", "live", "thread"):
        for row in rows.get(section) or []:
            path = str(row.get("path") or "")
            if not path or ".gateway" in path:
                continue
            blocks[path] = {
                "depth": int(row.get("depth") or 1),
                "natural": int(row.get("natural") or 0),
                "allotted": int(row.get("allotted") or 0),
                "delivered": int(row.get("delivered") or 0),
                "triggered": bool(row.get("triggered")),
                "original": str(row.get("original") or "")[:_TEXT_LIMIT],
                "compressed": str(row.get("compressed") or "")[:_TEXT_LIMIT],
                "items": row.get("items") or [],
            }
    return {"version": 1, "request": newest_index,
            "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "window": window or {}, "nodes": nodes, "hysteresis": hysteresis,
            "scalars": scalars,
            "blocks": blocks}


def write_page(data: dict, target: Path) -> Path:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    title = f"Context budget · request #{data.get('request', 1)}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_TEMPLATE.replace("__TITLE__", html.escape(title))
                      .replace("__DATA__", payload), encoding="utf-8")
    return target


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1d1b;--dim:#6b6b66;--line:#e2e1dc;--accent:#2f6fdb;
--out:#9aa0a6;--system:#2f6fdb;--tools:#8e5bd8;--live:#d08a1c;--thread:#2e9d6a;--free:#dfe7e2;--warn:#c9432f;--ok:#2e9d6a}
@media (prefers-color-scheme: dark){:root{--bg:#141413;--panel:#1d1d1b;--ink:#ecebe6;--dim:#9b9a94;--line:#33322f;
--accent:#6f9df0;--free:#26302b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{padding:16px 20px;border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:5}
h1{font-size:17px;margin:0 0 4px}h2{font-size:14px;margin:0 0 10px;text-transform:uppercase;letter-spacing:.04em;color:var(--dim)}
.meta{color:var(--dim);font-size:13px;display:flex;flex-wrap:wrap;gap:4px 18px}
.bar{display:flex;height:26px;border-radius:6px;overflow:hidden;margin-top:12px;border:1px solid var(--line)}
.bar div{height:100%;min-width:2px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:11px;white-space:nowrap;overflow:hidden;transition:width .15s}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:var(--dim);margin-top:6px}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
main{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:16px;padding:16px 20px}
@media (max-width:900px){main{grid-template-columns:1fr}}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:16px}
.node{border-left:2px solid var(--line);padding:6px 0 6px 10px;margin:4px 0}
.node.over{border-left-color:var(--warn)}
.row{display:grid;grid-template-columns:minmax(120px,1.1fr) minmax(120px,1.4fr) 70px 64px;gap:8px;align-items:center}
.name{font-weight:600;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.name:hover{color:var(--accent)}
.name small{font-weight:400;color:var(--dim)}
input[type=range]{width:100%;accent-color:var(--accent)}input[type=number],select{width:100%;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:5px;padding:2px 4px;font:inherit;font-size:12px}
.stat{font-size:12px;color:var(--dim);margin-top:2px}.stat b{color:var(--ink);font-weight:600}
.badge{font-size:11px;padding:1px 6px;border-radius:9px;background:var(--free);color:var(--ink)}.badge.over{background:var(--warn);color:#fff}
.mini{height:5px;background:var(--free);border-radius:3px;margin-top:4px;overflow:hidden}.mini div{height:100%;background:var(--accent)}.mini div.over{background:var(--warn)}
details>summary{cursor:pointer;color:var(--dim);font-size:12px;margin:2px 0}
table{width:100%;border-collapse:collapse;font-size:12px}td,th{padding:3px 6px;border-bottom:1px solid var(--line);text-align:left}th{color:var(--dim);font-weight:500}
.drop{color:var(--warn);text-decoration:line-through}.core{color:var(--dim)}
pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px;max-height:340px;overflow:auto;font:12px/1.4 ui-monospace,Menlo,Consolas,monospace;margin:6px 0}
.cut{color:var(--warn)}button{background:var(--accent);color:#fff;border:0;border-radius:6px;padding:6px 12px;font:inherit;cursor:pointer;margin:0 6px 6px 0}button.ghost{background:transparent;color:var(--accent);border:1px solid var(--accent)}
textarea{width:100%;min-height:120px;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:6px;font:12px ui-monospace,Menlo,monospace;padding:8px}
.err{color:var(--warn);font-size:12px;min-height:16px}.hint{color:var(--dim);font-size:12px}
.th{position:relative;height:18px;background:var(--free);border-radius:4px;margin:8px 0 18px}.th .used{position:absolute;left:0;top:0;bottom:0;background:var(--thread);border-radius:4px}
.th .mark{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--ink)}.th .mark span{position:absolute;top:22px;left:-20px;font-size:10px;color:var(--dim);white-space:nowrap}
</style>
</head>
<body>
<header>
  <h1 id="title"></h1>
  <div class="meta" id="meta"></div>
  <div class="bar" id="bar"></div>
  <div class="legend" id="legend"></div>
</header>
<main>
  <div>
    <section><h2>System prompt</h2><div id="tree-system"></div></section>
    <section><h2>Tools</h2><div id="tree-tools"></div><div id="toollist"></div></section>
    <section><h2>Live tail</h2><div id="tree-live"></div></section>
  </div>
  <div>
    <section><h2>Window</h2><div id="tree-output"></div><div id="hyst"></div></section>
    <section><h2>Thread</h2><div id="thread"></div></section>
    <section><h2>Block</h2><div id="preview" class="hint">Click a block's name to see its text and what the current shares would send.</div></section>
    <section><h2>Export</h2>
      <button id="dl">Download .config</button><button class="ghost" id="reset-cur">Undo changes</button><button class="ghost" id="reset-def">Shipped defaults</button>
      <div class="err" id="err"></div>
      <textarea id="out" readonly></textarea>
      <div class="hint">Apply it in the CLI: <code>/config import &lt;file&gt;</code>. One <code>/config</code> line per changed setting — the same file can hold any other setting too. Preview figures are estimates; the next request applies them exactly.</div>
    </section>
  </div>
</main>
<script id="data" type="application/json">__DATA__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("data").textContent);
const B = D.blocks, W = D.window || {};
const initial = {}, cfg = {};
for (const [path, leaves] of Object.entries(D.nodes)) {
  cfg[path] = {}; initial[path] = {};
  for (const [leaf, v] of Object.entries(leaves)) { cfg[path][leaf] = v.value; initial[path][leaf] = v.value; }
}
let hyst = D.hysteresis ? D.hysteresis.value : 0.9; const hystInit = hyst;
const scal = {}, scalInit = {};
for (const [k, v] of Object.entries(D.scalars || {})) { scal[k] = v.value; scalInit[k] = v.value; }
const fmt = n => (n >= 10000 ? (n / 1000).toFixed(1) + "K" : String(Math.round(n)));
const kids = p => Object.keys(B).filter(k => k.startsWith(p + ".") && !k.slice(p.length + 1).includes("."));
const share = p => (cfg[p] && typeof cfg[p].share === "number") ? cfg[p].share : 0;
const minOf = p => (cfg[p] && typeof cfg[p].min === "number") ? cfg[p].min : 0;
const shrinkOf = p => { let q = p; while (q) { if (cfg[q] && cfg[q].shrink) return cfg[q].shrink; q = q.includes(".") ? q.slice(0, q.lastIndexOf(".")) : ""; } return "truncate"; };
const allot = (p, parent) => Math.max(minOf(p), Math.floor(share(p) * Math.max(0, parent)));

function distribute(budget, naturals) {           // mirrors prompt_budget.distribute
  const res = {}; let remaining = budget; const flex = [];
  for (const [n, size] of Object.entries(naturals)) {
    if (n === "\u0000rest" || shrinkOf(n) === "none") { res[n] = size; remaining -= size; } else flex.push(n);
  }
  remaining = Math.max(0, remaining); let open = flex.slice();
  while (open.length) {
    const w = {}; let tw = 0; for (const n of open) { w[n] = Math.max(share(n), 1e-9); tw += w[n]; }
    const target = {}; for (const n of open) target[n] = Math.max(minOf(n), Math.floor(remaining * w[n] / tw));
    const settled = open.filter(n => naturals[n] <= target[n]);
    if (!settled.length) { for (const n of open) res[n] = Math.min(naturals[n], target[n]); break; }
    for (const n of settled) { res[n] = naturals[n]; remaining = Math.max(0, remaining - naturals[n]); open = open.filter(x => x !== n); }
  }
  return res;
}
const R = {};
function simulate(p, budget, enforce) {           // mirrors prompt_budget.fit
  const nat = B[p].natural, ks = kids(p), over = enforce && nat > budget;
  R[p] = {natural: nat, allotted: enforce ? budget : nat, over};
  if (!over) { for (const k of ks) simulate(k, B[k].natural, false); R[p].delivered = nat; return nat; }
  const pool = {}; let sum = 0; for (const k of ks) { pool[k] = B[k].natural; sum += B[k].natural; }
  pool["\u0000rest"] = Math.max(0, nat - sum);
  const split = distribute(budget, pool); let del = pool["\u0000rest"];
  for (const k of ks) { const d = simulate(k, split[k], true); del += Math.min(d, split[k]); }
  if (!ks.length) { const s = shrinkOf(p); del = s === "none" ? nat : (s === "drop" ? 0 : Math.min(nat, budget)); }
  R[p].delivered = del; return del;
}
let toolKeep = null;
function compute() {
  for (const k in R) delete R[k];
  const win = W.window || 0;
  const ceilings = [W.max_output, W.max_tokens].filter(x => x > 0);
  let output = allot("output", win); if (ceilings.length) output = Math.min(output, Math.min(...ceilings)); output = Math.min(output, win);
  const input = win - output; R.output = {allotted: output};
  let fixed = 0;
  for (const top of ["system", "live"]) if (B[top]) fixed += simulate(top, allot(top, input), true);
  if (B.tools) {
    const t = B.tools, a = allot("tools", input), items = t.items || [];
    const over = shrinkOf("tools") !== "none" && t.natural > a; let kept = items.map(i => i.name);
    if (over) {
      const ord = items.slice().sort((x, y) => (x.core === y.core ? x.name.localeCompare(y.name) : (x.core ? -1 : 1)));
      let used = ord.filter(i => i.core).reduce((s, i) => s + i.tokens, 0); kept = [];
      for (const i of ord) { if (i.core) kept.push(i.name); else if (used + i.tokens <= a) { kept.push(i.name); used += i.tokens; } }
    }
    toolKeep = new Set(kept);
    const del = items.filter(i => toolKeep.has(i.name)).reduce((s, i) => s + i.tokens, 0);
    R.tools = {natural: t.natural, allotted: a, over, delivered: items.length ? del : t.natural}; fixed += R.tools.delivered;
  }
  const usable = Math.max(0, input - fixed);
  R.thread = {usable, natural: B.thread ? B.thread.natural : 0};
  return {win, output, input, usable};
}

function slider(path, leaf, max, step) {
  const v = cfg[path][leaf];
  if (leaf === "shrink") {
    const opts = ["none", "truncate", "tail", "drop", "narrow"].map(o => `<option${o === v ? " selected" : ""}>${o}</option>`).join("");
    return `<select data-p="${path}" data-l="shrink">${opts}</select>`;
  }
  if (leaf === "min") return `<input type="number" min="0" step="100" value="${v}" data-p="${path}" data-l="min" title="minimum tokens">`;
  return `<input type="range" min="0" max="${max}" step="${step}" value="${v}" data-p="${path}" data-l="${leaf}" title="${leaf}">`;
}
const id = p => p.replace(/[^A-Za-z0-9_]/g, "_");
// Controls are built once; moving a slider only updates figures, so the
// slider under the pointer is never replaced mid-drag.
function nodeHTML(p, depth) {
  const c = cfg[p] || {}, name = p.split(".").pop(), k = id(p);
  let h = `<div class="node" id="nd-${k}" style="margin-left:${Math.max(0, depth - 1) * 10}px">
    <div class="row"><div class="name" data-show="${p}" title="${p.replace(/\./g, " ")}">${name} <small>${B[p] ? fmt(B[p].natural) + " tok" : ""}</small></div>
    <div>${"share" in c ? slider(p, "share", depth === 2 ? 0.4 : 1, depth === 2 ? 0.0025 : 0.005) : ""}</div>
    <div>${"min" in c ? slider(p, "min") : `<span class="hint" id="pc-${k}"></span>`}</div>
    <div>${"shrink" in c ? slider(p, "shrink") : `<span class="badge" id="bd-${k}"></span>`}</div></div>`;
  if (B[p]) h += `<div class="stat" id="st-${k}"></div><div class="mini"><div id="mb-${k}"></div></div>`;
  const ks = Object.keys(cfg).filter(x => x.startsWith(p + ".") && !x.slice(p.length + 1).includes("."));
  if (ks.length) {
    const inner = ks.map(x => nodeHTML(x, depth + 1)).join("");
    h += depth < 2 ? inner : `<details><summary>${ks.length} level-${depth + 1} marker(s)</summary>${inner}</details>`;
  }
  return h + "</div>";
}
function build() {
  compute();
  for (const top of ["system", "live", "tools", "output"])
    document.getElementById("tree-" + top).innerHTML = cfg[top] ? nodeHTML(top, 1) : "";
  document.getElementById("hyst").innerHTML = `<div class="node"><div class="row"><div class="name">release band</div><div><input type="range" min="0.5" max="1" step="0.01" value="${hyst}" id="hyst-in"></div><div class="hint" id="hyst-v"></div><div></div></div><div class="stat">a compressed block is released below this share of its allotment</div></div>` +
    Object.entries(D.scalars || {}).map(([k, v]) => `<div class="node"><div class="row"><div class="name" title="${esc(v.description || "")}">${k.replace(/_/g, " ")}</div><div><input type="number" step="${k === "chars_per_token" ? 0.1 : 1024}" min="0" value="${scal[k]}" data-s="${k}"></div><div></div><div></div></div><div class="stat">${esc(v.description || "")}</div></div>`).join("");
  let tt = `<div class="stat" id="th-now"></div><div class="th" id="th-bar"></div>`;
  for (const k of ["compact_target", "compact_background", "compact_foreground"])
    tt += `<div class="row"><div class="name">${k.replace("compact_", "compact ")}</div><div>${slider("thread", k, 1, 0.01)}</div><div class="hint" id="pc-thread_${k}"></div><div></div></div>`;
  tt += Object.keys(cfg).filter(k => k.startsWith("thread.")).map(k =>
    `<div class="row"><div class="name">${k.slice(7).replace(/_/g, " ")}</div><div>${slider(k, "share", 0.5, 0.001)}</div><div>${slider(k, "min")}</div><div class="hint" id="sz-${id(k)}"></div></div>`).join("");
  const aux = Object.keys(cfg).filter(k => k.startsWith("aux.") && "share" in cfg[k]);
  if (aux.length) tt += `<details><summary>auxiliary calls (summary, critic, memory) — shares of the auxiliary model's window</summary>` +
    aux.map(k => `<div class="row"><div class="name">${k.slice(4).replace(/\./g, " ")}</div><div>${slider(k, "share", 0.5, 0.001)}</div><div>${slider(k, "min")}</div><div></div></div>`).join("") + "</details>";
  document.getElementById("thread").innerHTML = tt;
  update();
}
function update() {
  const t = compute();
  document.getElementById("title").textContent = `Context budget · request #${D.request} · ${W.model || "model"}`;
  document.getElementById("meta").innerHTML = [
    `model <b>${W.model || "?"}</b>`, `window <b>${fmt(t.win)}</b>`, `model output ceiling <b>${W.max_output ? fmt(W.max_output) : "?"}</b>`,
    `output reserve <b>${fmt(t.output)}</b>`, `input <b>${fmt(t.input)}</b>`, `thread budget <b>${fmt(t.usable)}</b>`, `captured ${D.generated}`].join(" · ");
  const parts = [["output", t.output, "var(--out)"], ["system", (R.system || {}).delivered || 0, "var(--system)"],
    ["tools", (R.tools || {}).delivered || 0, "var(--tools)"], ["live", (R.live || {}).delivered || 0, "var(--live)"],
    ["thread used", Math.min(R.thread.natural, t.usable), "var(--thread)"], ["thread free", Math.max(0, t.usable - R.thread.natural), "var(--free)"]];
  document.getElementById("bar").innerHTML = parts.map(([n, v, c]) => `<div title="${n}: ${fmt(v)}" style="width:${t.win ? 100 * v / t.win : 0}%;background:${c}">${t.win && v / t.win > .06 ? n : ""}</div>`).join("");
  document.getElementById("legend").innerHTML = parts.map(([n, v, c]) => `<span><i style="background:${c}"></i>${n} ${fmt(v)}</span>`).join("");
  R.output.natural = t.output; R.output.delivered = t.output;
  for (const p of Object.keys(cfg)) {
    const k = id(p), r = R[p] || {}, c = cfg[p], over = !!r.over;
    const nd = document.getElementById("nd-" + k); if (nd) nd.classList.toggle("over", over);
    const pc = document.getElementById("pc-" + k); if (pc && "share" in c) pc.textContent = (c.share * 100).toFixed(1) + "%";
    const bd = document.getElementById("bd-" + k); if (bd) { bd.textContent = over ? "compress" : "as is"; bd.classList.toggle("over", over); }
    const st = document.getElementById("st-" + k), mb = document.getElementById("mb-" + k);
    if (st && B[p]) {
      const del = r.delivered ?? B[p].natural;
      st.innerHTML = `allotted <b>${fmt(r.allotted ?? 0)}</b> · sends <b>${fmt(del)}</b> of ${fmt(B[p].natural)}${"share" in c ? ` · share ${(c.share * 100).toFixed(1)}%` : ""}`;
      mb.style.width = (B[p].natural ? Math.min(100, 100 * del / B[p].natural) : 100) + "%"; mb.classList.toggle("over", over);
    }
  }
  if (R.output) { const st = document.getElementById("pc-output"); if (st) st.textContent = fmt(t.output) + " tok"; }
  const items = (B.tools && B.tools.items) || [];
  document.getElementById("toollist").innerHTML = items.length ? `<details${R.tools && R.tools.over ? " open" : ""}><summary>${items.length} tool schemas · ${toolKeep.size} kept</summary><table><tr><th>tool</th><th>tokens</th><th></th></tr>` +
    items.map(i => `<tr><td class="${toolKeep.has(i.name) ? "" : "drop"}">${i.name}</td><td>${i.tokens}</td><td class="core">${i.core ? "core — always kept" : (toolKeep.has(i.name) ? "" : "dropped")}</td></tr>`).join("") + "</table></details>" : "";
  document.getElementById("hyst-v").textContent = Math.round(hyst * 100) + "%";
  const th = cfg.thread || {}, u = t.usable || 1, used = R.thread.natural;
  const mark = (k, label) => `<div class="mark" style="left:${100 * (th[k] || 0)}%"><span>${label} ${fmt((th[k] || 0) * u)}</span></div>`;
  document.getElementById("th-now").innerHTML = `thread now <b>${fmt(used)}</b> of <b>${fmt(u)}</b> (${Math.round(100 * used / u)}%)`;
  document.getElementById("th-bar").innerHTML = `<div class="used" style="width:${Math.min(100, 100 * used / u)}%"></div>${mark("compact_target", "target")}${mark("compact_background", "background")}${mark("compact_foreground", "foreground")}`;
  for (const k of ["compact_target", "compact_background", "compact_foreground"]) document.getElementById("pc-thread_" + k).textContent = Math.round(th[k] * 100) + "%";
  for (const k of Object.keys(cfg).filter(k => k.startsWith("thread."))) { const c = cfg[k]; document.getElementById("sz-" + id(k)).textContent = fmt(Math.max(c.min || 0, c.share * u)) + " tok"; }
  exportText(); if (shown) showBlock(shown);
}
let shown = null;
function showBlock(p) {
  shown = p; const b = B[p], r = R[p] || {}; if (!b) return;
  const ratio = b.natural ? Math.min(1, (r.delivered ?? b.natural) / b.natural) : 1, text = b.original || b.compressed || "";
  const keep = Math.floor(text.length * ratio);
  const body = r.over ? `<pre>${esc(text.slice(0, keep))}<span class="cut">\n[… ${p.replace(/\./g, " ")} compressed to fit its budget]</span></pre><div class="hint">≈ what the current shares would send; the removed part:</div><pre class="cut">${esc(text.slice(keep))}</pre>`
    : `<pre>${esc(text)}</pre>`;
  document.getElementById("preview").innerHTML = `<b>${p.replace(/\./g, " ")}</b> <span class="badge${r.over ? " over" : ""}">${r.over ? "would be compressed" : "sent as is"}</span>
    <div class="stat">${fmt(b.natural)} tokens · allotted ${fmt(r.allotted ?? b.natural)} · at capture it sent ${fmt(b.delivered)}</div>${body}`;
}
const esc = s => s.replace(/[&<>]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]));
function changes() {
  const out = {};
  for (const [p, leaves] of Object.entries(cfg)) for (const [l, v] of Object.entries(leaves))
    if (initial[p][l] !== v) out[`budget ${p.replace(/\./g, " ")} ${l}`] = v;
  if (hyst !== hystInit) out["budget hysteresis"] = hyst;
  for (const k in scal) if (scal[k] !== scalInit[k]) out[`budget ${k}`] = scal[k];
  return out;
}
function exportText() {
  const th = cfg.thread || {}, err = document.getElementById("err");
  err.textContent = (th.compact_target < th.compact_background && th.compact_background < th.compact_foreground && th.compact_foreground <= 1)
    ? "" : "Compaction needs target < background < foreground ≤ 1 — the CLI will refuse this file.";
  const values = changes(), keys = Object.keys(values).sort();
  document.getElementById("out").value = [
    "# laintas-cli settings: one `/config` line per setting; apply with /config import <file>",
    `# from the budget page: request #${D.request}, ${W.model || "model"}, window ${W.window || "?"}`,
    ...keys.map(k => `${k} ${values[k]}`)].join("\n") + "\n";
  document.getElementById("dl").disabled = !keys.length;
}
function apply(el) {
  if (el.id === "hyst-in") { hyst = parseFloat(el.value); update(); return; }
  if (el.dataset.s) { const v = parseFloat(el.value); if (v > 0) scal[el.dataset.s] = el.dataset.s === "chars_per_token" ? v : Math.round(v); update(); return; }
  if (!el.dataset.p) return;
  const p = el.dataset.p, l = el.dataset.l;
  cfg[p][l] = l === "shrink" ? el.value : (l === "min" ? Math.max(0, parseInt(el.value || "0", 10)) : parseFloat(el.value));
  update();
}
// Sliders and selects follow the pointer; a number box applies once typed.
document.addEventListener("input", e => { if (e.target.type !== "number") apply(e.target); });
document.addEventListener("change", e => { if (e.target.type === "number") apply(e.target); });
document.addEventListener("click", e => { const s = e.target.closest("[data-show]"); if (s) showBlock(s.dataset.show); });
document.getElementById("dl").onclick = () => {
  const blob = new Blob([document.getElementById("out").value], {type: "text/plain"});
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `budget-${D.request}.config`; a.click();
};
document.getElementById("reset-cur").onclick = () => { for (const p in cfg) Object.assign(cfg[p], initial[p]); hyst = hystInit; Object.assign(scal, scalInit); build(); };
document.getElementById("reset-def").onclick = () => { for (const [p, leaves] of Object.entries(D.nodes)) for (const [l, v] of Object.entries(leaves)) cfg[p][l] = v.default; if (D.hysteresis) hyst = D.hysteresis.default; for (const [k, v] of Object.entries(D.scalars || {})) scal[k] = v.default; build(); };
build();
</script>
</body>
</html>
"""
