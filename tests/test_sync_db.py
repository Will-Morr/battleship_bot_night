"""Tests for the sync client's merge logic: a mirror built from /sync payloads matches
the source, and re-applying is idempotent (append-only tables dedupe on seq)."""

from battleship import db, sync_db
from battleship.db import Database


def seed(d, player, name="b"):
    s = db.new_uuid()
    bot = d.register_session(player=player, bot_name=name, session_uuid=s, code="x",
                             code_filename="b.py", code_hash="h", runner_version="1", now=1.0)
    return bot, s


def game(t, a, b):
    (ab, asess), (bb, bsess) = a, b
    return {
        "uuid": db.new_uuid(), "tourney_uuid": t, "a_bot_uuid": ab, "b_bot_uuid": bb,
        "a_session_uuid": asess, "b_session_uuid": bsess, "a_layout": [], "b_layout": [],
        "a_solved_round": 30, "b_solved_round": 40, "winner": "a", "a_outcome": "win",
        "b_outcome": "loss", "end_reason": "complete", "total_rounds": 40,
        "started_at": 1.0, "ended_at": 2.0,
        "moves": [{"round": 1, "side": "a", "row": 0, "col": 0, "result": "hit",
                   "sunk_ship": None, "response_ms": 1.0}],
    }


def build_source(tmp_path):
    src = Database(tmp_path / "src.db")
    a, b = seed(src, "alice"), seed(src, "bob")
    t = db.new_uuid()
    src.start_tourney(t, {}, 1.0)
    src.record_games([game(t, a, b), game(t, a, b)])
    return src


def test_mirror_matches_source(tmp_path):
    src = build_source(tmp_path)
    mirror = Database(tmp_path / "mirror.db")
    mirror.conn.execute("PRAGMA foreign_keys=OFF")

    sync_db.apply_payload(mirror, src.sync_since())

    for table in ("tourneys", "bots", "bot_sessions", "games", "moves"):
        s = src.query(f"SELECT COUNT(*) c FROM {table}")[0]["c"]
        m = mirror.query(f"SELECT COUNT(*) c FROM {table}")[0]["c"]
        assert s == m == mirror.query(f"SELECT COUNT(*) c FROM {table}")[0]["c"], table
    assert mirror.query("SELECT COUNT(*) c FROM games")[0]["c"] == 2


def test_incremental_and_idempotent(tmp_path):
    src = build_source(tmp_path)
    mirror = Database(tmp_path / "mirror.db")
    mirror.conn.execute("PRAGMA foreign_keys=OFF")

    # Cursor helper reads MAX(seq); first pull grabs everything.
    gc = sync_db._cursor(mirror.conn, "games")
    assert gc == 0
    sync_db.apply_payload(mirror, src.sync_since(gc, sync_db._cursor(mirror.conn, "moves")))
    assert mirror.query("SELECT COUNT(*) c FROM games")[0]["c"] == 2

    # Re-applying the same payload changes nothing (seq dedupe).
    sync_db.apply_payload(mirror, src.sync_since())
    assert mirror.query("SELECT COUNT(*) c FROM games")[0]["c"] == 2

    # A new game past the cursor appends exactly one.
    a = (src.query("SELECT a_bot_uuid u FROM games LIMIT 1")[0]["u"],
         src.query("SELECT a_session_uuid u FROM games LIMIT 1")[0]["u"])
    b = (src.query("SELECT b_bot_uuid u FROM games LIMIT 1")[0]["u"],
         src.query("SELECT b_session_uuid u FROM games LIMIT 1")[0]["u"])
    t = src.query("SELECT tourney_uuid u FROM games LIMIT 1")[0]["u"]
    src.record_games([game(t, a, b)])
    gc = sync_db._cursor(mirror.conn, "games")
    mc = sync_db._cursor(mirror.conn, "moves")
    added = src.sync_since(gc, mc)
    assert len(added["games"]) == 1
    sync_db.apply_payload(mirror, added)
    assert mirror.query("SELECT COUNT(*) c FROM games")[0]["c"] == 3
