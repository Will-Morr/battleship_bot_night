# Data Contracts

The frozen interfaces every component builds against. Change these deliberately;
everything else is implementation detail.

Serialization is **JSON** everywhere (wire + API). Chosen for readability over raw
speed; payloads are small and the network dominates latency. Swappable to msgpack
later without touching semantics.

---

## 1. Core encodings

### Coordinates
- The board is `rows` x `cols`, default **10 x 10**.
- A cell is `[row, col]`, **0-indexed**, row 0 at the top. Always a 2-element list.
- Flat index helpers exist in `bot_api` (`idx = row * cols + col`) but the wire
  format is always `[row, col]`.

### Fleet
Ordered list of ships. Default standard fleet:

```json
[
  {"name": "carrier",    "size": 5},
  {"name": "battleship", "size": 4},
  {"name": "cruiser",    "size": 3},
  {"name": "submarine",  "size": 3},
  {"name": "destroyer",  "size": 2}
]
```

Names are unique (the two size-3 ships differ) because a sink reports the ship name.

### Placement (one ship)
```json
{"name": "carrier", "row": 0, "col": 0, "orientation": "H"}
```
- `(row, col)` is the ship's anchor: its top-most / left-most cell.
- `"H"` extends toward increasing `col`; `"V"` toward increasing `row`.
- A **layout** is a list of placements, one per fleet ship.

A layout is **legal** iff: exactly the fleet's ship names appear once each, each
ship's `size` matches the fleet, every cell is in bounds, and no two ships overlap.
An illegal layout forfeits that game (see §6).

### Shot result
Every shot resolves to:
```json
{"result": "miss" | "hit" | "sunk", "sunk_ship": null | "cruiser"}
```
- `sunk_ship` is non-null **iff** `result == "sunk"` (a sink is also a hit).
- A player has **solved** the opponent's board the round their shot sinks the last
  ship. That shot's `result` is `"sunk"`.

### Rounds
- Round **0** = placement.
- Round **1** = first shots; increments by 1 each synchronous tick.
- A move's `round` is its 1-indexed shot number.
- `solved_round` for a player = the round on which they sank the opponent's last ship.

---

## 2. Bot API contract

A bot file is a single Python module with:

```python
BOT = {"player": "will", "bot": "greedy-hunter"}   # required identity dict

class Bot:
    def __init__(self, config):     # one instance per game
        ...
    def place_ships(self):          # round 0 -> list[placement]
        ...
    def make_move(self, view):      # each round it still shoots -> [row, col]
        ...
```

- One `Bot` instance **per game**; it owns that game's memory. The runner constructs
  instances and dispatches per-game.
- `config` (also sent as `registered.config` / `place_request.config`):
  ```json
  {
    "rows": 10, "cols": 10,
    "fleet": [ {"name": "...", "size": N}, ... ],
    "move_time_ms": 250,
    "game_id": 12345
  }
  ```
- `place_ships()` returns a layout (§1).
- `make_move(view)` returns the next target `[row, col]`. `view` is the **delta** for
  that game (the bot keeps its own history):
  ```json
  {
    "round": 7,
    "your_last":     {"cell": [3,4], "result": "hit", "sunk_ship": null} | null,
    "opponent_last": {"cell": [8,1], "result": "miss", "sunk_ship": null} | null,
    "opponent_finished": false
  }
  ```
  On round 1 both `*_last` are `null`. `opponent_last` describes the opponent's
  previous shot **against this bot's board** (sent per the synchronous rules).
  `opponent_finished` becomes true once the opponent has solved this bot's board.
  `opponent_last` is `null` on any round where the opponent did not shoot last round
  (round 1, or any round after it finished or forfeited); `opponent_finished`
  distinguishes "hasn't shot yet" from "done".

A minimal functional form (module-level `place_ships(config)` / `make_move(view, mem)`)
is also accepted by the runner, but `class Bot` is canonical.

---

## 3. Wire protocol (ZeroMQ ROUTER/DEALER)

