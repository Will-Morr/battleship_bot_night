"use strict";

// -- helpers -----------------------------------------------------------------

async function getJSON(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(path + " -> " + r.status);
  return r.json();
}
const pct = (x) => (x == null ? "–" : (x * 100).toFixed(1) + "%");
const one = (x) => (x == null ? "–" : x.toFixed(1));
const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const el = (id) => document.getElementById(id);

// Live updates: refresh on any server-sent event, plus a slow fallback poll.
function subscribe(refresh) {
  refresh();
  try {
    new EventSource("/events").onmessage = () => refresh();
  } catch (e) { /* fall back to polling only */ }
  setInterval(refresh, 4000);
}

// Bucket a {round: count} histogram into 10-round bins (0-9, ..., 90-99, 100).
function buckets(hist) {
  const b = new Array(11).fill(0);
  for (const [round, count] of Object.entries(hist || {})) {
    const r = Number(round);
    b[Math.min(10, Math.floor(r / 10))] += count;
  }
  return b;
}
function histSVG(hist, cls) {
  const b = buckets(hist);
  const max = Math.max(1, ...b);
  const cols = b.map((v) => `<div class="col ${cls}" style="height:${(v / max) * 100}%" title="${v}"></div>`).join("");
  return `<div class="hist">${cols}</div><div class="axis"><span>0</span><span>50</span><span>100 turns</span></div>`;
}

// -- projector board ---------------------------------------------------------

function initProjector() {
  const body = el("board-body");
  const sub = el("sub");
  async function refresh() {
    const rows = await getJSON("/api/rankings");
    const active = rows.filter((r) => r.active).length;
    sub.innerHTML = `<span class="dot"></span>live &middot; ${rows.length} bots &middot; ${active} active`;
    body.innerHTML = rows.map((r, i) => {
      const top = i < 3 ? ` top${i + 1}` : "";
      return `<tr class="${top}${r.active ? "" : " inactive"}">
        <td class="rank">${i + 1}</td>
        <td class="l"><span class="name">${esc(r.name)}</span> <span class="player">${esc(r.player)}</span></td>
        <td>${r.games}</td>
        <td>${pct(r.win_rate)}</td>
        <td>${one(r.solver_avg)}</td>
        <td>${one(r.layout_avg)}</td>
        <td><div class="bar"><span style="width:${(r.combined * 100).toFixed(0)}%"></span></div></td>
      </tr>`;
    }).join("");
  }
  subscribe(refresh);
}

// -- stats page --------------------------------------------------------------

function initStats() {
  let bots = [];
  async function refresh() {
    bots = await getJSON("/api/rankings");
    renderRankings(bots);
    renderPairings(await getJSON("/api/pairings"), bots);
    const open = el("detail").dataset.bot;
    if (open) showBot(open);
  }
  window.showBot = showBot;
  subscribe(refresh);
}

function renderRankings(rows) {
  el("rankings").innerHTML = `
    <thead><tr><th>#</th><th class="l">bot</th><th class="l">player</th><th>games</th>
      <th>win%</th><th>solver↓</th><th>layout↑</th><th>combined</th><th></th></tr></thead>
    <tbody>${rows.map((r) => `
      <tr class="clickable ${r.active ? "" : "inactive"}" onclick="showBot('${r.bot_uuid}')">
        <td>${r.rank}</td><td class="l">${esc(r.name)}</td><td class="l">${esc(r.player)}</td>
        <td>${r.games}</td><td>${pct(r.win_rate)}</td><td>${one(r.solver_avg)}</td>
        <td>${one(r.layout_avg)}</td>
        <td><div class="bar"><span style="width:${(r.combined * 100).toFixed(0)}%"></span></div></td>
        <td>${r.active ? '<span class="tag on">live</span>' : '<span class="tag">idle</span>'}</td>
      </tr>`).join("")}</tbody>`;
}

async function showBot(uuid) {
  const d = await getJSON("/api/bot/" + uuid);
  const box = el("detail");
  box.dataset.bot = uuid;
  const opps = d.opponents.map((o) => `<tr><td class="l">${esc(o.name)} <span class="muted">${esc(o.player)}</span></td>
    <td>${o.games}</td><td>${pct(o.win_rate)}</td><td>${one(o.solver_avg)}</td><td>${one(o.layout_avg)}</td></tr>`).join("");
  box.innerHTML = `
    <div class="row"><h2>${esc(d.name)} <span class="muted">${esc(d.player)}</span></h2>
      <div class="spacer"></div><div class="muted">${d.games} games</div></div>
    <div class="grid2">
      <div><div class="muted">solver — turns to clear an opponent (lower is better)</div>${histSVG(d.solver_hist, "")}</div>
      <div><div class="muted">layout — turns opponents took to crack you (higher is better)</div>${histSVG(d.layout_hist, "layout")}</div>
    </div>
    <h2 style="margin-top:1rem">vs each opponent</h2>
    <table><thead><tr><th class="l">opponent</th><th>games</th><th>win%</th><th>solver↓</th><th>layout↑</th></tr></thead>
      <tbody>${opps || '<tr><td class="muted">no games yet</td></tr>'}</tbody></table>`;
}

function renderPairings(pairs, bots) {
  // Build a win% matrix over bots that have played. cell(row, col) = row's win% vs col.
  const names = new Map(bots.map((b) => [b.bot_uuid, b.name]));
  const ids = [...new Set(pairs.flatMap((p) => [p.bot1, p.bot2]))].filter((id) => names.has(id));
  ids.sort((a, b) => (names.get(a) || "").localeCompare(names.get(b) || ""));
  const cell = {};
  for (const p of pairs) {
    const total = p.bot1_wins + p.bot2_wins + p.ties;
    if (!total) continue;
    cell[p.bot1 + "|" + p.bot2] = (p.bot1_wins + 0.5 * p.ties) / total;
    cell[p.bot2 + "|" + p.bot1] = (p.bot2_wins + 0.5 * p.ties) / total;
  }
  const head = ids.map((id) => `<th>${esc((names.get(id) || "?").slice(0, 6))}</th>`).join("");
  const body = ids.map((r) => {
    const cells = ids.map((c) => {
      if (r === c) return '<td class="muted">·</td>';
      const v = cell[r + "|" + c];
      if (v == null) return "<td>–</td>";
      const g = Math.round(v * 160), rd = Math.round((1 - v) * 160);
      return `<td style="color:rgb(${rd},${g},120)">${(v * 100).toFixed(0)}</td>`;
    }).join("");
    return `<tr><td class="l">${esc(names.get(r))}</td>${cells}</tr>`;
  }).join("");
  el("pairings").innerHTML = ids.length
    ? `<thead><tr><th class="l">win% (row vs col)</th>${head}</tr></thead><tbody>${body}</tbody>`
    : "<tbody><tr><td class='muted'>no games yet</td></tr></tbody>";
}
