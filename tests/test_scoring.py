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


def test_recent_games_and_replay(tmp_path):
    d = Database(tmp_path / "s.db")
    p1, p2 = reg(d, "p1"), reg(d, "p2")
    t = db.new_uuid()
    d.start_tourney(t, {"rows": 10, "cols": 10,
                        "fleet": [{"name": "destroyer", "size": 2}]}, 1.0)
    g1 = game(t, p1, p2, winner="a", ao="win", bo="loss", asr=3, bsr=4)
    g2 = game(t, p2, p1, winner="a", ao="win", bo="loss", asr=5, bsr=6)
    g2["a_layout"] = [{"name": "destroyer", "row": 0, "col": 0, "orientation": "H"}]
    g2["b_layout"] = [{"name": "destroyer", "row": 9, "col": 8, "orientation": "H"}]
    g2["moves"] = [
        {"round": 1, "side": "a", "row": 9, "col": 8, "result": "hit",
         "sunk_ship": None, "response_ms": 1.0},
        {"round": 1, "side": "b", "row": 5, "col": 5, "result": "miss",
         "sunk_ship": None, "response_ms": 1.0},
        {"round": 2, "side": "a", "row": 9, "col": 9, "result": "sunk",
         "sunk_ship": "destroyer", "response_ms": 1.0},
    ]
    d.record_games([g1, g2])

    recent = scoring.recent_games(d.conn, p1[0])
    assert [r["game_uuid"] for r in recent] == [g2["uuid"], g1["uuid"]]   # newest first
    assert recent[0]["opponent"]["bot_uuid"] == p2[0]      # p1 sat on side b in g2
    assert recent[0]["outcome"] == "loss"
    assert recent[0]["solved_round"] == 6 and recent[0]["opponent_solved_round"] == 5

    # `you` follows the requested bot into whichever seat it took.
    r = scoring.game_replay(d.conn, g2["uuid"], p1[0])
    assert r["you"]["side"] == "b" and r["you"]["bot_uuid"] == p1[0]
    assert r["opponent"]["side"] == "a"
    assert r["you"]["layout"][0]["row"] == 9               # b's own ships
    # `shots` are what that side fired, in order, so a board is drawn from its owner's
    # layout plus the other side's shots.
    assert [(s["row"], s["col"]) for s in r["opponent"]["shots"]] == [(9, 8), (9, 9)]
    assert [s["result"] for s in r["opponent"]["shots"]] == ["hit", "sunk"]
    assert [(s["row"], s["col"]) for s in r["you"]["shots"]] == [(5, 5)]
    assert r["rows"] == 10 and r["cols"] == 10 and r["fleet"][0]["size"] == 2

    # Unknown game, and a game with no layout, degrade instead of raising.
    assert scoring.game_replay(d.conn, "nope") is None
    plain = scoring.game_replay(d.conn, g1["uuid"])
    assert plain["you"]["side"] == "a" and plain["you"]["layout"] == []


def test_both_forfeit_game_scores_without_credit(tmp_path):
    # A game both sides forfeited stores winner NULL, and `winner='a'` is NULL (not 0)
    # in SQL — leaving `win`/`tie` as None and blowing up per-bot detail (a 500 on
    # /api/bot/<uuid>, i.e. a blank single-bot panel). Neither side gets credit.
    d = Database(tmp_path / "s.db")
    p1, p2 = reg(d, "p1"), reg(d, "p2")
    t = db.new_uuid()
    d.start_tourney(t, {}, 1.0)
    d.record_games([
        game(t, p1, p2, winner="a", ao="win", bo="loss", asr=30, bsr=40),
        game(t, p1, p2, winner=None, ao="forfeit", bo="forfeit", asr=None, bsr=None,
             end="both_forfeit"),
    ])

    detail = scoring.bot_detail(d.conn, p1[0])
    assert detail["games"] == 2 and detail["wins"] == 1 and detail["ties"] == 0
    assert detail["win_rate"] == 0.5
    assert detail["opponents"][0]["games"] == 2
    assert detail["opponents"][0]["win_rate"] == 0.5

    board = {b["bot_uuid"]: b for b in scoring.rankings(d.conn, window=None, idle_sec=0)}
    assert board[p1[0]]["wins"] == 1 and board[p1[0]]["win_rate"] == 0.5
    assert board[p2[0]]["wins"] == 0 and board[p2[0]]["win_rate"] == 0.0

    h2h = scoring.head_to_head(d.conn, p1[0], p2[0])
    assert h2h["games"] == 2 and h2h["a_wins"] == 1 and h2h["ties"] == 0


