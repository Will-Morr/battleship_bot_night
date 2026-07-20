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
3. **DB** — `db.py`: schema, WAL, batched inserts, `/sync` query helpers, snapshot for
   `/db`. Unit-tested round-trip + incremental sync.
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
