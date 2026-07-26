"""Example bot: fixed placement, and fires at every cell in row-major order.

A fully deterministic baseline — it always clears a board within rows*cols shots.
"""

from battleship.bot_api import Bot

# Distinct player from random_bot (see that file) so the two examples can play.
BOT = {"player": "example-b", "bot": "ordered"}


class Bot(Bot):
    def __init__(self, config):
        # The base class just stores config and unpacks self.rows / self.cols /
        # self.fleet from it.
        super().__init__(config)
        self.cells = [(r, c) for r in range(self.rows) for c in range(self.cols)]
        self.i = 0

    def place_ships(self):
        # A layout is one {name, row, col, orientation} dict per ship in the fleet.
        # Simplest legal layout there is: each ship horizontal, in its own row,
        # flush against the left edge. No overlaps by construction.
        return [
            {"name": ship["name"], "row": row, "col": 0, "orientation": "H"}
            for row, ship in enumerate(self.fleet)
        ]

    def make_move(self, view):
        # `view` is the delta since your last move (this bot ignores it entirely).
        # Return the next target as [row, col].
        cell = self.cells[self.i]
        self.i += 1
        return list(cell)
