"""Rankings and per-bot stats computed from the games table. Pure reads over a SQLite
connection. See DATA_CONTRACTS.md §6 for the metric definitions.

Each game contributes two perspectives (side a and side b); the UNION below flattens a
game into one row per participating bot so everything is a simple GROUP BY.
"""

import json
import math
import statistics
import time

from . import config as cfg
from . import game

# One row per (bot, game) with that bot's perspective of the result.
# A both-forfeit game has winner NULL (DATA_CONTRACTS.md §6), and `winner='a'` on NULL is
# NULL, not 0 — so COALESCE every flag to keep `win`/`tie` plain 0/1 ints. Neither side
# gets credit for such a game, which is the intended scoring.
_PERSPECTIVE_SQL = """
SELECT a_bot_uuid AS bot, b_bot_uuid AS opp,
       COALESCE(winner='a', 0) AS win, COALESCE(winner='tie', 0) AS tie,
       a_solved_round AS solver, b_solved_round AS opp_solver, a_outcome AS outcome,
       ended_at
FROM {src}
UNION ALL
SELECT b_bot_uuid, a_bot_uuid,
       COALESCE(winner='b', 0), COALESCE(winner='tie', 0),
       b_solved_round, a_solved_round, b_outcome,
       ended_at
FROM {src}
"""


def _perspectives(src="games"):
    """The per-(bot, game) view over `src` — the games table, or a subquery over it."""
    return _PERSPECTIVE_SQL.format(src=src)


_PERSPECTIVES = _perspectives()

# One bot's most recent `:window` games, from its own perspective — the leaderboard's
# scoring set, so a bot cannot coast on a record built against a field that has since
# improved. Each side is limited separately and the union re-limited, which lets the
# (bot, seq) indexes serve it as a bounded backward scan instead of a full-table sort.
# `seq` is insertion order, i.e. the order games finished.
_BOT_WINDOW = """
SELECT * FROM (
  SELECT seq, COALESCE(winner='a', 0) AS win, COALESCE(winner='tie', 0) AS tie,
         a_solved_round AS solver, b_solved_round AS opp_solver,
         a_outcome AS outcome, ended_at
  FROM games WHERE a_bot_uuid = :bot ORDER BY seq DESC {limit}
)
UNION ALL
SELECT * FROM (
  SELECT seq, COALESCE(winner='b', 0), COALESCE(winner='tie', 0),
         b_solved_round, a_solved_round, b_outcome, ended_at
  FROM games WHERE b_bot_uuid = :bot ORDER BY seq DESC {limit}
)
ORDER BY seq DESC {limit}
"""


def _bot_window(conn, bot_uuid, window):
    """Aggregate one bot's last `window` games (None = its whole record)."""
    limit = "LIMIT :window" if window else ""
    return conn.execute(f"""
        SELECT COUNT(*) AS games,
               SUM(win) AS wins,
               SUM(tie) AS ties,
               AVG(CASE WHEN solver IS NOT NULL THEN solver END) AS solver_avg,
               AVG(CASE WHEN opp_solver IS NOT NULL THEN opp_solver END) AS layout_avg,
               SUM(CASE WHEN outcome='forfeit' THEN 1 ELSE 0 END) AS forfeits,
               MAX(ended_at) AS last_played
        FROM ({_BOT_WINDOW.format(limit=limit)})
    """, {"bot": bot_uuid, "window": window}).fetchone()


def _games_played(conn, bot_uuid):
    """Every game this bot has played, window or no window — both index counts."""
    return conn.execute(
        "SELECT (SELECT COUNT(*) FROM games WHERE a_bot_uuid = :bot) "
        "     + (SELECT COUNT(*) FROM games WHERE b_bot_uuid = :bot)",
        {"bot": bot_uuid}).fetchone()[0]


def _percentiles(pairs, higher_better):
    """{bot: percentile in [0,1]} where 1 is best, from (bot, value) pairs."""
    if not pairs:
        return {}
    if len(pairs) == 1:
        return {pairs[0][0]: 1.0}
    ordered = sorted(pairs, key=lambda kv: kv[1], reverse=higher_better)
    n = len(ordered)
    return {bot: 1.0 - i / (n - 1) for i, (bot, _) in enumerate(ordered)}


