"""Rules tests for battleship.game — validation, shot resolution, and the Game state
machine (solving, ties, forfeit-continues-for-opponent, illegal layouts, timeouts)."""

import pytest

from battleship.config import make_config
from battleship.game import Board, Game, validate_layout

# A tiny deterministic world: 3x3 board, a single 2-cell destroyer.
MINI = make_config(rows=3, cols=3, fleet=[{"name": "destroyer", "size": 2}])
SIZES = {"destroyer": 2}


def ship(row, col, orient="H", name="destroyer"):
    return [{"name": name, "row": row, "col": col, "orientation": orient}]


# -- validate_layout -----------------------------------------------------------

def test_valid_layout():
    ok, reason, cells = validate_layout(ship(0, 0, "H"), 3, 3, SIZES)
    assert ok, reason
    assert set(cells) == {(0, 0), (0, 1)}


def test_layout_out_of_bounds():
    ok, reason, _ = validate_layout(ship(0, 2, "H"), 3, 3, SIZES)  # (0,2),(0,3)
    assert not ok and "out of bounds" in reason


def test_layout_vertical():
    ok, _, cells = validate_layout(ship(1, 2, "V"), 3, 3, SIZES)
    assert ok and set(cells) == {(1, 2), (2, 2)}


def test_layout_overlap():
    two = [
        {"name": "carrier", "row": 0, "col": 0, "orientation": "H"},
        {"name": "destroyer", "row": 0, "col": 1, "orientation": "H"},
    ]
    ok, reason, _ = validate_layout(two, 10, 10, {"carrier": 5, "destroyer": 2})
    assert not ok and "overlap" in reason


def test_layout_missing_ship():
    ok, reason, _ = validate_layout(ship(0, 0), 3, 3, {"destroyer": 2, "cruiser": 3})
    assert not ok and "missing" in reason


def test_layout_bad_orientation():
    ok, reason, _ = validate_layout(
        [{"name": "destroyer", "row": 0, "col": 0, "orientation": "X"}], 3, 3, SIZES
    )
    assert not ok


def test_layout_duplicate_ship():
    dup = ship(0, 0) + [{"name": "destroyer", "row": 2, "col": 0, "orientation": "H"}]
    ok, reason, _ = validate_layout(dup, 3, 3, SIZES)
    assert not ok and "duplicate" in reason


# -- Board ---------------------------------------------------------------------

def test_board_shot_resolution():
    _, _, cells = validate_layout(ship(0, 0), 3, 3, SIZES)
    board = Board(cells)
    assert board.receive_shot((2, 2)) == ("miss", None)
    assert board.receive_shot((0, 0)) == ("hit", None)
    assert not board.is_solved
    assert board.receive_shot((0, 1)) == ("sunk", "destroyer")
    assert board.is_solved


# -- Game ----------------------------------------------------------------------

def new_game(a=ship(0, 0), b=ship(2, 1)):
    return Game(a, b, MINI)


def test_tie_when_both_finish_same_round():
    g = new_game()
    g.resolve({"a": [2, 1], "b": [0, 0]})   # round 1: both hit
    g.resolve({"a": [2, 2], "b": [0, 1]})   # round 2: both sink -> both finish
    assert g.over
    r = g.result()
    assert r["winner"] == "tie"
    assert r["a_solved_round"] == r["b_solved_round"] == 2
    assert r["end_reason"] == "complete"
    assert r["total_rounds"] == 2


def test_faster_solver_wins():
    g = new_game()
    g.resolve({"a": [2, 1], "b": [1, 1]})   # r1: a hit, b miss
    g.resolve({"a": [2, 2], "b": [0, 0]})   # r2: a sinks (finishes), b hit
    assert not g.over                        # b still solving
    assert g.views().keys() == {"b"}         # only b is asked to move
    g.resolve({"b": [0, 1]})                 # r3: b sinks
    assert g.over
    r = g.result()
    assert r["winner"] == "a"
    assert (r["a_solved_round"], r["b_solved_round"]) == (2, 3)
    assert r["a_outcome"] == "win" and r["b_outcome"] == "loss"


def test_view_deltas():
    g = new_game()
    v = g.views()
    assert v["a"]["your_last"] is None and v["a"]["opponent_last"] is None  # round 1
    g.resolve({"a": [2, 1], "b": [0, 0]})
    v = g.views()
    assert v["a"]["your_last"] == {"cell": [2, 1], "result": "hit", "sunk_ship": None}
    assert v["a"]["opponent_last"] == {"cell": [0, 0], "result": "hit", "sunk_ship": None}
    assert v["a"]["opponent_finished"] is False


def test_illegal_move_forfeits_but_game_continues():
    g = new_game()
    g.resolve({"a": [2, 1], "b": [1, 0]})   # r1: a hit, b miss
    g.resolve({"a": [2, 1], "b": [0, 0]})   # r2: a repeats -> illegal forfeit; b hit
    assert g.status["a"] == "forfeited"
    assert not g.over                        # b keeps solving
    v = g.views()
    assert v.keys() == {"b"}
    # opponent (a) didn't shoot last round, and hasn't finished:
    assert v["b"]["opponent_last"] is None
    assert v["b"]["opponent_finished"] is False
    g.resolve({"b": [0, 1]})                 # r3: b sinks a's board
    r = g.result()
    assert r["winner"] == "b"
    assert r["a_outcome"] == "forfeit" and r["b_outcome"] == "win"
    assert r["end_reason"] == "a_illegal"
    assert r["a_solved_round"] is None and r["b_solved_round"] == 3


def test_timeout_forfeits_missing_side():
    g = new_game()
    g.resolve({"b": [0, 0]})                 # a submits nothing -> timeout forfeit
    assert g.status["a"] == "forfeited"
    assert g.forfeit_reason["a"] == "timeout"
    g.resolve({"b": [0, 1]})                 # b finishes
    r = g.result()
    assert r["winner"] == "b" and r["end_reason"] == "a_timeout"


def test_external_drop_forfeit():
    g = new_game()
    g.forfeit("b", "drop")
    g.resolve({"a": [2, 1]})
    g.resolve({"a": [2, 2]})                 # a solves b's board
    r = g.result()
    assert r["winner"] == "a" and r["end_reason"] == "b_drop"


def test_both_forfeit_no_winner():
    g = new_game()
    g.forfeit("a", "drop")
    g.forfeit("b", "timeout")
    assert g.over
    r = g.result()
    assert r["winner"] is None
    assert r["a_outcome"] == "forfeit" and r["b_outcome"] == "forfeit"
    assert r["end_reason"] == "both_forfeit"


def test_illegal_layout_ends_immediately():
    bad = ship(0, 2, "H")                    # (0,2),(0,3) -> off a 3-wide board
    g = Game(bad, ship(2, 1), MINI)
    assert g.over                            # opponent has no valid board to solve
    assert g.views() == {}
    r = g.result()
    assert r["winner"] == "b" and r["end_reason"] == "a_illegal"
    assert r["a_solved_round"] is None and r["b_solved_round"] is None


def test_moves_log_records_each_shot():
    g = new_game()
    g.resolve({"a": [2, 1], "b": [0, 0]}, response={"a": 1.5, "b": 2.0})
    log = g.moves_log
    assert len(log) == 2
    a_move = next(m for m in log if m["side"] == "a")
    assert a_move["round"] == 1 and a_move["row"] == 2 and a_move["col"] == 1
    assert a_move["result"] == "hit" and a_move["response_ms"] == 1.5
