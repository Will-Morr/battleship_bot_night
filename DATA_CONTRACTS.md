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
{"type": "registered", "uuid": "<session-uuid>", "bot_id": 7, "config": {...}}

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

At the deadline, the round is resolved from whatever `move_reply` arrived:
- **Missed window (per game):** a game whose move is absent / late / illegal is
  forfeited (instant loss for that bot; the game continues for the opponent, §6). A bot
  may miss a few of these and stays connected.
- **Total blackout (all windows at once):** if no usable move for *any* requested game
  arrives by the deadline, the session is treated as dead/hung -> **auto-disconnect**:
  all its remaining games forfeit and the bot is excluded until it re-registers (new
  uuid). A small number of consecutive full blackouts is tolerated (`blackout_grace`,
  default 1) to ride out a transient blip; ZMTP socket heartbeat is the backstop.

`compute_ms` is recorded but never gates anything — the deadline is authoritative.

---

## 4. Database schema (SQLite, WAL mode)

Append-only where possible; autoincrement ids drive incremental sync. Everything is
kept forever — every layout and every move persists.

```sql
players(
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL
);

bots(                                  -- logical bot; stats accumulate here
  id INTEGER PRIMARY KEY,
  player_id INTEGER NOT NULL REFERENCES players(id),
  name TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(player_id, name)
);

bot_sessions(                          -- one per register/reconnect; holds code (private)
  uuid TEXT PRIMARY KEY,               -- new each reconnect
  bot_id INTEGER NOT NULL REFERENCES bots(id),
  code TEXT NOT NULL,                  -- full source, saved forever, never public
  code_filename TEXT,
  code_hash TEXT NOT NULL,             -- sha256, version identity
  runner_version TEXT,
  registered_at REAL NOT NULL,
  disconnected_at REAL
);

tourneys(
  id INTEGER PRIMARY KEY,
  started_at REAL NOT NULL,
  ended_at REAL,
  status TEXT NOT NULL,                -- 'running' | 'complete'
  num_games INTEGER,
  config_json TEXT NOT NULL
);

games(                                 -- denormalized results table (fast parsing)
  id INTEGER PRIMARY KEY,
  tourney_id INTEGER NOT NULL REFERENCES tourneys(id),
  a_session TEXT NOT NULL REFERENCES bot_sessions(uuid),
  b_session TEXT NOT NULL REFERENCES bot_sessions(uuid),
  a_bot_id INTEGER NOT NULL,           -- denorm for group-by
  b_bot_id INTEGER NOT NULL,
  a_layout_json TEXT NOT NULL,         -- full layout, forever
  b_layout_json TEXT NOT NULL,
  a_solved_round INTEGER,              -- A's solver metric (turns A took); NULL = DNF
  b_solved_round INTEGER,              -- B's solver metric
  winner TEXT,                         -- 'a' | 'b' | 'tie' | NULL
  a_outcome TEXT,                      -- 'win'|'loss'|'tie'|'forfeit'
  b_outcome TEXT,
  end_reason TEXT,                     -- 'complete'|'a_illegal'|'b_illegal'|'a_timeout'|'b_timeout'|'a_drop'|'b_drop'|'both_forfeit'
  total_rounds INTEGER,
  started_at REAL,
  ended_at REAL
);

moves(                                 -- per-move rows; the big table
  id INTEGER PRIMARY KEY,
  game_id INTEGER NOT NULL REFERENCES games(id),
  round INTEGER NOT NULL,              -- 1-indexed shot number
  side TEXT NOT NULL,                  -- 'a' | 'b' (shooter)
  row INTEGER NOT NULL,
  col INTEGER NOT NULL,
  result TEXT NOT NULL,                -- 'hit'|'miss'|'sunk'
  sunk_ship TEXT,                      -- ship name iff result='sunk'
  compute_ms REAL
);
```

Indexes: `moves(game_id, round)`, `games(tourney_id)`, `games(a_bot_id)`,
`games(b_bot_id)`, `bot_sessions(bot_id)`.

**Derived metrics (not stored, computed in `scoring.py`):**
- A's *layout* score = `b_solved_round` (turns the opponent took to crack A). Higher
  is better; a `NULL` (opponent DNF) is the best possible layout.
- A's *solver* score = `a_solved_round`. Lower is better.

---

## 5. HTTP API (aiohttp, same process)

- `GET /`            -> stats site (static).
- `GET /projector`   -> full-screen live rankings board (static).
- `GET /api/rankings` -> leaderboards: per bot `{bot_id, player, name, games, wins,
  win_rate, avg_solver, avg_layout, combined, active}`.
- `GET /api/bots`, `GET /api/bot/{id}` -> bot list; detail with solver/layout
  histograms and per-opponent breakdown.
- `GET /api/pairings` -> pairwise winrate/avg-score matrix over active bots.
- `GET /api/tourneys` -> tourney list + status.
- `GET /events`      -> SSE: `tourney_start`, `game_complete`, `ranking_update`,
  `tourney_end` for the live projector.
- `GET /sync?since_session=&since_game=&since_move=` -> incremental JSON of new rows
  (append-only tables by id cursor; small tables re-sent whole). The sync client keeps
  a local SQLite mirror with this identical schema.
- `GET /db` -> one-shot download of a consistent snapshot (`VACUUM INTO`) of the DB.

---

## 6. Scoring & game-end semantics

Three competitions, all derivable from `games`:
- **Win rate** = wins / games. A win = solving in strictly fewer rounds than the
  opponent; equal rounds = **tie** (counts 0.5 in win rate). Forfeit = loss.
- **Solver** = mean `solved_round` over solved games (lower better); DNFs reported
  separately, excluded from the mean.
- **Layout** = mean opponent `solved_round` against this bot (higher better); opponent
  DNF counts as the best case (tracked separately).
- **Combined** (single projector rank) = configurable; default is the mean of the
  three per-competition percentile ranks. The three boards remain primary.

**Illegal move / timeout / drop (game-end):** the offending player takes an instant
loss (win rate) and a DNF (solver). **The game continues** so the opponent can finish
solving — preserving the opponent's valid solver score and the offender's layout
score. If both sides are out, the game ends. `end_reason` records the cause.
