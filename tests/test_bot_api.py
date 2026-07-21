"""Tests for the bot API: cell helpers and the class/functional bot loaders."""

import pytest

from battleship.bot_api import all_cells, in_bounds, load_bot, neighbors

CLASS_BOT = '''
from battleship.bot_api import Bot
BOT = {"player": "alice", "bot": "cls"}
class Bot(Bot):
    def place_ships(self):
        return [{"name": "destroyer", "row": 0, "col": 0, "orientation": "H"}]
    def make_move(self, view):
        return [0, 0]
'''

FUNC_BOT = '''
BOT = {"player": "bob", "bot": "fn"}
def place_ships(config):
    return [{"name": "destroyer", "row": 1, "col": 1, "orientation": "V"}]
def make_move(view, mem):
    mem["n"] = mem.get("n", 0) + 1
    return [mem["n"], 0]
'''

NO_META = 'x = 1\n'


def write(tmp_path, name, src):
    p = tmp_path / name
    p.write_text(src)
    return str(p)


def test_helpers():
    assert len(all_cells(10, 10)) == 100
    assert in_bounds((0, 0), 3, 3) and not in_bounds((3, 0), 3, 3)
    assert set(neighbors((0, 0), 3, 3)) == {(1, 0), (0, 1)}
    assert len(neighbors((1, 1), 3, 3)) == 4


def test_load_class_bot(tmp_path):
    meta, factory = load_bot(write(tmp_path, "cls.py", CLASS_BOT))
    assert meta == {"player": "alice", "bot": "cls"}
    bot = factory({"rows": 3, "cols": 3, "fleet": []})
    assert bot.place_ships()[0]["name"] == "destroyer"
    assert bot.make_move({"round": 1}) == [0, 0]


def test_load_functional_bot_has_per_game_state(tmp_path):
    meta, factory = load_bot(write(tmp_path, "fn.py", FUNC_BOT))
    assert meta["player"] == "bob"
    bot = factory({"rows": 3, "cols": 3, "fleet": []})
    assert bot.make_move({}) == [1, 0]
    assert bot.make_move({}) == [2, 0]        # mem persists within an instance
    fresh = factory({"rows": 3, "cols": 3, "fleet": []})
    assert fresh.make_move({}) == [1, 0]      # a new game starts clean


def test_load_rejects_missing_meta(tmp_path):
    with pytest.raises(ValueError):
        load_bot(write(tmp_path, "bad.py", NO_META))