def test_rankings_metrics(tmp_path):
    d, ids = seeded(tmp_path)
    board = {b["bot_uuid"]: b for b in scoring.rankings(d.conn, window=None, idle_sec=0)}

    p1 = board[ids["p1"]]
    assert p1["games"] == 2 and p1["wins"] == 2
    assert p1["win_rate"] == 1.0
    assert p1["solver_avg"] == 40.0        # (30 + 50) / 2
    assert p1["layout_avg"] == 40.0        # opponent solved p1 only in g1, at 40
    assert p1["rank"] == 1                  # highest win rate

    p2 = board[ids["p2"]]
    assert p2["win_rate"] == 0.25          # 0 wins, 1 tie, 2 games
    assert p2["solver_avg"] == 42.5        # (40 + 45) / 2

    p3 = board[ids["p3"]]
    assert p3["forfeits"] == 1
    assert p3["solver_avg"] == 45.0        # only the tie game; the forfeit is a DNF


def test_rankings_order_by_win_rate(tmp_path):
    # The board ranks on win rate, not the combined score, and comes back in rank order.
    d, ids = seeded(tmp_path)
    board = scoring.rankings(d.conn, window=None, idle_sec=0)

    assert [b["rank"] for b in board] == [1, 2, 3]           # returned in rank order
    rates = [b["win_rate"] for b in board]
    assert rates == sorted(rates, reverse=True), rates
    assert board[0]["bot_uuid"] == ids["p1"]                 # 1.0
    assert {b["bot_uuid"] for b in board[1:]} == {ids["p2"], ids["p3"]}
    assert board[1]["win_rate"] == 0.25 and board[2]["win_rate"] == 0.25
    # Equal win rates: more games played comes first.
    assert board[1]["games"] >= board[2]["games"]


def windowed(tmp_path):
    """An old bot with a perfect record against a weak field, then a newer field that
    plays on. `now` is 1000.0; the old bot's last game is 20 minutes before that."""
    d = Database(tmp_path / "w.db")
    old, weak, a, b = (reg(d, "old"), reg(d, "weak"), reg(d, "a"), reg(d, "b"))
    t = db.new_uuid()
    d.start_tourney(t, {}, 1.0)

    def at(g, ended):
        g["ended_at"] = ended
        return g

    games = [at(game(t, old, weak, winner="a", ao="win", bo="loss", asr=20, bsr=90), -200.0)
             for _ in range(6)]
    games += [at(game(t, a, b, winner="a", ao="win", bo="loss", asr=40, bsr=50), 900.0),
              at(game(t, b, a, winner="a", ao="win", bo="loss", asr=45, bsr=55), 950.0),
              at(game(t, a, b, winner="b", ao="loss", bo="win", asr=60, bsr=41), 990.0)]
    d.record_games(games)
    return d, {"old": old[0], "weak": weak[0], "a": a[0], "b": b[0]}


def test_rankings_window_is_per_bot(tmp_path):
    d, ids = windowed(tmp_path)

    # Full history: the old bot's 6-0 record against a weak field tops the board.
    everything = {b["bot_uuid"]: b for b in
                  scoring.rankings(d.conn, window=None, idle_sec=0, now=1000.0)}
    assert everything[ids["old"]]["games"] == 6
    assert everything[ids["old"]]["win_rate"] == 1.0
    assert everything[ids["old"]]["rank"] == 1

    # Windowed to 3: every bot is scored on ITS OWN last 3 games, so each one that has
    # played at least 3 shows 3 — not a share of one global window.
    recent = {b["bot_uuid"]: b for b in
              scoring.rankings(d.conn, window=3, idle_sec=0, now=1000.0)}
    assert {b: recent[b]["games"] for b in recent} == {
        ids["old"]: 3, ids["weak"]: 3, ids["a"]: 3, ids["b"]: 3}
    # A bot with fewer games than the window keeps all of them.
    assert {b: recent[b]["total_games"] for b in recent} == {
        ids["old"]: 6, ids["weak"]: 6, ids["a"]: 3, ids["b"]: 3}

    # Bot a won only the first of its three games (it sat on side b for the second),
    # so scoring those three gives it 1 of 3.
    assert recent[ids["a"]]["win_rate"] == 1 / 3
    assert recent[ids["b"]]["win_rate"] == 2 / 3


