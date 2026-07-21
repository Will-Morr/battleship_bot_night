---
name: test-bot
description: Test a Battleship bot file locally against the two example bots before deploying it live. Use for "test my bot", "test-bot", "check my battleship bot", "run bot vs examples". Runs test/test_bot.py and reports win-rate / solver / layout and any forfeits.
---

# Test a Battleship bot

Runs a bot file through N local games against both example bots (`bots/random_bot.py`
and `bots/ordered_bot.py`) with no server needed, and reports how it did.

## Steps

1. Identify the bot file to test from the user's request (a path like `bots/mybot.py`).
   If none is given, ask which file.
2. Run the harness from the repo root, preferring the project virtualenv if present:

   ```bash
   ./.venv/bin/python test/test_bot.py <BOTFILE> --games 200 --seed 1
   ```

   If there is no `.venv`, use `python3 test/test_bot.py <BOTFILE> --games 200`.
   Pass through any `--games` / `--seed` the user specified.
3. Report the printed table and call out:
   - **forfeit > 0** — the bot is making illegal moves or crashing. This is a bug that
     would lose games live; investigate before deploying. Common causes: repeating a
     shot, returning an out-of-bounds/malformed cell, or an unhandled exception.
   - **solver↓** — average turns to clear an opponent (lower is better).
   - **layout↑** — average turns opponents took to crack this bot (higher is better).
   - **win%** — vs each example bot.
4. If the bot fails to load, surface the loader error (it usually means a missing
   `BOT = {"player": ..., "bot": ...}` dict or no `Bot` class / `place_ships`+`make_move`).

Keep the summary short: the table plus a one-line verdict (ready to deploy, or fix the
forfeits first).