def rankings(conn, active=frozenset(), window=cfg.LEADERBOARD_GAMES,
             idle_sec=cfg.LEADERBOARD_IDLE_SEC, now=None):
    """Per-bot leaderboard rows, best first.

    Each bot is scored over its own last `window` games (None = its whole record), so a
    bot that farmed a weak early field cannot hold a rank the current field says it
    hasn't earned, and a bot that joined late is judged on the same sample size as
    everyone else. A bot with no game in the last `idle_sec` is dropped from the board
    unless it still has a live session — it has left, and its stale numbers would sit
    above bots that are actually playing.
    """
    now = time.time() if now is None else now
    bots = []
    for m in conn.execute("SELECT uuid, player, name FROM bots").fetchall():
        b = dict(_bot_window(conn, m["uuid"], window))
        if not b["games"]:
            continue
        b["bot"] = m["uuid"]
        b["active"] = m["uuid"] in active
        idle = now - b["last_played"] if b["last_played"] is not None else None
        if not b["active"] and idle_sec and (idle is None or idle > idle_sec):
            continue
        b["player"] = m["player"]
        b["name"] = m["name"]
        b["win_rate"] = (b["wins"] + 0.5 * b["ties"]) / b["games"] if b["games"] else 0.0
        b["idle_sec"] = idle
        # `games` is the scoring window; `total_games` is everything the bot has played,
        # so the board can show both without the window looking like a stalled counter.
        b["total_games"] = _games_played(conn, m["uuid"])
        bots.append(b)

    # Combined = mean of the (up to three) per-competition percentile ranks.
    wr = _percentiles([(b["bot"], b["win_rate"]) for b in bots], True)
    sv = _percentiles([(b["bot"], b["solver_avg"]) for b in bots if b["solver_avg"] is not None], False)
    ly = _percentiles([(b["bot"], b["layout_avg"]) for b in bots if b["layout_avg"] is not None], True)
    for b in bots:
        parts = [wr[b["bot"]]]
        if b["bot"] in sv:
            parts.append(sv[b["bot"]])
        if b["bot"] in ly:
            parts.append(ly[b["bot"]])
        b["combined"] = sum(parts) / len(parts)
        b["bot_uuid"] = b.pop("bot")

    # The leaderboard ranks on win rate. `combined` is still reported (the UI shows it as
    # a bar) but no longer decides the order. Ties break on games played — a bot that held
    # a win rate over more games has the better-evidenced one — then on solver speed.
    bots.sort(key=lambda b: (b["win_rate"], b["games"],
                             -(b["solver_avg"] if b["solver_avg"] is not None else 1e9)),
              reverse=True)
    for i, b in enumerate(bots):
        b["rank"] = i + 1
    return bots


# One bot's most recent games, optionally only those against `:vs`, tagged with the seat
# it took so its own layout and its own shots can be picked out.
_SAMPLE = """
SELECT * FROM (
  SELECT seq, uuid, tourney_uuid, 'a' AS side, a_layout_json AS layout FROM games
  WHERE a_bot_uuid = :bot {a_vs} ORDER BY seq DESC LIMIT :games
)
UNION ALL
SELECT * FROM (
  SELECT seq, uuid, tourney_uuid, 'b', b_layout_json FROM games
  WHERE b_bot_uuid = :bot {b_vs} ORDER BY seq DESC LIMIT :games
)
ORDER BY seq DESC LIMIT :games
"""


def _grid(rows, cols):
    return [[0] * cols for _ in range(rows)]


