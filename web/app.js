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

// Every panel re-renders from scratch on each poll. Writing identical markup back into
// the DOM still collapses and re-lays-out the page, which throws the reader to the top
// mid-scroll — so skip no-op writes, and hold the scroll position across real ones.
const PAINTED = new WeakMap();
function paint(node, html) {
  if (!node || PAINTED.get(node) === html) return false;
  const y = window.scrollY;
  node.innerHTML = html;
  PAINTED.set(node, html);
  if (window.scrollY !== y) window.scrollTo(window.scrollX, y);
  return true;
}

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
    paint(sub, `<span class="dot"></span>live &middot; ${rows.length} bots &middot; ${active} active`);
    paint(body, rows.map((r, i) => {
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
    }).join(""));
  }
  subscribe(refresh);
}

// -- stats page (tabbed: leaderboard / bot / compare) ------------------------

let BOTS = [];
let currentTab = "leaderboard";

function initStats() {
  document.querySelectorAll(".tabs button").forEach((btn) => {
    btn.onclick = () => setTab(btn.dataset.tab);
  });
  window.openBot = openBot;
  window.openGame = openGame;
  el("botSelect").onchange = (e) => showBot(e.target.value);
  el("autoRefresh").onchange = (e) => { if (e.target.checked) refresh(); };
  el("cmpA").onchange = () => renderCompare(el("cmpA").value, el("cmpB").value);
  el("cmpB").onchange = () => renderCompare(el("cmpA").value, el("cmpB").value);
  subscribe(refresh);
}

async function refresh() {
  // Unchecking "live updates" freezes the page — nothing moves while you study a board.
  const auto = el("autoRefresh");
  if (auto && !auto.checked) return;
  BOTS = await getJSON("/api/rankings");
  renderRankings(BOTS);
  renderPairings(await getJSON("/api/pairings"), BOTS);
  if (BOTS.length) {
    fillSelect(el("botSelect"), BOTS[0].bot_uuid);
    fillSelect(el("cmpA"), BOTS[0].bot_uuid);
    fillSelect(el("cmpB"), (BOTS[1] || BOTS[0]).bot_uuid);
  }
  if (currentTab === "bot" && el("botSelect").value) showBot(el("botSelect").value);
  if (currentTab === "compare") renderCompare(el("cmpA").value, el("cmpB").value);
}

function fillSelect(sel, def) {
  const cur = sel.value;
  sel.innerHTML = BOTS.map((b) =>
    `<option value="${b.bot_uuid}">${esc(b.name)} (${esc(b.player)})</option>`).join("");
  sel.value = BOTS.some((b) => b.bot_uuid === cur) ? cur : def;
}

