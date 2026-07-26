"""Example bot: random placement, and fires at uniformly random cells.

The two example bots are also templates. A bot file needs a BOT identity dict and
either a `Bot` subclass (one instance per game) or place_ships/make_move functions.
Everything else here is plain Python, spelled out rather than imported, so this file
is all you need to read to write your own.
"""

import random

from battleship.bot_api import Bot

# Distinct player from ordered_bot so the two examples can be matched against each
# other (same-player bots are never paired).
BOT = {"player": "example-a", "bot": "random"}


class Bot(Bot):
    def __init__(self, config):
        # The base class just stores config and unpacks self.rows / self.cols /
        # self.fleet from it.
        super().__init__(config)
        # Pre-shuffle every cell; popping gives distinct random shots (never a repeat,
        # so never an illegal move).
        self.unfired = [(r, c) for r in range(self.rows) for c in range(self.cols)]
        random.shuffle(self.unfired)

    def place_ships(self):
        # A layout is one {name, row, col, orientation} dict per ship in the fleet.
        # Keep re-rolling each ship until it fits on the board without overlapping.
        occupied = set()
        layout = []
        for ship in self.fleet:
            name, size = ship["name"], ship["size"]
            while True:
                orientation = random.choice(("H", "V"))
                if orientation == "H":
                    row = random.randrange(self.rows)
                    col = random.randrange(self.cols - size + 1)
                    cells = [(row, col + i) for i in range(size)]
                else:
                    row = random.randrange(self.rows - size + 1)
                    col = random.randrange(self.cols)
                    cells = [(row + i, col) for i in range(size)]
                if occupied.isdisjoint(cells):
                    occupied.update(cells)
                    layout.append(
                        {"name": name, "row": row, "col": col, "orientation": orientation}
                    )
                    break
        return layout

    def make_move(self, view):
        # `view` is the delta since your last move (this bot ignores it entirely).
        # Return the next target as [row, col].
        return list(self.unfired.pop())