def heatmap(conn, bot_uuid, vs=None, games=cfg.HEATMAP_GAMES):
    """Where a bot puts its ships and where it aims, over its last `games` games.

    `vs` restricts the sample to games against that one opponent, which is what the
    head-to-head view wants. Counts are per cell: `ships` is how many of those games had
    a hull on that cell, `shots` is how many shots the bot fired at it.
    """
    sample = _SAMPLE.format(a_vs="AND b_bot_uuid = :vs" if vs else "",
                            b_vs="AND a_bot_uuid = :vs" if vs else "")
    params = {"bot": bot_uuid, "games": games, **({"vs": vs} if vs else {})}
    sampled = conn.execute(sample, params).fetchall()

    # Board size and fleet come from the tourney each game was played in.
    configs = {}
    for r in sampled:
        if r["tourney_uuid"] not in configs:
            row = conn.execute("SELECT config_json FROM tourneys WHERE uuid=?",
                               (r["tourney_uuid"],)).fetchone()
            try:
                configs[r["tourney_uuid"]] = json.loads(row["config_json"]) if row else {}
            except (TypeError, ValueError):
                configs[r["tourney_uuid"]] = {}
    latest = configs.get(sampled[0]["tourney_uuid"], {}) if sampled else {}
    rows, cols = latest.get("rows", 10), latest.get("cols", 10)

    ships = _grid(rows, cols)
    for r in sampled:
        sizes = cfg.fleet_sizes(configs.get(r["tourney_uuid"], {}).get("fleet", cfg.FLEET))
        for placement in _json_list(r["layout"]):
            for cell in game.placement_cells(placement, sizes) or ():
                cr, cc = cell
                if 0 <= cr < rows and 0 <= cc < cols:
                    ships[cr][cc] += 1

    shots = _grid(rows, cols)
    for r in conn.execute(f"""
        SELECT m.row AS row, m.col AS col, COUNT(*) AS n
        FROM moves m JOIN ({sample}) s ON m.game_uuid = s.uuid AND m.side = s.side
        GROUP BY m.row, m.col
    """, params):
        if 0 <= r["row"] < rows and 0 <= r["col"] < cols:
            shots[r["row"]][r["col"]] = r["n"]

    return {
        "bot_uuid": bot_uuid,
        "vs": vs,
        "games": len(sampled),
        "rows": rows,
        "cols": cols,
        "ships": ships,
        "shots": shots,
        "total_shots": sum(sum(row) for row in shots),
    }


def _histogram(values):
    """{round: count} over non-null values."""
    hist = {}
    for v in values:
        if v is not None:
            hist[v] = hist.get(v, 0) + 1
    return hist


def bot_detail(conn, bot_uuid):
    """Distributions and per-opponent breakdown for one bot."""
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM ({_PERSPECTIVES}) WHERE bot=?", (bot_uuid,)).fetchall()]
    meta = {r["uuid"]: dict(r) for r in conn.execute("SELECT uuid, player, name FROM bots")}
    me = meta.get(bot_uuid, {})

    by_opp = {}
    for r in rows:
        o = by_opp.setdefault(r["opp"], {"wins": 0, "ties": 0, "games": 0,
                                         "solver": [], "layout": []})
        o["games"] += 1
        o["wins"] += r["win"]
        o["ties"] += r["tie"]
        if r["solver"] is not None:
            o["solver"].append(r["solver"])
        if r["opp_solver"] is not None:
            o["layout"].append(r["opp_solver"])

    opponents = []
    for opp_uuid, o in by_opp.items():
        om = meta.get(opp_uuid, {})
        opponents.append({
            "bot_uuid": opp_uuid,
            "player": om.get("player", "?"),
            "name": om.get("name", "?"),
            "games": o["games"],
            "win_rate": (o["wins"] + 0.5 * o["ties"]) / o["games"] if o["games"] else 0.0,
            "solver_avg": sum(o["solver"]) / len(o["solver"]) if o["solver"] else None,
            "layout_avg": sum(o["layout"]) / len(o["layout"]) if o["layout"] else None,
        })
    opponents.sort(key=lambda x: (-x["games"], x["player"], x["name"]))

    scores = [1.0 if r["win"] else (0.5 if r["tie"] else 0.0) for r in rows]
    solver_vals = [r["solver"] for r in rows if r["solver"] is not None]
    layout_vals = [r["opp_solver"] for r in rows if r["opp_solver"] is not None]
    return {
        "bot_uuid": bot_uuid,
        "player": me.get("player", "?"),
        "name": me.get("name", "?"),
        "games": len(rows),
        "wins": sum(1 for r in rows if r["win"]),
        "ties": sum(1 for r in rows if r["tie"]),
        "win_rate": statistics.mean(scores) if scores else 0.0,
        "solver_avg": statistics.mean(solver_vals) if solver_vals else None,
        "layout_avg": statistics.mean(layout_vals) if layout_vals else None,
        "solver_hist": _histogram([r["solver"] for r in rows]),
        "layout_hist": _histogram([r["opp_solver"] for r in rows]),
        "opponents": opponents,
    }