function setTab(name) {
  currentTab = name;
  document.querySelectorAll(".tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name));
  ["leaderboard", "bot", "compare"].forEach((t) => { el("tab-" + t).hidden = t !== name; });
  if (name === "bot" && el("botSelect").value) showBot(el("botSelect").value);
  if (name === "compare") renderCompare(el("cmpA").value, el("cmpB").value);
}

function openBot(uuid) {
  setTab("bot");
  el("botSelect").value = uuid;
  showBot(uuid);
}

function renderRankings(rows) {
  paint(el("rankings"), `
    <thead><tr><th>#</th><th class="l">bot</th><th class="l">player</th><th>games</th>
      <th>win%</th><th>solver↓</th><th>layout↑</th><th>combined</th><th></th></tr></thead>
    <tbody>${rows.map((r) => `
      <tr class="clickable ${r.active ? "" : "inactive"}" onclick="openBot('${r.bot_uuid}')">
        <td>${r.rank}</td><td class="l">${esc(r.name)}</td><td class="l">${esc(r.player)}</td>
        <td>${r.games}</td><td>${pct(r.win_rate)}</td><td>${one(r.solver_avg)}</td>
        <td>${one(r.layout_avg)}</td>
        <td><div class="bar"><span style="width:${(r.combined * 100).toFixed(0)}%"></span></div></td>
        <td>${r.active ? '<span class="tag on">live</span>' : '<span class="tag">idle</span>'}</td>
      </tr>`).join("")}</tbody>`);
}

async function showBot(uuid) {
  const box = el("detail");
  let d;
  try {
    d = await getJSON("/api/bot/" + uuid);
  } catch (e) {
    // Say so rather than leaving an empty panel that looks like a dead page.
    paint(box, `<span class="muted">could not load this bot: ${esc(e.message)}</span>`);
    return;
  }
  if (box.dataset.bot !== uuid) {
    // New bot: lay down a fixed skeleton once. Everything below repaints into it piece
    // by piece, so a poll that only moves the win rate leaves the game log — and any
    // open board — alone instead of rebuilding the whole panel under the reader.
    OPEN_GAME = null;
    box.dataset.bot = uuid;
    paint(box, `
      <div class="row"><h2 id="botName"></h2>
        <div class="spacer"></div><div class="muted" id="botGames"></div></div>
      <div class="scores" id="botStats"></div>
      <div class="grid2" id="botHists"></div>
      <h2 style="margin-top:1rem">vs each opponent</h2>
      <table><thead><tr><th class="l">opponent</th><th>games</th><th>win%</th><th>solver↓</th><th>layout↑</th></tr></thead>
        <tbody id="botOpps"></tbody></table>
      <h2 style="margin-top:1rem">recent games</h2>
      <div class="muted">click a game to see both boards and the order each side guessed</div>
      <div id="recent"></div>
      <div id="replay"></div>`);
    paint(el("replay"), "");
  }
  paint(el("botName"), `${esc(d.name)} <span class="muted">${esc(d.player)}</span>`);
  paint(el("botGames"), `${d.games} games`);
  paint(el("botStats"), `
    <div class="stat"><div class="k">win rate</div><div class="v">${pct(d.win_rate)}</div></div>
    <div class="stat"><div class="k">solver ↓</div><div class="v">${one(d.solver_avg)}</div></div>
    <div class="stat"><div class="k">layout ↑</div><div class="v">${one(d.layout_avg)}</div></div>`);
  paint(el("botHists"), `
    <div><div class="muted">solver — turns to clear an opponent (lower is better)</div>${histSVG(d.solver_hist, "")}</div>
    <div><div class="muted">layout — turns opponents took to crack you (higher is better)</div>${histSVG(d.layout_hist, "layout")}</div>`);
  paint(el("botOpps"), d.opponents.map((o) => `<tr class="clickable" onclick="openBot('${o.bot_uuid}')">
    <td class="l">${esc(o.name)} <span class="muted">${esc(o.player)}</span></td>
    <td>${o.games}</td><td>${pct(o.win_rate)}</td><td>${one(o.solver_avg)}</td><td>${one(o.layout_avg)}</td></tr>`).join("")
    || '<tr><td class="muted">no games yet</td></tr>');
  renderRecent(uuid);
}

// -- recent-games log --------------------------------------------------------

let OPEN_GAME = null;            // game uuid currently expanded, if any
const REPLAYS = new Map();       // game uuid -> replay (a finished game never changes)

async function renderRecent(botUuid) {
  const box = el("recent");
  if (!box) return;
  let games;
  try {
    games = await getJSON(`/api/bot/${botUuid}/games?limit=10`);
  } catch (e) {
    paint(box, `<span class="muted">could not load recent games: ${esc(e.message)}</span>`);
    return;
  }
  if (!games.length) { paint(box, '<span class="muted">no games yet</span>'); return; }
  paint(box, `
    <table><thead><tr><th class="l">ended</th><th class="l">opponent</th><th class="l">result</th>
      <th>you solved</th><th>they solved</th><th class="l">end reason</th></tr></thead>
      <tbody>${games.map((g) => `
        <tr class="clickable" onclick="openGame('${g.game_uuid}','${botUuid}')">
          <td class="l muted">${clock(g.ended_at)}</td>
          <td class="l">${esc(g.opponent.name)} <span class="muted">${esc(g.opponent.player)}</span></td>
          <td class="l">${outcomeTag(g.outcome)}</td>
          <td>${g.solved_round == null ? "–" : g.solved_round}</td>
          <td>${g.opponent_solved_round == null ? "–" : g.opponent_solved_round}</td>
          <td class="l muted">${esc(g.end_reason || "")}</td>
        </tr>`).join("")}</tbody></table>`);
  if (OPEN_GAME) showGame(OPEN_GAME, botUuid);   // survive the 4s auto-refresh
}

function openGame(gameUuid, botUuid) {
  if (OPEN_GAME === gameUuid) { OPEN_GAME = null; paint(el("replay"), ""); return; }
  OPEN_GAME = gameUuid;
  showGame(gameUuid, botUuid);
}

async function showGame(gameUuid, botUuid) {
  const box = el("replay");
  if (!box) return;
  let g = REPLAYS.get(gameUuid);
  if (!g) {
    try {
      g = await getJSON(`/api/game/${gameUuid}?bot=${botUuid}`);
    } catch (e) {
      paint(box, `<span class="muted">could not load that game: ${esc(e.message)}</span>`);
      return;
    }
    REPLAYS.set(gameUuid, g);
  }
  if (OPEN_GAME !== gameUuid) return;    // a later click won the race
  paint(box, `
    <div class="card replay">
      <div class="row"><strong>${esc(g.you.name)}</strong><span class="muted">vs</span>
        <strong>${esc(g.opponent.name)}</strong><div class="spacer"></div>
        <span class="muted">${g.total_rounds} rounds &middot; ${esc(g.end_reason || "")}</span></div>
      <div class="grid2">
        ${boardBlock(g, g.you, g.opponent)}
        ${boardBlock(g, g.opponent, g.you)}
      </div>
      <div class="legend">
        <span><svg class="swship" viewBox="0 0 3 1"><line x1=".5" y1=".5" x2="2.5" y2=".5"/>
          <circle cx=".5" cy=".5" r=".3"/><circle cx="1.5" cy=".5" r=".3"/>
          <circle cx="2.5" cy=".5" r=".3"/></svg>ship</span><span><i class="sw miss"></i>miss</span>
        <span><i class="sw hit"></i>hit</span><span><i class="sw sunk"></i>sunk</span>
        <span class="muted">numbers are the order that side's shots were fired</span>
      </div>
    </div>`);
}

// One side's board: the layout it submitted, overlaid with the shots fired at it.
function boardBlock(g, owner, shooter) {
  const missing = !owner.layout.length;
  return `<div>
    <div class="muted"><strong>${esc(owner.name)}</strong>'s board — ${esc(shooter.name)}'s guesses
      ${owner.solved_round ? "" : `<span class="tag">${esc(owner.outcome || "")}</span>`}</div>
    ${missing ? '<div class="muted" style="padding:.5rem 0">no layout submitted</div>' : ""}
    ${miniBoard(g.rows, g.cols, owner.layout, g.fleet, shooter.shots)}</div>`;
}

// {"r,c": ship name} for every cell a layout occupies.
function shipCells(layout, fleet) {
  const size = new Map((fleet || []).map((s) => [s.name, s.size]));
  const cells = new Map();
  for (const p of layout || []) {
    for (let i = 0; i < (size.get(p.name) || 0); i++) {
      const r = p.orientation === "V" ? p.row + i : p.row;
      const c = p.orientation === "V" ? p.col : p.col + i;
      cells.set(r + "," + c, p.name);
    }
  }
  return cells;
}

function miniBoard(rows, cols, layout, fleet, shots) {
  const ships = shipCells(layout, fleet);
  const fired = new Map();
  (shots || []).forEach((s, i) => fired.set(s.row + "," + s.col, { n: i + 1, ...s }));
  let cells = "";
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const key = r + "," + c;
      const s = fired.get(key);
      const ship = ships.get(key);
      const cls = [ship ? "ship" : "", s ? s.result : ""].filter(Boolean).join(" ");
      const tip = `${key}${ship ? " " + ship : ""}${s ? ` — shot #${s.n}, round ${s.round}, ${s.result}` : ""}`;
      cells += `<div class="c ${cls}" title="${esc(tip)}">${s ? s.n : ""}</div>`;
    }
  }
  // Ships are drawn as an overlay so they read as hulls across cells rather than as a
  // block of tinted squares — one circle per tile, joined along the ship's axis.
  return `<div class="mini" style="--cols:${cols}">${cells}${shipMarks(rows, cols, layout, fleet)}</div>`;
}

