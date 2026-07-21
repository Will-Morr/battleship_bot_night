"""Rankings and per-bot stats computed from the games table. Pure reads over a SQLite
connection. See DATA_CONTRACTS.md §6 for the metric definitions.

Each game contributes two perspectives (side a and side b); the UNION below flattens a
game into one row per participating bot so everything is a simple GROUP BY.
"""

# One row per (bot, game) with that bot's perspective of the result.
_PERSPECTIVES = """
SELECT a_bot_uuid AS bot, b_bot_uuid AS opp,
       (winner='a') AS win, (winner='tie') AS tie,
       a_solved_round AS solver, b_solved_round AS opp_solver, a_outcome AS outcome
FROM games
UNION ALL
SELECT b_bot_uuid, a_bot_uuid,
       (winner='b'), (winner='tie'),
       b_solved_round, a_solved_round, b_outcome
FROM games
"""


def _percentiles(pairs, higher_better):
    """{bot: percentile in [0,1]} where 1 is best, from (bot, value) pairs."""
    if not pairs:
        return {}
    if len(pairs) == 1:
        return {pairs[0][0]: 1.0}
    ordered = sorted(pairs, key=lambda kv: kv[1], reverse=higher_better)
    n = len(ordered)
    return {bot: 1.0 - i / (n - 1) for i, (bot, _) in enumerate(ordered)}


def rankings(conn, active=frozenset()):
    """Per-bot leaderboard rows, sorted by combined score (best first)."""
    rows = conn.execute(f"""
        SELECT bot,
               COUNT(*) AS games,
               SUM(win) AS wins,
               SUM(tie) AS ties,
               AVG(CASE WHEN solver IS NOT NULL THEN solver END) AS solver_avg,
               AVG(CASE WHEN opp_solver IS NOT NULL THEN opp_solver END) AS layout_avg,
               SUM(CASE WHEN outcome='forfeit' THEN 1 ELSE 0 END) AS forfeits
        FROM ({_PERSPECTIVES}) GROUP BY bot
    """).fetchall()
    meta = {r["uuid"]: r for r in conn.execute("SELECT uuid, player, name FROM bots")}

    bots = []
    for r in rows:
        b = dict(r)
        m = meta.get(b["bot"])
        b["player"] = m["player"] if m else "?"
        b["name"] = m["name"] if m else "?"
        b["win_rate"] = (b["wins"] + 0.5 * b["ties"]) / b["games"] if b["games"] else 0.0
        b["active"] = b["bot"] in active
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

    bots.sort(key=lambda b: b["combined"], reverse=True)
    for i, b in enumerate(bots):
        b["rank"] = i + 1
    return bots


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

    return {
        "bot_uuid": bot_uuid,
        "player": me.get("player", "?"),
        "name": me.get("name", "?"),
        "games": len(rows),
        "solver_hist": _histogram([r["solver"] for r in rows]),
        "layout_hist": _histogram([r["opp_solver"] for r in rows]),
        "opponents": opponents,
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