def test_rankings_hide_idle_bots_unless_connected(tmp_path):
    d, ids = windowed(tmp_path)

    # 15-minute cutoff: the old bot last played 20 minutes ago, so it drops off.
    board = {b["bot_uuid"]: b for b in
             scoring.rankings(d.conn, window=None, idle_sec=900, now=1000.0)}
    assert ids["old"] not in board and ids["weak"] not in board
    assert set(board) == {ids["a"], ids["b"]}
    assert board[ids["a"]]["idle_sec"] == 10.0                # 1000.0 - 990.0

    # A live session keeps a bot on the board however long it has been idle.
    with_session = {b["bot_uuid"]: b for b in scoring.rankings(
        d.conn, active=frozenset({ids["old"]}), window=None, idle_sec=900, now=1000.0)}
    assert ids["old"] in with_session and with_session[ids["old"]]["active"] is True
    assert ids["weak"] not in with_session

    # Cutoff off entirely: everyone is back.
    assert len(list(scoring.rankings(d.conn, window=None, idle_sec=0, now=1000.0))) == 4


def test_heatmap_counts_own_ships_and_own_shots(tmp_path):
    d = Database(tmp_path / "h.db")
    p1, p2, p3 = reg(d, "p1"), reg(d, "p2"), reg(d, "p3")
    t = db.new_uuid()
    d.start_tourney(t, {"rows": 10, "cols": 10,
                        "fleet": [{"name": "destroyer", "size": 2}]}, 1.0)

    def played(a, b, *, a_ship, b_ship, a_shot, b_shot):
        g = game(t, a, b, winner="a", ao="win", bo="loss", asr=1, bsr=2)
        g["a_layout"] = [{"name": "destroyer", "row": a_ship[0], "col": a_ship[1],
                          "orientation": "H"}]
        g["b_layout"] = [{"name": "destroyer", "row": b_ship[0], "col": b_ship[1],
                          "orientation": "H"}]
        g["moves"] = [
            {"round": 1, "side": "a", "row": a_shot[0], "col": a_shot[1], "result": "miss",
             "sunk_ship": None, "response_ms": 1.0},
            {"round": 1, "side": "b", "row": b_shot[0], "col": b_shot[1], "result": "miss",
             "sunk_ship": None, "response_ms": 1.0},
        ]
        return g

    d.record_games([
        # p1 on side a twice, then on side b — its own ships and shots must follow it.
        played(p1, p2, a_ship=(0, 0), b_ship=(5, 5), a_shot=(9, 9), b_shot=(4, 4)),
        played(p1, p2, a_ship=(0, 0), b_ship=(5, 5), a_shot=(9, 9), b_shot=(4, 4)),
        played(p3, p1, a_ship=(7, 7), b_ship=(0, 0), a_shot=(1, 1), b_shot=(9, 9)),
    ])

    h = scoring.heatmap(d.conn, p1[0])
    assert h["games"] == 3 and h["rows"] == 10 and h["cols"] == 10
    # A size-2 destroyer at (0,0) horizontal covers (0,0) and (0,1), in all three games.
    assert h["ships"][0][0] == 3 and h["ships"][0][1] == 3
    assert h["ships"][5][5] == 0 and h["ships"][7][7] == 0      # opponents' ships
    assert sum(map(sum, h["ships"])) == 6                       # 3 games x 2 cells
    assert h["shots"][9][9] == 3 and h["shots"][4][4] == 0      # p1 always fired at 9,9
    assert h["total_shots"] == 3

    # The opponent's map is the mirror image.
    h2 = scoring.heatmap(d.conn, p2[0])
    assert h2["games"] == 2 and h2["ships"][5][5] == 2 and h2["shots"][4][4] == 2

    # ?vs= keeps only the pair's games.
    hv = scoring.heatmap(d.conn, p1[0], vs=p3[0])
    assert hv["games"] == 1 and hv["total_shots"] == 1
    assert hv["ships"][0][0] == 1 and hv["shots"][9][9] == 1

    # The sample is bounded and takes the most recent games first.
    assert scoring.heatmap(d.conn, p1[0], games=1)["games"] == 1