// Circles on every tile of each ship, with a line through them. One SVG per board, in
// grid units (cell r,c centres on (c+0.5, r+0.5)), so it scales with the board.
function shipMarks(rows, cols, layout, fleet) {
  const size = new Map((fleet || []).map((s) => [s.name, s.size]));
  const marks = (layout || []).map((p) => {
    const n = size.get(p.name) || 0;
    if (!n) return "";
    const vert = p.orientation === "V";
    const x = p.col + 0.5, y = p.row + 0.5;
    let out = n > 1
      ? `<line x1="${x}" y1="${y}" x2="${vert ? x : x + n - 1}" y2="${vert ? y + n - 1 : y}"/>`
      : "";
    for (let i = 0; i < n; i++) {
      out += `<circle cx="${vert ? x : x + i}" cy="${vert ? y + i : y}" r=".3"/>`;
    }
    return out;
  }).join("");
  return marks ? `<svg class="ships" viewBox="0 0 ${cols} ${rows}">${marks}</svg>` : "";
}

const clock = (ts) => (ts == null ? "–" : new Date(ts * 1000).toLocaleTimeString());

function outcomeTag(outcome) {
  const cls = outcome === "win" ? "on" : "";
  return `<span class="tag ${cls}">${esc(outcome || "?")}</span>`;
}

