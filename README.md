# Battleship Bot Tournament

**Vibe Code Warning for this whole repo**

A real-time, continuous Battleship tournament for bot game night. Players write bots and
deploy them live; the server runs back-to-back **tourneys** (batches of games), and a
projector shows the rankings update in real time.

Games are played **synchronously**: both players submit ship layouts, then every round
both fire at once and each learns their own result and their opponent's shot. Games run
until *both* players have solved each other's boards, which splits the event into three
competitions:

- **Win rate** — did you finish (sink all their ships) before they finished yours?
- **Solver** — how few turns did *you* take to clear the opponent (lower is better)?
- **Layout** — how many turns did opponents take to crack *your* layout (higher is better)?

Win rate is noisy; solver and layout are high-resolution, so we play a lot of games.

The full interface spec is in [`DATA_CONTRACTS.md`](DATA_CONTRACTS.md); the design and
rationale are in [`PLAN.md`](PLAN.md).

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # pyzmq + aiohttp
```

A bot **runner** only needs `pyzmq`. Bots themselves and the local test harness need
nothing beyond the standard library. Works on Linux, WSL, and macOS.

## Write a bot

A bot is a single file: a `BOT` identity dict plus a `Bot` class (one instance is created
per game, so it can keep that game's state).

```python
from battleship.bot_api import Bot, all_cells, random_layout

BOT = {"player": "your-name", "bot": "my-hunter"}

class Bot(Bot):
    def __init__(self, config):          # config: rows, cols, fleet, move_time_ms
        super().__init__(config)
        self.unfired = all_cells(self.rows, self.cols)

    def place_ships(self):               # once per game -> a layout
        return random_layout(self.config)

    def make_move(self, view):           # each round -> [row, col]
        return list(self.unfired.pop())
```

`make_move(view)` receives the per-round delta:

```python
view = {
  "round": 7,
  "your_last":     {"cell": [3, 4], "result": "hit", "sunk_ship": None},   # or None
  "opponent_last": {"cell": [8, 1], "result": "miss", "sunk_ship": None},  # or None
  "opponent_finished": False,
}
```

Return `[row, col]` (0-indexed). Rules: don't shoot the same cell twice, stay in bounds,
and answer within the round deadline (250 ms for the whole batch of your games) — any of
those loses the offending game. See the two examples in [`bots/`](bots/): `random_bot.py`
and `ordered_bot.py`. (A functional form — module-level `place_ships(config)` and
`make_move(view, mem)` — also works.)

> Bots from the **same player** are never matched against each other. Give each of your
> bots the same `player` and a distinct `bot` name.

## Test before you deploy

```bash
python test/test_bot.py bots/random_bot.py --games 200
```

Runs your bot against both example bots in-process (no server, no network) and prints
exact results: W-T-L and win%, the full solver and layout distributions (avg / median /
min / max / sd), your own compute time per move, and **every fault** — each crash,
illegal move, and illegal layout named with its round and reason. Faults should be 0;
each one is a game lost live.

Debug a bot without ever starting a server:

```bash
python test/test_bot.py mybot.py --debug debug/     # replays + heatmaps
```

`debug/` gets `index.html` (open it), `summary.txt`, `games.csv` with one row per game,
`faults/` with a full replay of every faulted game (traceback, the `view` your bot was
answering, both boards, the move log), and `heatmaps/` — where your ships sit, where you
shoot, in what order, and where you hit — as `.svg` and as ascii `.txt`.

Every game is seeded from `(seed, opponent, game index)`, so any game the report
mentions replays on its own:

```bash
python test/test_bot.py mybot.py --opponent random --game 17 --seed 1
```

(There is also a `test-bot` skill that wraps all this.)

## Deploy a bot (live)

```bash
python -m battleship.run_bot path/to/mybot.py --server <host>:5555
# multi-threaded move computation (helps bots that release the GIL / do I/O):
python -m battleship.run_bot path/to/mybot.py --server <host>:5555 --mode threads
```

The runner registers the bot (uploading its full source for later analysis), then plays
every tourney until you stop it (Ctrl-C). It re-registers automatically if the connection
drops. Restarting mid-tourney forfeits that tourney's in-flight games and rejoins the
next one — cheap, because tourneys are short.

## Run the server

```bash
python -m battleship.server --db battleship.db --zmq-port 5555 --http-port 8080
# knobs: --target <games/bot/tourney>  --gap-ms <pause between tourneys>
```

One process runs the game server (ZeroMQ), the tournament loop, the SQLite writer, and
the web/analytics HTTP server.

## Watch the rankings

With the server running:

- **Projector board:** `http://<host>:8080/projector` — live leaderboard, auto-updating.
- **Stats site:** `http://<host>:8080/` — rankings, per-bot score distributions, and the
  head-to-head win% matrix.

## Get the data

Every game, layout, and move is stored forever in SQLite. Keep a local mirror that syncs
itself so your analysis never adds load to the server:

```bash
python -m battleship.sync_db --server <host>:8080 --db mirror.db     # syncs every 2s
```

Then query `mirror.db` with any SQLite tool. One-shot full download: `GET /db`. Schema:
[`DATA_CONTRACTS.md` §4](DATA_CONTRACTS.md). Joins use `uuid` columns; `seq` is the sync
cursor.

## Repository layout

```
battleship/     server, runner, and shared logic (game rules, protocol, db, scoring)
bots/           the two example bots (also templates)
test/           test_bot.py — local bot tester (+ debug_report.py, its output)
tests/          pytest suite
web/            projector + stats site (static, no build step)
```

## Run the tests

```bash
pip install pytest
python -m pytest -q
```
