"""Example bot: random placement, and fires at uniformly random cells.

The two example bots are also templates. A bot file needs a BOT identity dict and
either a `Bot` subclass (one instance per game) or place_ships/make_move functions.
"""

import random

from battleship.bot_api import Bot, all_cells, random_layout

# Distinct player from ordered_bot so the two examples can be matched against each
# other (same-player bots are never paired).
BOT = {"player": "example-a", "bot": "random"}


class Bot(Bot):
    def __init__(self, config):
        super().__init__(config)
        # Pre-shuffle every cell; popping gives distinct random shots (never a repeat,
        # so never an illegal move).
        self.unfired = all_cells(self.rows, self.cols)
        random.shuffle(self.unfired)

    def place_ships(self):
        return random_layout(self.config)

    def make_move(self, view):
        return list(self.unfired.pop())