def test_h2h_score_counts_pairs_out_solved(tmp_path):
    d = Database(tmp_path / "s.db")
    fast, mid, slow, gone = (reg(d, "fast"), reg(d, "mid"), reg(d, "slow"), reg(d, "gone"))
    t = db.new_uuid()
    d.start_tourney(t, {}, 1.0)
    d.record_games([
        # fast out-solves mid; seats swap between the two games of the pair.
        game(t, fast, mid, winner="a", ao="win", bo="loss", asr=20, bsr=40),
        game(t, mid, fast, winner="b", ao="loss", bo="win", asr=40, bsr=20),
        # fast out-solves slow, and mid out-solves slow.
        game(t, fast, slow, winner="a", ao="win", bo="loss", asr=22, bsr=60),
        game(t, mid, slow, winner="a", ao="win", bo="loss", asr=41, bsr=61),
        # slow beats a bot that is no longer a contender — must not count for it.
        game(t, slow, gone, winner="a", ao="win", bo="loss", asr=30, bsr=90),
        # an unsolved pair: neither side has a time, so it goes unjudged.
        game(t, fast, gone, winner=None, ao="forfeit", bo="forfeit", asr=None, bsr=None,
             end="both_forfeit"),
    ])
    field = [fast[0], mid[0], slow[0]]
    h = scoring.h2h_scores(d.conn, field)

    assert h[fast[0]]["score"] == 2 and h[fast[0]]["pairs"] == 2      # beats mid and slow
    assert set(h[fast[0]]["beats"]) == {mid[0], slow[0]}
    assert h[mid[0]]["score"] == 1 and h[mid[0]]["pairs"] == 2        # loses to fast
    assert h[slow[0]]["score"] == 0 and h[slow[0]]["pairs"] == 2      # the `gone` pair is
    assert gone[0] not in h[slow[0]]["beats"]                         # outside the field

    # A tie on mean solve time gives neither side the pair.
    d2 = Database(tmp_path / "tie.db")
    p1, p2 = reg(d2, "p1"), reg(d2, "p2")
    t2 = db.new_uuid()
    d2.start_tourney(t2, {}, 1.0)
    d2.record_games([game(t2, p1, p2, winner="tie", ao="tie", bo="tie", asr=30, bsr=30)])
    tie = scoring.h2h_scores(d2.conn, [p1[0], p2[0]])
    assert tie[p1[0]]["score"] == 0 and tie[p2[0]]["score"] == 0
    assert tie[p1[0]]["pairs"] == 1

    # The window is global and takes the most recent games: with room for one game only,
    # a single pair is judged.
    narrow = scoring.h2h_scores(d.conn, field, window=1)
    assert sum(v["pairs"] for v in narrow.values()) <= 2

    # The board carries the score through.
    board = {b["bot_uuid"]: b for b in scoring.rankings(d.conn, window=None, idle_sec=0)}
    assert board[fast[0]]["h2h_score"] == 2 and board[fast[0]]["h2h_pairs"] == 2
    # `gone` is on the board here too (idle filter off), so slow's win over it now counts.
    assert board[slow[0]]["h2h_score"] == 1


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
    # enriched summary
    assert detail["win_rate"] == 1.0            # p1 won both its games
    assert detail["solver_avg"] == 40.0         # (30 + 50) / 2


def test_head_to_head(tmp_path):
    d, ids = seeded(tmp_path)
    h = scoring.head_to_head(d.conn, ids["p1"], ids["p2"])
    assert h["games"] == 1
    assert h["a_win_rate"] == 1.0               # p1 beat p2
    assert h["solve_delta"] == 10.0             # b_solve(40) - a_solve(30): p1 faster
    assert h["solve_leader"] == h["a"]["name"]
    assert "win_rate_sig" in h and "solve_delta_sig" in h
    # a bot has no games against itself
    assert scoring.head_to_head(d.conn, ids["p1"], ids["p1"])["games"] == 0