- Server binds one **ROUTER**; each runner is one **DEALER** (one process, one
  connection, all of that bot's parallel games multiplexed over it).
- Every message is a single frame: a JSON object with a `"type"` field. ROUTER
  identity frames are handled by ZMQ and never appear in the JSON.
- **Batching is the throughput core:** one request per bot per round carries *all*
  that bot's pending games; one reply carries all its moves.
- `game_id` on the wire is a compact **per-tourney integer handle** (JSON keys are its
  string form), not the persistent `games.uuid`; the server maps between them. Bots
  analyze finished data via the synced DB by `bot_uuid`, so they never need the uuid live.

### Client -> Server
```json
{"type": "register", "player": "will", "bot": "greedy-hunter",
 "code": "<full source>", "code_filename": "greedy.py", "runner_version": "1"}

{"type": "place_reply", "tourney_id": 4,
 "placements": {"101": [<placement>, ...], "102": [...]},
 "compute_ms": {"101": 0.8, "102": 0.9}}

{"type": "move_reply", "tourney_id": 4,
 "moves": {"101": [3,4], "102": [0,0]},
 "compute_ms": {"101": 1.2, "102": 0.7}}

{"type": "pong", "t": 4}            // reply to server ping while idle
{"type": "bye"}                     // optional clean disconnect
```
`compute_ms[game_id]` is the bot's self-timed compute per move, recorded for
**analytics only**. Enforcement is the whole-request deadline (below), not this value.

### Server -> Client
```json
{"type": "registered", "uuid": "<session-uuid>", "bot_uuid": "<logical-bot-uuid>", "config": {...}}

{"type": "tourney_start", "tourney_id": 4, "your_games": [101, 102]}

{"type": "place_request", "tourney_id": 4, "config": {...}, "games": [101, 102]}

{"type": "move_request", "tourney_id": 4, "deadline_ms": 250,
 "views": {"101": <view>, "102": <view>}}

{"type": "game_result", "game_id": 101, "outcome": "win",
 "you_solved_round": 41, "opponent_solved_round": 55, "reason": "complete"}

{"type": "tourney_end", "tourney_id": 4}
{"type": "idle", "next_tourney_in_ms": 3000}
{"type": "ping", "t": 4}
{"type": "kick", "reason": "unresponsive"}
```

### Round flow (server authoritative)
1. `tourney_start` -> `place_request` (all games) -> `place_reply`. Validate each
   layout; illegal layout forfeits that game.
2. Loop: `move_request` (views for games where this bot still shoots) -> `move_reply`
   within `deadline_ms`. Per game, a missing / late / illegal move forfeits that game.
   Apply all moves simultaneously, resolve, emit `game_result` for finished games.
3. `tourney_end` when every game is finished.

### Timeout & liveness (one whole-request deadline)
`deadline_ms` is a **single wall-clock budget on the entire batch reply**. It bounds
bot processing time *and* comms *and* prevents games from hanging — one mechanism, not
several. Default **250 ms** (the standard), configurable per tourney; it may optionally
scale with batch size (`base_ms + per_game_ms * num_games`, default per_game_ms small)
so large batches stay feasible.

At the deadline the round is resolved from whatever `move_reply` arrived:
- **Missed window (partial):** if the session replied but a game's move is absent / late
  / illegal, that game is forfeited (instant loss; the game continues for the opponent,
  §6) and the session stays connected. A bot may miss a few of these — the "miss a few
  windows" case.
- **Total blackout (whole batch):** if nothing usable arrives for *any* of the session's
  games, the session is unresponsive this round. Its games are **stalled** (not lost) for
  up to `blackout_grace` consecutive rounds (default 1) to absorb a transient blip — a
  bounded pause of at most grace x deadline, never an open-ended hang. Exceed the grace
  (or lose the ZMTP heartbeat) and the session is **auto-disconnected**: every remaining
  game forfeits ('drop') and the bot is excluded until it re-registers (new uuid).

`compute_ms` is recorded but never gates anything — the deadline is authoritative.

### Session restart & re-registration
A restart always means a new session/uuid (per spec). Re-registering a logical bot
`(player, name)` that already has a live session **immediately retires the old session**:
its still-active games forfeit at once rather than hanging opponents on repeated
deadlines, and the new session joins from the next tourney. In-flight games of the old
session are lost — an accepted tradeoff, kept cheap because **tourneys are short**: all
games run concurrently and rounds advance at bot speed (a few ms on a LAN), so a tourney
is typically seconds. A grace-window "resume my in-flight games" path is intentionally
out of scope for v1 — a restarted process has no memory of those games, so resuming
would require a full-history resync that short tourneys make unnecessary.

---

## 4. Database schema (SQLite, WAL mode)

Everything is kept forever — every layout and every move persists. Two id columns per
table: **`uuid`** (TEXT, globally unique) is what all joins/foreign keys use; **`seq`**
(INTEGER PRIMARY KEY = rowid, monotonic because we never delete) is the cursor for
incremental sync and ordering.

```sql
tourneys(
  seq INTEGER PRIMARY KEY,
  uuid TEXT UNIQUE NOT NULL,
  started_at REAL NOT NULL,             -- start timestamp
  ended_at REAL,                        -- stop timestamp (NULL while running)
  status TEXT NOT NULL,                 -- 'running' | 'complete'
  num_games INTEGER,
  config_json TEXT NOT NULL
);

bots(                                   -- logical bot (player, name); stats accumulate here
  seq INTEGER PRIMARY KEY,
  uuid TEXT UNIQUE NOT NULL,            -- stable logical-bot id; games join on this
  player TEXT NOT NULL,
  name TEXT NOT NULL,
  first_tourney_uuid TEXT REFERENCES tourneys(uuid),
  last_tourney_uuid  TEXT REFERENCES tourneys(uuid),
  first_seen_at REAL NOT NULL,
  last_seen_at  REAL NOT NULL,
  metadata_json TEXT,                   -- optional, free-form
  UNIQUE(player, name)
);

bot_sessions(                           -- one per register/reconnect; holds code (private)
  seq INTEGER PRIMARY KEY,
  uuid TEXT UNIQUE NOT NULL,            -- session uuid, new each reconnect
  bot_uuid TEXT NOT NULL REFERENCES bots(uuid),
  code TEXT NOT NULL,                   -- full source, saved forever, never public
  code_filename TEXT,
  code_hash TEXT NOT NULL,              -- sha256, version identity
  runner_version TEXT,
  registered_at REAL NOT NULL,
  disconnected_at REAL
);

games(                                  -- denormalized results table (fast parsing)
  seq INTEGER PRIMARY KEY,
  uuid TEXT UNIQUE NOT NULL,
  tourney_uuid TEXT NOT NULL REFERENCES tourneys(uuid),
  a_bot_uuid TEXT NOT NULL REFERENCES bots(uuid),        -- for stats joins
  b_bot_uuid TEXT NOT NULL REFERENCES bots(uuid),
  a_session_uuid TEXT NOT NULL REFERENCES bot_sessions(uuid),  -- which code played
  b_session_uuid TEXT NOT NULL REFERENCES bot_sessions(uuid),
  a_layout_json TEXT NOT NULL,          -- full layout, forever
  b_layout_json TEXT NOT NULL,
  a_solved_round INTEGER,               -- A's solver metric (turns A took); NULL = DNF
  b_solved_round INTEGER,               -- B's solver metric
  winner TEXT,                          -- 'a' | 'b' | 'tie' | NULL
  a_outcome TEXT,                       -- 'win'|'loss'|'tie'|'forfeit'
  b_outcome TEXT,
  end_reason TEXT,                      -- 'complete'|'a_illegal'|'b_illegal'|'a_timeout'|'b_timeout'|'a_drop'|'b_drop'|'both_forfeit'
  total_rounds INTEGER,
  started_at REAL NOT NULL,             -- start timestamp
  ended_at REAL,                        -- stop timestamp
  a_resp_min_ms REAL, a_resp_max_ms REAL, a_resp_avg_ms REAL,  -- A's per-move response times
  b_resp_min_ms REAL, b_resp_max_ms REAL, b_resp_avg_ms REAL   -- B's
);

moves(                                  -- per-move rows; the big table
  seq INTEGER PRIMARY KEY,
  uuid TEXT UNIQUE NOT NULL,            -- present per spec; join key is game_uuid
  game_uuid TEXT NOT NULL REFERENCES games(uuid),
  round INTEGER NOT NULL,               -- 1-indexed shot number
  side TEXT NOT NULL,                   -- 'a' | 'b' (shooter)
  row INTEGER NOT NULL,
  col INTEGER NOT NULL,
  result TEXT NOT NULL,                 -- 'hit'|'miss'|'sunk'
  sunk_ship TEXT,                       -- ship name iff result='sunk'
  response_ms REAL                      -- bot-reported response time for this move
);
```

Indexes: `moves(game_uuid, round)`, `games(tourney_uuid)`, `games(a_bot_uuid)`,
`games(b_bot_uuid)`, `bot_sessions(bot_uuid)`.

Note: `moves.uuid` is identity-only (nothing joins to it) and is the heaviest column at
scale — the obvious lever (drop it, or store all uuids as 16-byte BLOBs) if a dataset of
many millions of moves needs trimming. Kept as readable TEXT by default.

**Derived metrics (not stored, computed in `scoring.py`):**
- A's *layout* score = `b_solved_round` (turns the opponent took to crack A). Higher
  is better; a `NULL` (opponent DNF) is the best possible layout.
- A's *solver* score = `a_solved_round`. Lower is better.

---

## 5. HTTP API (aiohttp, same process)

- `GET /`            -> stats site (static).
- `GET /projector`   -> full-screen live rankings board (static).
- `GET /api/rankings` -> leaderboards: per bot `{bot_uuid, player, name, games, wins,
  win_rate, avg_solver, avg_layout, combined, active}`.
- `GET /api/bots`, `GET /api/bot/{bot_uuid}` -> bot list; detail with solver/layout
  histograms and per-opponent breakdown.
- `GET /api/pairings` -> pairwise winrate/avg-score matrix over active bots.
- `GET /api/tourneys` -> tourney list + status.
- `GET /events`      -> SSE: `tourney_start`, `game_complete`, `ranking_update`,
  `tourney_end` for the live projector.
- `GET /sync?games=&moves=` -> incremental JSON of rows with `seq` greater than each
  per-table cursor. The sync client keeps a local SQLite mirror with this identical
  schema and advances cursors by max `seq` per table. **Paged**: at most 20k rows per
  table; the response carries `"more": true` when another page is waiting, and the
  caller re-requests with the returned cursors until it is false. Never unbounded —
  `moves` reaches millions of rows within an evening, and one unpaged response
  materializes the table as dicts plus JSON (~1.3 KB peak RSS per row, i.e. multiple GB)
  and will OOM the server.
- `GET /db` -> one-shot download of a consistent snapshot (`VACUUM INTO`) of the DB. The
  snapshot is a full-size temporary copy, streamed and then deleted; expect the download
  to be as large as the live DB (GBs during a long event) and set a client timeout to
  match. A truncated download still carries HTTP 200, so check `Content-Length` against
  the bytes received before trusting the file.

---

## 6. Scoring & game-end semantics

Three competitions, all derivable from `games`:
- **Win rate** = wins / games (ties count 0.5). The winner is the side that solved in
  strictly fewer rounds. Equal solving round = **tie** (0.5 each). A `finished` side
  beats a `forfeited` one. **Both forfeited / neither solved = no winner and a loss for
  both** (0 each) — a tie is only mutual success on the same round, never mutual failure.
- **Solver** = mean `solved_round` over solved games (lower better); DNFs reported
  separately, excluded from the mean.
- **Layout** = mean opponent `solved_round` against this bot (higher better); opponent
  DNF counts as the best case (tracked separately).
- **Combined** (single projector rank) = configurable; default is the mean of the
  three per-competition percentile ranks. The three boards remain primary.

**Game termination:** each side is `solving`, `finished` (solved the opponent), or
`forfeited`. A side needs a move only while `solving`. **The game ends the round no
side is still `solving`** — every side is finished or forfeited. This covers all cases:
both finish, one finishes then the other forfeits, one forfeits then the other finishes,
or both forfeit.

**Forfeit (illegal move / missed window / timeout):** the offender is set `forfeited`
for that game — instant win-rate loss and solver DNF — but the game keeps running so a
still-`solving` opponent can finish, preserving the opponent's solver score and the
offender's layout score. `end_reason` records the cause.
