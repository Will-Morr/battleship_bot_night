"""What bot authors import, plus the loader the runner and test harness use.

A bot file defines a `BOT` identity dict and its logic as either a `Bot` subclass (one
instance per game, canonical) or module-level `place_ships`/`make_move` functions. See
DATA_CONTRACTS.md §2.
"""

import importlib.util


class Bot:
    """Subclass this for the class form. One instance is created per game."""

    def __init__(self, config):
        self.config = config
        self.rows = config["rows"]
        self.cols = config["cols"]
        self.fleet = config["fleet"]

    def place_ships(self):
        """Return a layout: a list of {name, row, col, orientation} placements."""
        raise NotImplementedError

    def make_move(self, view):
        """Return the next target as [row, col]. `view` is the per-round delta."""
        raise NotImplementedError


# -- cell helpers (handy for bot authors) --------------------------------------

def all_cells(rows, cols):
    """Every board cell in row-major order."""
    return [(r, c) for r in range(rows) for c in range(cols)]


def in_bounds(cell, rows, cols):
    r, c = cell
    return 0 <= r < rows and 0 <= c < cols


def neighbors(cell, rows, cols):
    """Orthogonal in-bounds neighbours — the classic hunt/target follow-ups."""
    r, c = cell
    out = []
    for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nr, nc = r + dr, c + dc
        if 0 <= nr < rows and 0 <= nc < cols:
            out.append((nr, nc))
    return out


# -- loading a bot file --------------------------------------------------------

class _FunctionalBot:
    """Adapter that wraps module-level place_ships/make_move into the class interface,
    giving each game its own `mem` dict for state."""

    def __init__(self, config, place, move):
        self.config = config
        self._place = place
        self._move = move
        self.mem = {}

    def place_ships(self):
        return self._place(self.config)

    def make_move(self, view):
        return self._move(view, self.mem)


def load_bot_module(path):
    """Import a bot file by path as an isolated module."""
    spec = importlib.util.spec_from_file_location("player_bot", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load bot file {path!r}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_bot(path):
    """Return (meta, factory) for a bot file. `meta` is the BOT dict; `factory(config)`
    builds a fresh per-game instance exposing place_ships()/make_move(view)."""
    module = load_bot_module(path)
    meta = getattr(module, "BOT", None)
    if not isinstance(meta, dict) or "player" not in meta or "bot" not in meta:
        raise ValueError("bot file must define BOT = {'player': ..., 'bot': ...}")

    bot_cls = getattr(module, "Bot", None)
    if isinstance(bot_cls, type) and bot_cls is not Bot:
        return meta, (lambda config: bot_cls(config))

    place = getattr(module, "place_ships", None)
    move = getattr(module, "make_move", None)
    if callable(place) and callable(move):
        return meta, (lambda config: _FunctionalBot(config, place, move))

    raise ValueError("bot file must define a `Bot` subclass or place_ships/make_move")
