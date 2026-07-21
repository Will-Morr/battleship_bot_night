"""Tests for battleship.db — write round-trips, session upsert, response-time
aggregation, incremental sync cursors, and consistent snapshots."""

from battleship import db
from battleship.db import Database


def make_db(tmp_path):
    return Database(tmp_path / "t.db")


def seed_bot(d, player="will", name="greedy", now=100.0):
    session = db.new_uuid()
    bot_uuid = d.register_session(
        player=player, bot_name=name, session_uuid=session, code="print(1)",
        code_filename="b.py", code_hash="abc", runner_version="1", now=now)
    return bot_uuid, session


def make_game(tourney, a_bot, b_bot, a_sess, b_sess, moves):
    return {
        "uuid": db.new_uuid(), "tourney_uuid": tourney,
        "a_bot_uuid": a_bot, "b_bot_uuid": b_bot,
        "a_session_uuid": a_sess, "b_session_uuid": b_sess,
        "a_layout": [{"name": "destroyer", "row": 0, "col": 0, "orientation": "H"}],
        "b_layout": [{"name": "destroyer", "row": 2, "col": 0, "orientation": "H"}],
        "a_solved_round": 40, "b_solved_round": 55, "winner": "a",
        "a_outcome": "win", "b_outcome": "loss", "end_reason": "complete",
        "total_rounds": 55, "started_at": 1.0, "ended_at": 2.0, "moves": moves,
    }


def test_register_session_upserts_bot(tmp_path):
    d = make_db(tmp_path)
    b1, s1 = seed_bot(d, now=100.0)
    b2, s2 = seed_bot(d, now=200.0)              # same (player, name) reconnecting
    assert b1 == b2 and s1 != s2                 # one logical bot, two sessions
    sessions = d.query("SELECT * FROM bot_sessions WHERE bot_uuid=?", (b1,))
    assert len(sessions) == 2
    b3, _ = seed_bot(d, name="other")            # different bot name -> new logical bot
    assert b3 != b1


def test_record_game_and_moves(tmp_path):
    d = make_db(tmp_path)
    a_bot, a_sess = seed_bot(d, player="alice", name="x")
    b_bot, b_sess = seed_bot(d, player="bob", name="y")
    t = db.new_uuid()
    d.start_tourney(t, {"rows": 10}, now=1.0)
    d.mark_tourney_bots(t, [a_bot, b_bot])
    moves = [
        {"round": 1, "side": "a", "row": 0, "col": 0, "result": "hit", "sunk_ship": None, "response_ms": 1.0},
        {"round": 1, "side": "b", "row": 5, "col": 5, "result": "miss", "sunk_ship": None, "response_ms": None},
        {"round": 2, "side": "a", "row": 0, "col": 1, "result": "sunk", "sunk_ship": "destroyer", "response_ms": 3.0},
    ]
    g = make_game(t, a_bot, b_bot, a_sess, b_sess, moves)
    d.record_games([g])

    rows = d.query("SELECT * FROM games")
    assert len(rows) == 1
    game = rows[0]
    assert game["winner"] == "a" and game["end_reason"] == "complete"
    # response aggregates computed from side 'a' moves (1.0, 3.0); side 'b' has none.
    assert game["a_resp_min_ms"] == 1.0 and game["a_resp_max_ms"] == 3.0 and game["a_resp_avg_ms"] == 2.0
    assert game["b_resp_min_ms"] is None
    assert d.query("SELECT COUNT(*) c FROM moves")[0]["c"] == 3
    # first/last tourney recorded on the bots.
    bot = d.query("SELECT * FROM bots WHERE uuid=?", (a_bot,))[0]
    assert bot["first_tourney_uuid"] == t and bot["last_tourney_uuid"] == t

    d.finish_tourney(t, num_games=1, now=9.0)
    tour = d.query("SELECT * FROM tourneys")[0]
    assert tour["status"] == "complete" and tour["num_games"] == 1


def test_incremental_sync(tmp_path):
    d = make_db(tmp_path)
    a_bot, a_sess = seed_bot(d, player="a", name="a")
    b_bot, b_sess = seed_bot(d, player="b", name="b")
    t = db.new_uuid()
    d.start_tourney(t, {}, now=1.0)
    move = [{"round": 1, "side": "a", "row": 0, "col": 0, "result": "hit", "sunk_ship": None, "response_ms": 1.0}]
    d.record_games([make_game(t, a_bot, b_bot, a_sess, b_sess, move)])

    first = d.sync_since()
    assert len(first["games"]) == 1 and len(first["moves"]) == 1
    assert len(first["bots"]) == 2 and len(first["tourneys"]) == 1
    cur = first["cursors"]

    # Nothing new -> empty append tables, cursors unchanged.
    again = d.sync_since(cur["games"], cur["moves"])
    assert again["games"] == [] and again["moves"] == []
    assert again["cursors"] == cur

    # A second game shows up only past the cursor.
    d.record_games([make_game(t, a_bot, b_bot, a_sess, b_sess, move)])
    third = d.sync_since(cur["games"], cur["moves"])
    assert len(third["games"]) == 1 and len(third["moves"]) == 1
    assert third["cursors"]["games"] > cur["games"]


def test_snapshot_is_readable(tmp_path):
    d = make_db(tmp_path)
    a_bot, a_sess = seed_bot(d, player="a", name="a")
    b_bot, b_sess = seed_bot(d, player="b", name="b")
    t = db.new_uuid()
    d.start_tourney(t, {}, now=1.0)
    move = [{"round": 1, "side": "a", "row": 0, "col": 0, "result": "hit", "sunk_ship": None, "response_ms": 1.0}]
    d.record_games([make_game(t, a_bot, b_bot, a_sess, b_sess, move)])

    snap = tmp_path / "snap.db"
    d.snapshot(snap)
    copy = Database(snap)
    assert copy.query("SELECT COUNT(*) c FROM games")[0]["c"] == 1
    assert copy.query("SELECT COUNT(*) c FROM moves")[0]["c"] == 1


def test_persistence_across_reopen(tmp_path):
    path = tmp_path / "p.db"
    d = Database(path)
    seed_bot(d)
    d.close()
    again = Database(path)
    assert again.query("SELECT COUNT(*) c FROM bots")[0]["c"] == 1
