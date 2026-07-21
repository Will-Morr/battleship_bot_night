"""Tests for pairing and the synchronous tourney engine, driven by an in-process
dispatcher (no ZeroMQ) that can simulate silent/dead sessions."""

import asyncio
import os
import random

from battleship.bot_api import load_bot
from battleship.config import default_config
from battleship.tourney import TourneyEngine, make_pairings

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_, RANDOM_FACTORY = load_bot(os.path.join(REPO, "bots", "random_bot.py"))


# -- pairing -------------------------------------------------------------------

def test_pairing_trivial():
    assert make_pairings(["p"], 5, random.Random(0)) == []
    assert make_pairings([], 5, random.Random(0)) == []


def test_pairing_balanced_no_same_player():
    players = ["alice", "bob", "carol", "dave"]
    rng = random.Random(1)
    pairs = make_pairings(players, 3, rng)
    assert len(pairs) == 6                                    # 12 slots -> 6 pairs
    counts = {i: 0 for i in range(4)}
    for i, j in pairs:
        assert players[i] != players[j]                       # never same player
        counts[i] += 1
        counts[j] += 1
    assert all(c == 3 for c in counts.values())               # each ~target games


def test_pairing_excludes_same_player_bots():
    # Two of the three participants belong to the same player.
    players = ["p", "p", "q"]
    for seed in range(20):
        pairs = make_pairings(players, 4, random.Random(seed))
        for i, j in pairs:
            assert players[i] != players[j]


def test_pairing_dominant_player_tail_dropped():
    players = ["p", "p", "p", "q"]                            # p owns 3/4 of the room
    pairs = make_pairings(players, 1, random.Random(2))
    assert len(pairs) == 1                                     # only q can absorb one p
    assert {players[i] for i, j in pairs} | {players[j] for i, j in pairs} == {"p", "q"}


# -- engine --------------------------------------------------------------------

class LocalDispatcher:
    """Drives real bot instances in-process. Sessions in `silent_moves` place normally
    but never move; `silent_until[s]` keeps s silent through that move-round."""

    def __init__(self, factories, config):
        self.factories = factories
        self.config = config
        self.bots = {}
        self.silent_moves = set()
        self.silent_until = {}
        self._round = 0

    async def request_placements(self, requests, config, deadline):
        replies = {}
        for sess, handles in requests.items():
            out = {}
            for h in handles:
                bot = self.factories[sess](dict(config, game_id=h))
                self.bots[(sess, h)] = bot
                out[h] = bot.place_ships()
            replies[sess] = out
        return replies

    async def request_moves(self, requests, deadline):
        self._round += 1
        replies = {}
        for sess, games in requests.items():
            if sess in self.silent_moves or self._round <= self.silent_until.get(sess, 0):
                continue
            replies[sess] = {h: (self.bots[(sess, h)].make_move(v), 1.0) for h, v in games.items()}
        return replies


def participants(*specs):
    # specs: (player, session_uuid)
    return [{"player": p, "session_uuid": s, "bot_uuid": "bot-" + p} for p, s in specs]


def build(parts, target, grace=1):
    factories = {p["session_uuid"]: RANDOM_FACTORY for p in parts}
    engine = TourneyEngine("t1", parts, default_config(), target, random.Random(7),
                           blackout_grace=grace)
    disp = LocalDispatcher(factories, default_config())
    return engine, disp


def test_engine_runs_full_tourney():
    random.seed(3)
    parts = participants(("alice", "s_alice"), ("bob", "s_bob"))
    engine, disp = build(parts, target=3)
    records = asyncio.run(engine.run(disp))
    assert engine.num_games == 3 and len(records) == 3
    for r in records:
        assert r["end_reason"] == "complete"
        assert r["winner"] in ("a", "b", "tie")
        assert r["moves"], "expected a move log"
        assert 17 <= r["a_solved_round"] <= 100
        assert r["started_at"] is not None and r["ended_at"] is not None


def test_engine_drop_forfeits_and_opponent_wins():
    random.seed(4)
    parts = participants(("alice", "s_alice"), ("bob", "s_bob"))
    engine, disp = build(parts, target=3, grace=0)   # grace 0: first blackout disconnects
    disp.silent_moves.add("s_bob")                   # bob places but never moves
    records = asyncio.run(engine.run(disp))
    assert "s_bob" in engine.dead_sessions
    for r in records:
        # bob is whichever side holds s_bob; that side forfeits by drop, alice wins.
        bob_side = "a" if r["a_session_uuid"] == "s_bob" else "b"
        opp = "b" if bob_side == "a" else "a"
        assert r[f"{bob_side}_outcome"] == "forfeit"
        assert r[f"{opp}_outcome"] == "win"
        assert r["end_reason"].endswith("_drop")
        assert r[f"{opp}_solved_round"] is not None     # opponent still got a solver score


def test_engine_stall_then_recover_no_forfeit():
    random.seed(5)
    parts = participants(("alice", "s_alice"), ("bob", "s_bob"))
    engine, disp = build(parts, target=2, grace=1)
    disp.silent_until["s_bob"] = 1                    # bob silent on move-round 1 only
    records = asyncio.run(engine.run(disp))
    assert engine.dead_sessions == set()              # within grace -> nobody dropped
    for r in records:
        assert r["end_reason"] == "complete"          # games stalled then finished cleanly
