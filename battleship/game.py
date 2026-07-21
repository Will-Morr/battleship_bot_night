"""Core Battleship rules: layout validation, shot resolution, and the synchronous
two-player Game state machine. Pure logic — no I/O, no networking.

Cells are (row, col) tuples internally and [row, col] lists on any boundary (views,
move logs). See DATA_CONTRACTS.md §1, §6 for the encodings and game-end semantics.
"""

from .config import fleet_sizes


def placement_cells(placement, sizes):
    """Cells a placement occupies. Returns None if the placement is malformed or names
    an unknown ship (caller treats that as an illegal layout)."""
    try:
        name = placement["name"]
        row = int(placement["row"])
        col = int(placement["col"])
        orient = placement["orientation"]
    except (TypeError, KeyError, ValueError):
        return None
    if name not in sizes:
        return None
    n = sizes[name]
    if orient == "H":
        return [(row, col + i) for i in range(n)]
    if orient == "V":
        return [(row + i, col) for i in range(n)]
    return None


def validate_layout(layout, rows, cols, sizes):
    """Check a layout against the fleet. Returns (ok, reason, cell_to_ship).

    Legal iff: exactly the fleet's ships appear once each at their correct size, every
    cell is in bounds, and no two ships overlap.
    """
    if not isinstance(layout, list):
        return False, "layout is not a list", {}
    cell_to_ship = {}
    seen = set()
    for placement in layout:
        if not isinstance(placement, dict) or "name" not in placement:
            return False, "malformed placement", {}
        name = placement["name"]
        if name not in sizes:
            return False, f"unknown ship {name!r}", {}
        if name in seen:
            return False, f"duplicate ship {name!r}", {}
        seen.add(name)
        cells = placement_cells(placement, sizes)
        if cells is None:
            return False, f"malformed placement for {name!r}", {}
        for cell in cells:
            r, c = cell
            if not (0 <= r < rows and 0 <= c < cols):
                return False, f"{name} out of bounds", {}
            if cell in cell_to_ship:
                return False, f"{name} overlaps {cell_to_ship[cell]}", {}
            cell_to_ship[cell] = name
    if seen != set(sizes):
        missing = set(sizes) - seen
        return False, f"missing ships {sorted(missing)}", {}
    return True, "ok", cell_to_ship


class Board:
    """One player's layout plus bookkeeping for shots the opponent fires at it."""

    def __init__(self, cell_to_ship):
        self.cell_to_ship = cell_to_ship
        self.ship_remaining = {}
        for name in cell_to_ship.values():
            self.ship_remaining[name] = self.ship_remaining.get(name, 0) + 1
        self.remaining = len(cell_to_ship)

    def receive_shot(self, cell):
        """Resolve a shot at `cell`. Returns (result, sunk_ship). Assumes the caller
        has already rejected out-of-bounds and repeat shots."""
        name = self.cell_to_ship.get(cell)
        if name is None:
            return "miss", None
        self.ship_remaining[name] -= 1
        self.remaining -= 1
        if self.ship_remaining[name] == 0:
            return "sunk", name
        return "hit", None

    @property
    def is_solved(self):
        return self.remaining == 0


def other(side):
    """The opposing side label."""
    return "b" if side == "a" else "a"


