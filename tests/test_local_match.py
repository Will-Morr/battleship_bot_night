"""End-to-end (in-process) games between the example bots — exercises the whole game
loop with real bots and checks the results are sane."""

import os
import random

from battleship.bot_api import load_bot
from battleship.config import default_config, total_ship_cells
from battleship.local_match import play_game

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RANDOM_BOT = os.path.join(REPO, "bots", "random_bot.py")
ORDERED_BOT = os.path.join(REPO, "bots", "ordered_bot.py")


def test_example_bots_play_clean_games():
    random.seed(0)
    config = default_config()
    _, rand = load_bot(RANDOM_BOT)
    _, ordered = load_bot(ORDERED_BOT)
    min_turns = total_ship_cells(config["fleet"])       # 17: need at least this many hits
    max_turns = config["rows"] * config["cols"]         # 100: whole board

    for i in range(30):
        result = play_game(rand, ordered, config, game_id=i).result()
        # Legal bots never forfeit.
        assert result["end_reason"] == "complete", result
        assert result["a_outcome"] != "forfeit" and result["b_outcome"] != "forfeit"
        assert result["winner"] in ("a", "b", "tie")
        for side in ("a_solved_round", "b_solved_round"):
            assert min_turns <= result[side] <= max_turns, result


def test_ordered_bot_is_deterministic_solver():
    # The ordered bot fires row-major, so it always clears a board on the turn it
    # reaches that board's last ship cell — never more than 100 turns.
    random.seed(1)
    config = default_config()
    _, ordered = load_bot(ORDERED_BOT)
    _, rand = load_bot(RANDOM_BOT)
    result = play_game(ordered, rand, config).result()
    assert result["a_solved_round"] <= config["rows"] * config["cols"]
