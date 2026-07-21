"""Example bot: random placement, and fires at every cell in row-major order.

A deterministic baseline — it always takes rows*cols shots minus a bit to clear a board.
"""

from battleship.bot_api import Bot, all_cells, random_layout

# Distinct player from random_bot (see that file) so the two examples can play.
BOT = {"player": "example-b", "bot": "ordered"}


class Bot(Bot):
    def __init__(self, config):
        super().__init__(config)
        self.cells = all_cells(self.rows, self.cols)
        self.i = 0

    def place_ships(self):
        return random_layout(self.config)

    def make_move(self, view):
        cell = self.cells[self.i]
        self.i += 1
        return list(cell)