class Game:
    """A synchronous two-player game.

    Each side is 'solving', 'finished' (solved the opponent's board), or 'forfeited'.
    Every round the engine collects a move from each still-'solving' side via `views()`
    and applies them together with `resolve()`. The game is over once no side is still
    'solving'. Forfeits (illegal move / missed window) drop the offender but let the
    opponent keep solving, preserving solver and layout data.
    """

    SIDES = ("a", "b")

    def __init__(self, layout_a, layout_b, config):
        self.rows = config["rows"]
        self.cols = config["cols"]
        self.fleet = config["fleet"]
        self.sizes = fleet_sizes(self.fleet)
        self.layout = {"a": layout_a, "b": layout_b}

        self.current_round = 1
        self.status = {"a": "solving", "b": "solving"}
        self.solved_round = {"a": None, "b": None}
        self.forfeit_reason = {"a": None, "b": None}
        self.shots = {"a": set(), "b": set()}
        self.last_shot = {"a": None, "b": None}
        self.last_shot_round = {"a": 0, "b": 0}
        self.moves_log = []

        # A side with an illegal layout forfeits immediately. If either board is
        # missing the game cannot run (the opponent has nothing valid to solve), so it
        # ends at once — the one case where "continue for the opponent" cannot apply.
        self.board = {}
        for side in self.SIDES:
            ok, _reason, cell_to_ship = validate_layout(
                self.layout[side], self.rows, self.cols, self.sizes
            )
            if ok:
                self.board[side] = Board(cell_to_ship)
            else:
                self.board[side] = None
                self._forfeit(side, "illegal")
        self._layout_forfeit = any(self.board[s] is None for s in self.SIDES)

    # -- round loop -------------------------------------------------------------

    @property
    def over(self):
        if self._layout_forfeit:
            return True
        return all(self.status[s] != "solving" for s in self.SIDES)

    def needs_move(self, side):
        return not self.over and self.status[side] == "solving"

    def views(self):
        """Per-side observation for every side that must move this round."""
        if self.over:
            return {}
        return {s: self._view(s) for s in self.SIDES if self.status[s] == "solving"}

    def resolve(self, cells, response=None):
        """Apply one round. `cells` maps side -> [row, col] for moves that arrived;
        `response` maps side -> response_ms (optional). A still-'solving' side with no
        cell is forfeited as a timeout. Returns per-side result dicts.
        """
        if self.over:
            return {}
        response = response or {}
        results = {}
        for side in self.SIDES:
            if self.status[side] != "solving":
                continue
            cell = cells.get(side)
            if cell is None:
                self._forfeit(side, "timeout")
                results[side] = {"forfeit": True, "reason": "timeout"}
            else:
                results[side] = self._play(side, cell, response.get(side))
        self.current_round += 1
        return results

    def forfeit(self, side, reason="drop"):
        """Externally forfeit a side (e.g. the server saw its session drop)."""
        self._forfeit(side, reason)

    def result(self):
        """Final outcome. Call once the game is over. See DATA_CONTRACTS.md §6."""
        fa = self.status["a"] == "forfeited"
        fb = self.status["b"] == "forfeited"
        ra, rb = self.solved_round["a"], self.solved_round["b"]
        if not fa and not fb:
            # Over with neither forfeited => both finished.
            if ra == rb:
                winner, oa, ob = "tie", "tie", "tie"
            elif ra < rb:
                winner, oa, ob = "a", "win", "loss"
            else:
                winner, oa, ob = "b", "loss", "win"
        elif fa and fb:
            winner, oa, ob = None, "forfeit", "forfeit"
        elif fa:
            winner, oa, ob = "b", "forfeit", "win"
        else:
            winner, oa, ob = "a", "win", "forfeit"
        return {
            "winner": winner,
            "a_outcome": oa,
            "b_outcome": ob,
            "a_solved_round": ra,
            "b_solved_round": rb,
            "end_reason": self._end_reason(fa, fb),
            "total_rounds": max(self.last_shot_round["a"], self.last_shot_round["b"]),
        }

    # -- internals --------------------------------------------------------------

    def _view(self, side):
        opp = other(side)
        r = self.current_round
        # A *_last is shown only if that side actually shot in the previous round;
        # otherwise it is null (round 1, or after the side finished / forfeited).
        return {
            "round": r,
            "your_last": self.last_shot[side] if self.last_shot_round[side] == r - 1 else None,
            "opponent_last": self.last_shot[opp] if self.last_shot_round[opp] == r - 1 else None,
            "opponent_finished": self.status[opp] == "finished",
        }

    def _legal(self, side, cell):
        if not isinstance(cell, (list, tuple)) or len(cell) != 2:
            return False
        try:
            r, c = int(cell[0]), int(cell[1])
        except (TypeError, ValueError):
            return False
        if not (0 <= r < self.rows and 0 <= c < self.cols):
            return False
        return (r, c) not in self.shots[side]

    def _play(self, side, cell, response_ms):
        opp = other(side)
        if not self._legal(side, cell):
            self._forfeit(side, "illegal")
            return {"forfeit": True, "reason": "illegal"}
        c = (int(cell[0]), int(cell[1]))
        self.shots[side].add(c)
        result, sunk = self.board[opp].receive_shot(c)
        self.moves_log.append({
            "round": self.current_round,
            "side": side,
            "row": c[0],
            "col": c[1],
            "result": result,
            "sunk_ship": sunk,
            "response_ms": response_ms,
        })
        self.last_shot[side] = {"cell": [c[0], c[1]], "result": result, "sunk_ship": sunk}
        self.last_shot_round[side] = self.current_round
        if self.board[opp].is_solved:
            self.status[side] = "finished"
            self.solved_round[side] = self.current_round
        return {"result": result, "sunk_ship": sunk}

    def _forfeit(self, side, reason):
        if self.status[side] == "solving":
            self.status[side] = "forfeited"
            self.forfeit_reason[side] = reason

    def _end_reason(self, fa, fb):
        if not fa and not fb:
            return "complete"
        if fa and fb:
            return "both_forfeit"
        if fa:
            return f"a_{self.forfeit_reason['a']}"
        return f"b_{self.forfeit_reason['b']}"
