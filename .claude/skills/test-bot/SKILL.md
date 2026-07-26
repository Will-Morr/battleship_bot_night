---
name: test-bot
description: Test a Battleship bot file locally against the two example bots before deploying it live. Use for "test my bot", "test-bot", "check my battleship bot", "run bot vs examples", "debug my bot", "why does my bot forfeit". Runs test/test_bot.py and reports win-rate / solver / layout / faults, and can write fault replays and heatmaps.
---

# Test a Battleship bot

Runs a bot file through N local games against both example bots (`bots/random_bot.py`
and `bots/ordered_bot.py`) with no server needed, and reports how it did. With
`--debug DIR` it also writes fault replays and heatmaps for manual debugging.

## Steps

1. Identify the bot file to test from the user's request (a path like `bots/mybot.py`).
   If none is given, ask which file.
2. Run the harness from the repo root, preferring the project virtualenv if present:

   ```bash
   ./.venv/bin/python test/test_bot.py <BOTFILE> --games 200 --seed 1
   ```

   If there is no `.venv`, use `python3 test/test_bot.py <BOTFILE> --games 200`.
   Pass through any `--games` / `--seed` the user specified.
3. Report the printed tables and call out:
   - **FAULTS > 0** — the bot crashes, moves illegally, or hands in a bad layout. Each
     fault is a game lost live; fix these before anything else. The report names the
     kind, round, and reason for every one.
   - **SOLVER** — rounds to clear an opponent (lower is better).
   - **LAYOUT** — rounds opponents took to crack this bot (higher is better), plus how
     often the layout went uncracked.
   - **win%** vs each example.
   - **TIMING** — worth raising only if a move takes an appreciable slice of the live
     budget (250 ms for a whole batch of moves).
4. If there are faults, or the user asks to debug, dig in:

   ```bash
   ./.venv/bin/python test/test_bot.py <BOTFILE> --games 100 --seed 1 --debug debug/
   ```

   Then read `debug/faults/<opponent>-game-NNNN.txt` for the failing games — each has
   the traceback, the `view` the bot was answering, both boards, and the move log.
   Reproduce one game with the command printed at the top of that replay:

   ```bash
   ./.venv/bin/python test/test_bot.py <BOTFILE> --opponent random --game 17 --seed 1
   ```

   `debug/heatmaps/*.txt` (ascii, readable in the terminal) show where the bot places
   ships, where and in what order it shoots, and where it hits — use them to explain
   *strategy* problems (firing a fixed order, clustering ships in one corner).
   `debug/index.html` is the same report with SVG heatmaps for the user to open.
5. If the bot fails to load, surface the loader error (usually a missing
   `BOT = {"player": ..., "bot": ...}` dict or no `Bot` class / `place_ships`+`make_move`).

Keep the summary short: the key numbers plus a one-line verdict (ready to deploy, or
fix the faults first). Quote a fault's exact reason rather than paraphrasing it.
