"""In-process synchronous match between two bots — no server, no network.

Drives a `Game` by calling each bot directly. Used by the test harness and for quick
local checks. A bot that raises or returns a bad move just forfeits (mirroring how the
server treats a missing / illegal move), so a buggy bot can never hang the match.
"""

from .game import Game


def _safe_place(bot):
    try:
        return bot.place_ships()
    except Exception:
        return None  # invalid layout -> forfeit at game start


def _safe_move(bot, view):
    try:
        return bot.make_move(view)
    except Exception:
        return None  # missing move -> timeout-style forfeit


def play_game(factory_a, factory_b, config, game_id=0):
    """Play one game between two bot factories. Returns the finished `Game` (inspect
    `.result()` and `.moves_log`)."""
    cfg = dict(config)
    cfg["game_id"] = game_id
    bots = {"a": factory_a(cfg), "b": factory_b(cfg)}
    game = Game(_safe_place(bots["a"]), _safe_place(bots["b"]), config)
    while not game.over:
        cells = {side: _safe_move(bots[side], view) for side, view in game.views().items()}
        game.resolve(cells)
    return game
