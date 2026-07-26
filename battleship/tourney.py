"""Pairing + the synchronous round engine. Transport-agnostic: the engine drives games
by calling a `dispatcher` that exchanges batched requests/replies with bots, so it runs
identically over ZeroMQ (server) or in-process (tests).

A dispatcher implements two async methods:
    request_placements(requests, config, deadline_ms) -> {session: {handle: layout}}
    request_moves(requests, deadline_ms)              -> {session: {handle: (cell, ms)}}
A session missing from a reply (or an empty reply) means it produced nothing that round.
"""

import time
from collections import defaultdict

from . import config as cfg
from .db import new_uuid
from .game import Game


def make_pairings(players, target, rng):
    """Pair participant indices ~`target` times each, never same-player. `players[i]` is
    participant i's player name. See PLAN.md "Pairing" — a shuffled greedy match."""
    n = len(players)
    if n < 2 or target < 1:
        return []
    slots = [i for i in range(n) for _ in range(target)]
    rng.shuffle(slots)
    pairs = []
    waiting = []  # slots with no legal partner yet
    for slot in slots:
        for k, w in enumerate(waiting):
            if players[w] != players[slot]:
                waiting.pop(k)
                pairs.append((w, slot))
                break
        else:
            waiting.append(slot)
    # Whatever is still waiting is the unpairable same-player tail — dropped.
    return pairs


class Match:
    """One game and its two participants (each a dict with session_uuid, bot_uuid,
    player). `handle` is the compact per-tourney id used on the wire."""

    def __init__(self, handle, tourney_uuid, a, b):
        self.handle = handle
        self.tourney_uuid = tourney_uuid
        self.side = {"a": a, "b": b}
        self.uuid = new_uuid()
        self.game = None
        self.started_at = None
        self.ended_at = None

    def session(self, side):
        return self.side[side]["session_uuid"]

    def record(self):
        r = self.game.result()
        return {
            "uuid": self.uuid,
            "tourney_uuid": self.tourney_uuid,
            "a_bot_uuid": self.side["a"]["bot_uuid"],
            "b_bot_uuid": self.side["b"]["bot_uuid"],
            "a_session_uuid": self.side["a"]["session_uuid"],
            "b_session_uuid": self.side["b"]["session_uuid"],
            "a_layout": self.game.layout["a"],
            "b_layout": self.game.layout["b"],
            "winner": r["winner"],
            "a_outcome": r["a_outcome"],
            "b_outcome": r["b_outcome"],
            "a_solved_round": r["a_solved_round"],
            "b_solved_round": r["b_solved_round"],
            "end_reason": r["end_reason"],
            "total_rounds": r["total_rounds"],
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "moves": self.game.moves_log,
        }


class TourneyEngine:
    def __init__(self, tourney_uuid, participants, config, target, rng,
                 blackout_grace=cfg.BLACKOUT_GRACE, per_game_ms=cfg.DEADLINE_PER_GAME_MS,
                 place_time_ms=cfg.PLACE_TIME_MS):
        self.tourney_uuid = tourney_uuid
        self.participants = participants
        self.config = config
        self.grace = blackout_grace
        self.move_time_ms = config["move_time_ms"]
        self.per_game_ms = per_game_ms
        self.place_time_ms = place_time_ms
        self.dead_sessions = set()          # sessions the engine disconnected mid-tourney
        self._streak = defaultdict(int)      # consecutive full-blackout rounds per session

        players = [p["player"] for p in participants]
        self.matches = [
            Match(handle, tourney_uuid, participants[i], participants[j])
            for handle, (i, j) in enumerate(make_pairings(players, target, rng))
        ]

    @property
    def num_games(self):
        return len(self.matches)

    def participant_bot_uuids(self):
        return {p["bot_uuid"] for p in self.participants}

    async def run(self, dispatcher):
        """Play the whole tourney. Returns a list of game records for the DB."""
        if not self.matches:
            return []
        await self._placement(dispatcher)
        await self._play(dispatcher)
        return [m.record() for m in self.matches]

    # -- rounds -----------------------------------------------------------------

    async def _placement(self, dispatcher):
        now = time.time()
        requests = defaultdict(list)
        for m in self.matches:
            requests[m.session("a")].append(m.handle)
            requests[m.session("b")].append(m.handle)
        replies = await dispatcher.request_placements(dict(requests), self.config,
                                                      self.place_time_ms)
        for m in self.matches:
            layout_a = replies.get(m.session("a"), {}).get(m.handle)
            layout_b = replies.get(m.session("b"), {}).get(m.handle)
            m.game = Game(layout_a, layout_b, self.config)  # missing/invalid -> forfeit
            m.started_at = now

    async def _play(self, dispatcher):
        max_rounds = self.config["rows"] * self.config["cols"] + 5
        while True:
            active = [m for m in self.matches if not m.game.over]
            if not active:
                break

            requests = defaultdict(dict)
            for m in active:
                for side, view in m.game.views().items():
                    requests[m.session(side)][m.handle] = view
            pending = set(requests)
            deadline = self._deadline(len(v) for v in requests.values())
            replies = await dispatcher.request_moves(dict(requests), deadline)

            dead_now, stalled = self._liveness(pending, replies)
            self.dead_sessions |= dead_now
            self._apply(active, replies, dead_now, stalled)

            # Safety net: no legal solver can need more than rows*cols shots.
            now = time.time()
            for m in active:
                if not m.game.over and m.game.current_round > max_rounds:
                    m.game.forfeit("a", "drop")
                    m.game.forfeit("b", "drop")
                    m.ended_at = now

        for m in self.matches:
            if m.ended_at is None:
                m.ended_at = time.time()

    def _liveness(self, pending, replies):
        """Classify each session that owed a move: reset streaks for responders, and
        split silent ones into disconnected (over grace) vs stalled (within grace)."""
        dead_now, stalled = set(), set()
        for sess in pending:
            if replies.get(sess):
                self._streak[sess] = 0
            else:
                self._streak[sess] += 1
                (dead_now if self._streak[sess] > self.grace else stalled).add(sess)
        return dead_now, stalled

    def _apply(self, active, replies, dead_now, stalled):
        now = time.time()
        for m in active:
            # Disconnected sessions forfeit their side ('drop'); a live opponent plays on.
            for side in ("a", "b"):
                if m.game.needs_move(side) and m.session(side) in dead_now:
                    m.game.forfeit(side, "drop")
            # Stall (don't advance) games whose live-but-silent session is within grace.
            if any(m.game.needs_move(side) and m.session(side) in stalled for side in ("a", "b")):
                continue
            if m.game.over:
                m.ended_at = now
                continue
            cells, response = {}, {}
            for side in ("a", "b"):
                if not m.game.needs_move(side):
                    continue
                got = replies.get(m.session(side), {}).get(m.handle)
                if got is not None:
                    cells[side], response[side] = got
                else:
                    cells[side] = None  # session replied but omitted this game -> timeout
            m.game.resolve(cells, response)
            if m.game.over:
                m.ended_at = now

    def _deadline(self, batch_sizes):
        max_batch = max(batch_sizes, default=1)
        return self.move_time_ms + self.per_game_ms * max_batch