def recent_games(conn, bot_uuid, limit=10):
    """The bot's most recently finished games, newest first, from its own perspective.
    Summary only — the boards and shot order come from `game_replay`."""
    rows = conn.execute("""
        SELECT uuid, a_bot_uuid, b_bot_uuid, winner, a_outcome, b_outcome,
               a_solved_round, b_solved_round, end_reason, total_rounds, ended_at
        FROM games WHERE a_bot_uuid=? OR b_bot_uuid=? ORDER BY seq DESC LIMIT ?
    """, (bot_uuid, bot_uuid, limit)).fetchall()
    meta = {r["uuid"]: dict(r) for r in conn.execute("SELECT uuid, player, name FROM bots")}

    out = []
    for r in rows:
        me, opp = ("a", "b") if r["a_bot_uuid"] == bot_uuid else ("b", "a")
        om = meta.get(r[f"{opp}_bot_uuid"], {})
        out.append({
            "game_uuid": r["uuid"],
            "opponent": {"bot_uuid": r[f"{opp}_bot_uuid"],
                         "name": om.get("name", "?"), "player": om.get("player", "?")},
            "outcome": r[f"{me}_outcome"],
            "solved_round": r[f"{me}_solved_round"],
            "opponent_solved_round": r[f"{opp}_solved_round"],
            "end_reason": r["end_reason"],
            "total_rounds": r["total_rounds"],
            "ended_at": r["ended_at"],
        })
    return out


def game_replay(conn, game_uuid, bot_uuid=None):
    """One game as two symmetric sides: each side's submitted layout plus the shots it
    fired, in order. `bot_uuid` (if given and playing) is put on the 'you' side so the
    single-bot page can render it without knowing which seat the bot took."""
    g = conn.execute("""
        SELECT g.*, t.config_json FROM games g
        JOIN tourneys t ON t.uuid = g.tourney_uuid WHERE g.uuid=?
    """, (game_uuid,)).fetchone()
    if g is None:
        return None
    config = json.loads(g["config_json"] or "{}")
    meta = {r["uuid"]: dict(r) for r in conn.execute("SELECT uuid, player, name FROM bots")}

    moves = conn.execute(
        "SELECT round, side, row, col, result, sunk_ship FROM moves "
        "WHERE game_uuid=? ORDER BY round, side", (game_uuid,)).fetchall()
    shots = {"a": [], "b": []}
    for m in moves:
        shots[m["side"]].append({"round": m["round"], "row": m["row"], "col": m["col"],
                                 "result": m["result"], "sunk_ship": m["sunk_ship"]})

    def side(s):
        bm = meta.get(g[f"{s}_bot_uuid"], {})
        return {
            "side": s,
            "bot_uuid": g[f"{s}_bot_uuid"],
            "name": bm.get("name", "?"),
            "player": bm.get("player", "?"),
            # The layout this bot submitted; NULL/invalid ones are why a game can end
            # 'illegal' before a shot is fired, so pass the parse failure through as [].
            "layout": _json_list(g[f"{s}_layout_json"]),
            "shots": shots[s],          # what this bot fired at the opponent's board
            "outcome": g[f"{s}_outcome"],
            "solved_round": g[f"{s}_solved_round"],
        }

    you, opp = ("a", "b") if bot_uuid is None or g["a_bot_uuid"] == bot_uuid else ("b", "a")
    return {
        "game_uuid": game_uuid,
        "rows": config.get("rows", 10),
        "cols": config.get("cols", 10),
        "fleet": config.get("fleet", []),
        "winner": g["winner"],
        "end_reason": g["end_reason"],
        "total_rounds": g["total_rounds"],
        "ended_at": g["ended_at"],
        "you": side(you),
        "opponent": side(opp),
    }


def _json_list(raw):
    """Parse a stored JSON list, tolerating NULL / 'null' / malformed values."""
    try:
        value = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _sig(z):
    """Two-sided p-value for a standard-normal z, and whether |z| clears 1.96 (p<0.05)."""
    return math.erfc(abs(z) / math.sqrt(2)), abs(z) > 1.96