async function renderCompare(aUuid, bUuid) {
  const box = el("compare");
  if (!aUuid || !bUuid) { paint(box, '<span class="muted">pick two bots above</span>'); return; }
  if (aUuid === bUuid) { paint(box, '<span class="muted">pick two different bots</span>'); return; }
  const d = await getJSON(`/api/compare?a=${aUuid}&b=${bUuid}`);
  if (!d.games) {
    paint(box, `<span class="muted">${esc(d.a.name)} and ${esc(d.b.name)} never played each other</span>`);
    return;
  }
  const badge = (sig, p) => sig
    ? `<span class="badge sig">significant · p ${p < 0.001 ? "< 0.001" : "= " + p.toFixed(3)}</span>`
    : `<span class="badge nsig">not significant · p = ${p.toFixed(2)}</span>`;
  const anySig = d.solve_delta_sig || d.win_rate_sig;
  const verdict = anySig
    ? `${esc(d.solve_leader)} is stronger`
    : `${esc(d.a.name)} vs ${esc(d.b.name)}: statistical tie`;
  paint(box, `
    <div class="row"><h2>${esc(d.a.name)} <span class="muted">${esc(d.a.player)}</span>
      &nbsp;vs&nbsp; ${esc(d.b.name)} <span class="muted">${esc(d.b.player)}</span></h2>
      <div class="spacer"></div><div class="muted">${d.games} head-to-head games</div></div>
    <div class="verdict ${anySig ? "ok" : "tie"}">${verdict}</div>
    <table class="cmp"><tbody>
      <tr><td class="l">win rate</td>
        <td>${esc(d.a.name)} <b>${pct(d.a_win_rate)}</b>
            <span class="muted">(${d.a_wins}-${d.ties}-${d.b_wins})</span></td>
        <td>${badge(d.win_rate_sig, d.win_rate_p)}</td></tr>
      <tr><td class="l">mean solve</td>
        <td>${esc(d.a.name)} ${one(d.a.solve_mean)}±${one(d.a.solve_sd)}
            &nbsp;·&nbsp; ${esc(d.b.name)} ${one(d.b.solve_mean)}±${one(d.b.solve_sd)}</td>
        <td></td></tr>
      <tr><td class="l">solve delta</td>
        <td><b>${esc(d.solve_leader)}</b> faster by ${Math.abs(d.solve_delta).toFixed(1)}
            turns <span class="muted">(±${one(d.solve_delta_se)})</span></td>
        <td>${badge(d.solve_delta_sig, d.solve_delta_p)}</td></tr>
    </tbody></table>
    <div class="grid2" style="margin-top:1rem">
      <div><div class="muted">${esc(d.a.name)} — solve turns</div>${histSVG(d.a.solve_hist, "")}</div>
      <div><div class="muted">${esc(d.b.name)} — solve turns</div>${histSVG(d.b.solve_hist, "b")}</div>
    </div>`);
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
  paint(el("pairings"), ids.length
    ? `<thead><tr><th class="l">win% (row vs col)</th>${head}</tr></thead><tbody>${body}</tbody>`
    : "<tbody><tr><td class='muted'>no games yet</td></tr></tbody>");
}
