# Implementation Plan

Builds against `DATA_CONTRACTS.md` (the frozen interfaces). See `prompt.md` for the
original spec.

## Architecture at a glance

One `asyncio` server process runs everything:
- **ZeroMQ ROUTER** for bot comms (each runner is a DEALER; one connection per bot
  multiplexes all its parallel games).
- **Tourney engine**: pairing -> synchronous round loop -> record -> update scores.
- **SQLite** (WAL) writer; every layout and move kept forever.
- **aiohttp HTTP**: `/sync`, `/db`, JSON analytics API, SSE, and the static site.

`zmq.asyncio` sockets share the aiohttp event loop, so it is genuinely one process,
one loop. Battleship logic is trivial CPU; the load is I/O coordination, which asyncio
handles well.

Batching is the throughput lever: one request/reply per bot per round covering all its
games. A single whole-request deadline (default 250 ms) bounds processing + comms and
prevents hangs.

## Key mechanisms

### DB writes never block the loop
Latency is the top priority, so the event loop must never do synchronous SQLite I/O. The
round loop only pushes finished records (games, moves) onto an in-memory queue — cheap.
A **dedicated writer thread** (via `loop.run_in_executor`, one connection) drains the
queue and commits in batched transactions. Analytics reads use a **separate WAL reader
connection** run in `asyncio.to_thread`, so reads and writes never touch the loop or each
other (WAL allows concurrent readers during a write). A DB flush can never eat into
`deadline_ms`.

### Pairing (per tourney)
Constraints: each active bot gets ~`target` games, no same-player pairings (and no
bot-vs-itself), otherwise **fully random** — no repeat-avoidance, no seeding, no
skill-matching. Repeat pairings are allowed and expected.

Pairing is a uniform random matching of a slot multiset, conditioned on the constraint,
via **rejection sampling** (shuffling + adjacent pairing is already a uniform random
matching; reshuffling until valid keeps it uniform conditioned on "no same-player"):
1. Build a slot multiset — each active bot repeated `target` times.
2. Shuffle and pair adjacent slots. If every pair is legal, accept — this draw is
   uniform over legal matchings.
3. If any pair is same-player, reshuffle and retry (bounded retries).
4. **Fallback** (only if retries exhaust — one player owns ~half the slots, so random
   draws rarely come out legal): repair collisions by swapping with a later valid slot,
   and drop any still-unpairable tail. This case cannot be uniform regardless, since the
   constraint itself is near-infeasible.

Degenerate cases, handled explicitly and unit-tested:
- **Odd slot count:** one leftover slot is dropped (that bot gets `target-1` this
  tourney). `target` is a target, not a guarantee.
- **One player owns > half the slots:** their excess slots cannot be legally paired, so
  those get dropped — that player's bots play fewer games. Unavoidable given the rule.
- **Late-joining bot:** pairing is independent per tourney over the currently-active set,
  so a late joiner simply gets `target` games from its first tourney on. Lifetime totals
  differ, but rankings use rates/means, so uneven totals only change noise, not fairness.

Repeat pairings across a tourney (playing the same opponent more than once) are allowed
and expected. Duplicate exact pairings are not deduplicated.

### Tourney brevity & restart tradeoff
Because all games in a tourney run concurrently and rounds advance at bot speed, a full
tourney is typically seconds. This is what makes the "restart forfeits your in-flight
games" tradeoff cheap (see `DATA_CONTRACTS.md` §3, Session restart). `target` games/bot
is the main knob organizers use to keep tourneys short.

## Repository layout

```
battleship/                 # importable package (server + client + shared)
  __init__.py
  config.py                 # defaults: board, fleet, timeouts, target games
  protocol.py               # message builders/parsers, JSON (de)serialization
  game.py                   # Board, ship placement validation, shot resolution, Game
  bot_api.py                # Bot base class, View/config types, cell helpers
  tourney.py                # pairing algorithm + synchronous round engine
  db.py                     # schema, batched writes (WAL), sync queries, snapshot
  scoring.py                # rankings: win rate / solver / layout / combined
  server.py                 # ROUTER loop, sessions, drop handling, HTTP app, SSE
  run_bot.py                # runner: register + play; --mode single|threads|processes
  sync_db.py                # incremental local DB mirror client
  local_match.py            # in-process game runner used by the test harness
bots/
  random_bot.py             # random placement + random distinct guessing
  ordered_bot.py            # random placement + row-major ordered guessing
test/
  test_bot.py               # run a bot in N local games vs the two examples
tests/                      # pytest unit tests for game/tourney/scoring/protocol
web/
  index.html  app.js  styles.css   # stats site (vanilla JS + small CDN chart lib)
  projector.html                   # full-screen live rankings board
.claude/skills/test-bot/SKILL.md   # skill wrapping test/test_bot.py
requirements.txt  README.md
```

## Build phases

1. **Foundation** — `config`, `protocol`, `game`, `bot_api`. Pure logic, unit-tested:
   placement validation, shot resolution, sink detection, solve detection, illegal-move
   rules. No network.
2. **Bots + local match + test harness** — two example bots, `local_match` (synchronous
   game between two in-process bots), `test/test_bot.py` (bot vs both examples, prints
   win-rate / solver / layout summary). Gives an end-to-end game loop with zero infra.
3. **DB** — `db.py`: schema, WAL, off-loop writer thread + queue, batched inserts,
   `/sync` query helpers (seq cursors), separate reader connection, snapshot for `/db`.
   Unit-tested round-trip + incremental sync.
4. **Server + tourney engine** — `tourney.py` (pairing, round loop reusing `game.py`),
   `server.py` (ROUTER, registration + session/uuid mgmt, batched dispatch, whole-request
   deadline, missed-window forfeit + blackout disconnect, DB writes, scoring hooks).
5. **Runner** — `run_bot.py`: DEALER connect, register (uploads source), receive/compute/
   send loop, `--mode single|threads|processes`, idle handling between tourneys. Verify a
   real bot completes a real tourney against the server.
6. **Analytics** — HTTP JSON API + SSE in `server.py`; `web/` projector + stats site;
   `sync_db.py` incremental mirror.
7. **Polish** — `README.md` (run/write-a-bot/mac notes), `requirements.txt`, the
   `test-bot` skill, and an end-to-end smoke run (server + example bots + a test bot).

Each phase is committed and verified before the next. Correctness of the game rules and
the round engine is validated by driving real games, not just unit tests.

## Defaults (tunable in `config.py`)

- Board 10x10; fleet carrier5/battleship4/cruiser3/submarine3/destroyer2.
- `request_timeout_ms = 250` (whole batch), `blackout_grace = 1`.
- Target games/bot/tourney ~50; continuous back-to-back tourneys.
- Rankings: three boards (win rate, solver, layout) + a combined percentile rank.