def head_to_head(conn, a_uuid, b_uuid):
    """Direct comparison of two bots over only their shared games, with significance on
    both win rate (vs 50%) and mean solve time (delta > 0 means A is the faster solver)."""
    meta = {r["uuid"]: dict(r) for r in conn.execute("SELECT uuid, player, name FROM bots")}

    def side(uuid):
        m = meta.get(uuid, {})
        return {"bot_uuid": uuid, "name": m.get("name", "?"), "player": m.get("player", "?")}

    a, b = side(a_uuid), side(b_uuid)
    rows = conn.execute(
        f"SELECT * FROM ({_PERSPECTIVES}) WHERE bot=? AND opp=?", (a_uuid, b_uuid)).fetchall()
    n = len(rows)
    if n == 0:
        return {"a": a, "b": b, "games": 0}

    scores = [1.0 if r["win"] else (0.5 if r["tie"] else 0.0) for r in rows]
    a_solve = [r["solver"] for r in rows if r["solver"] is not None]
    b_solve = [r["opp_solver"] for r in rows if r["opp_solver"] is not None]

    win_rate = statistics.mean(scores)
    se_wr = statistics.pstdev(scores) / math.sqrt(n) if n > 1 else 0.0
    z_wr = (win_rate - 0.5) / se_wr if se_wr else 0.0
    p_wr, sig_wr = _sig(z_wr)

    ma = statistics.mean(a_solve) if a_solve else float("nan")
    mb = statistics.mean(b_solve) if b_solve else float("nan")
    delta = mb - ma  # A's turn advantage; >0 => A solves faster
    se_d = (math.sqrt(statistics.pvariance(a_solve) / len(a_solve)
                      + statistics.pvariance(b_solve) / len(b_solve))
            if len(a_solve) > 1 and len(b_solve) > 1 else 0.0)
    z_d = delta / se_d if se_d else 0.0
    p_d, sig_d = _sig(z_d)

    a.update({"solve_mean": ma, "solve_sd": statistics.pstdev(a_solve) if len(a_solve) > 1 else 0.0,
              "solve_hist": _histogram(a_solve)})
    b.update({"solve_mean": mb, "solve_sd": statistics.pstdev(b_solve) if len(b_solve) > 1 else 0.0,
              "solve_hist": _histogram(b_solve)})
    return {
        "a": a, "b": b, "games": n,
        "a_wins": sum(1 for r in rows if r["win"]),
        "b_wins": sum(1 for r in rows if not r["win"] and not r["tie"]),
        "ties": sum(1 for r in rows if r["tie"]),
        "a_win_rate": win_rate,
        "win_rate_z": z_wr, "win_rate_p": p_wr, "win_rate_sig": sig_wr,
        "solve_delta": delta, "solve_delta_se": se_d, "solve_delta_z": z_d,
        "solve_delta_p": p_d, "solve_delta_sig": sig_d,
        "win_leader": a["name"] if win_rate > 0.5 else b["name"],
        "solve_leader": a["name"] if delta > 0 else b["name"],
    }


def pairings(conn):
    """Symmetric per-pair summary (each unordered bot pair once)."""
    rows = conn.execute("""
        SELECT a_bot_uuid AS a, b_bot_uuid AS b, COUNT(*) AS games,
               SUM(winner='a') AS a_wins, SUM(winner='b') AS b_wins, SUM(winner='tie') AS ties,
               AVG(a_solved_round) AS a_solver, AVG(b_solved_round) AS b_solver
        FROM games GROUP BY a_bot_uuid, b_bot_uuid
    """).fetchall()

    merged = {}
    for r in rows:
        key = tuple(sorted((r["a"], r["b"])))
        m = merged.setdefault(key, {"bot1": key[0], "bot2": key[1], "games": 0,
                                    "bot1_wins": 0, "bot2_wins": 0, "ties": 0})
        # Orient this directed row onto (bot1, bot2).
        if r["a"] == key[0]:
            m["bot1_wins"] += r["a_wins"] or 0
            m["bot2_wins"] += r["b_wins"] or 0
        else:
            m["bot1_wins"] += r["b_wins"] or 0
            m["bot2_wins"] += r["a_wins"] or 0
        m["ties"] += r["ties"] or 0
        m["games"] += r["games"]
    return list(merged.values())
