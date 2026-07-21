"""Tests for scoring — rankings metrics, per-bot detail, and pairing merges — over a
DB seeded with games of known outcomes."""

from battleship import db, scoring
from battleship.db import Database


def reg(d, player):
    session = db.new_uuid()
    bot = d.register_session(player=player, bot_name="b", session_uuid=session,
                             code="x", code_filename="b.py", code_hash="h",
                             runner_version="1", now=1.0)
    return bot, session


def game(t, a, b, *, winner, ao, bo, asr, bsr, end="complete"):
    (ab, asess), (bb, bsess) = a, b
    return {
        "uuid": db.new_uuid(), "tourney_uuid": t,
        "a_bot_uuid": ab, "b_bot_uuid": bb, "a_session_uuid": asess, "b_session_uuid": bsess,
        "a_layout": [], "b_layout": [], "a_solved_round": asr, "b_solved_round": bsr,
        "winner": winner, "a_outcome": ao, "b_outcome": bo, "end_reason": end,
        "total_rounds": max(x for x in (asr, bsr, 0) if x is not None),
        "started_at": 1.0, "ended_at": 2.0, "moves": [],
    }


def seeded(tmp_path):
    d = Database(tmp_path / "s.db")
    p1, p2, p3 = reg(d, "p1"), reg(d, "p2"), reg(d, "p3")
    t = db.new_uuid()
    d.start_tourney(t, {}, 1.0)
    d.record_games([
        game(t, p1, p2, winner="a", ao="win", bo="loss", asr=30, bsr=40),
        game(t, p1, p3, winner="a", ao="win", bo="forfeit", asr=50, bsr=None, end="b_illegal"),
        game(t, p2, p3, winner="tie", ao="tie", bo="tie", asr=45, bsr=45),
    ])
    return d, {"p1": p1[0], "p2": p2[0], "p3": p3[0]}


def test_rankings_metrics(tmp_path):
    d, ids = seeded(tmp_path)
    board = {b["bot_uuid"]: b for b in scoring.rankings(d.conn)}

    p1 = board[ids["p1"]]
    assert p1["games"] == 2 and p1["wins"] == 2
    assert p1["win_rate"] == 1.0
    assert p1["solver_avg"] == 40.0        # (30 + 50) / 2
    assert p1["layout_avg"] == 40.0        # opponent solved p1 only in g1, at 40
    assert p1["rank"] == 1                  # best combined

    p2 = board[ids["p2"]]
    assert p2["win_rate"] == 0.25          # 0 wins, 1 tie, 2 games
    assert p2["solver_avg"] == 42.5        # (40 + 45) / 2

    p3 = board[ids["p3"]]
    assert p3["forfeits"] == 1
    assert p3["solver_avg"] == 45.0        # only the tie game; the forfeit is a DNF


def test_pairings_merge(tmp_path):
    d, ids = seeded(tmp_path)
    pairs = scoring.pairings(d.conn)
    assert len(pairs) == 3
    by_key = {tuple(sorted((p["bot1"], p["bot2"]))): p for p in pairs}
    p1p2 = by_key[tuple(sorted((ids["p1"], ids["p2"])))]
    assert p1p2["games"] == 1
    # p1 beat p2, oriented onto whichever slot p1 landed in.
    winner_slot = "bot1_wins" if p1p2["bot1"] == ids["p1"] else "bot2_wins"
    assert p1p2[winner_slot] == 1


def test_bot_detail(tmp_path):
    d, ids = seeded(tmp_path)
    detail = scoring.bot_detail(d.conn, ids["p1"])
    assert detail["games"] == 2
    assert detail["solver_hist"] == {30: 1, 50: 1}
    opp_ids = {o["bot_uuid"] for o in detail["opponents"]}
    assert opp_ids == {ids["p2"], ids["p3"]}
