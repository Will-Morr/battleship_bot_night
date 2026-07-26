"""The local bot tester (test/test_bot.py): fault detection, stats, debug folder.

Faults are the whole point of the tool — a bot that crashes, repeats a shot, or hands
in an illegal layout must be named exactly, not just counted as a forfeit.
"""

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "test"))

import debug_report as dbg          # noqa: E402
import test_bot as tester           # noqa: E402

from battleship.bot_api import load_bot, random_layout   # noqa: E402
from battleship.config import default_config            # noqa: E402

RANDOM_BOT = os.path.join(REPO, "bots", "random_bot.py")
ORDERED_BOT = os.path.join(REPO, "bots", "ordered_bot.py")


def _factories():
    _, rand = load_bot(RANDOM_BOT)
    _, ordered = load_bot(ORDERED_BOT)
    return rand, ordered


def _broken(behaviour):
    """A bot factory that plays legally until `behaviour` says otherwise."""
    class _Bot:
        def __init__(self, config):
            self.config = config
            self.rows, self.cols = config["rows"], config["cols"]
            self.cells = [(r, c) for r in range(self.rows) for c in range(self.cols)]
            self.i = 0

        def place_ships(self):
            if behaviour == "layout_crash":
                raise RuntimeError("no idea where to put these")
            if behaviour == "layout_illegal":
                return [{"name": s["name"], "row": 0, "col": 0, "orientation": "H"}
                        for s in self.config["fleet"]]
            return random_layout(self.config)

        def make_move(self, view):
            if view["round"] == 5:
                if behaviour == "move_crash":
                    raise ValueError("boom")
                if behaviour == "move_repeat":
                    return list(self.cells[0])
                if behaviour == "move_off_board":
                    return [self.rows, 0]
                if behaviour == "move_garbage":
                    return "up a bit"
            last = view.get("your_last")
            if last is not None and list(last["cell"]) == list(self.cells[self.i]):
                self.i += 1
            return list(self.cells[self.i])
    return _Bot


def _play(behaviour, opponent=None):
    rand, _ = _factories()
    return tester.play_instrumented(_broken(behaviour), opponent or rand,
                                    default_config(), "seed:x:0")


def test_clean_bot_reports_no_fault():
    played = _play("none")
    assert played["fault"] is None and played["opponent_fault"] is None
    assert played["result"]["end_reason"] == "complete"
    assert len(played["move_ms"]) == played["result"]["a_solved_round"]


@pytest.mark.parametrize("behaviour,phase,kind,fragment", [
    ("layout_crash", "place_ships", "crash", "RuntimeError"),
    ("layout_illegal", "place_ships", "illegal layout", "overlaps"),
    ("move_crash", "make_move", "crash", "ValueError: boom"),
    ("move_repeat", "make_move", "illegal move", "already fired"),
    ("move_off_board", "make_move", "illegal move", "off the 10x10 board"),
    ("move_garbage", "make_move", "illegal move", "not a [row, col] pair"),
])
def test_each_fault_kind_is_named(behaviour, phase, kind, fragment):
    played = _play(behaviour)
    fault = played["fault"]
    assert fault is not None, behaviour
    assert (fault["phase"], fault["kind"]) == (phase, kind)
    assert fragment in fault["detail"]
    assert played["result"]["a_outcome"] == "forfeit"
    if phase == "make_move":
        assert fault["round"] == 5
        assert fault["view"]["round"] == 5
    if kind == "crash":
        assert "Traceback" in fault["traceback"]


def test_the_opponent_keeps_solving_after_our_fault():
    # A forfeit must not cost the example bot its layout data — that is what makes a
    # faulted game still worth reading.
    played = _play("move_crash")
    assert played["result"]["b_solved_round"] is not None


def test_series_and_summary_over_clean_games():
    rand, ordered = _factories()
    records = tester.run_series(rand, ordered, default_config(), "ordered", 5, seed=3)
    assert [r["index"] for r in records] == [0, 1, 2, 3, 4]
    assert [r["seed_key"] for r in records] == [f"3:ordered:{i}" for i in range(5)]
    s = tester.summarize(records)
    assert s["games"] == 5 and s["forfeits"] == 0
    assert s["wins"] + s["ties"] + s["losses"] == 5
    assert s["win_rate"] == (s["wins"] + 0.5 * s["ties"]) / 5
    assert 17 <= s["solver"]["min"] <= s["solver"]["max"] <= 100
    assert s["solver"]["n"] == 5 and s["layout"]["n"] == 5


def test_same_seed_replays_the_same_game():
    rand, ordered = _factories()
    config = default_config()
    first = tester.run_series(rand, ordered, config, "ordered", 1, seed=11, first_game=7)[0]
    again = tester.run_series(rand, ordered, config, "ordered", 1, seed=11, first_game=7)[0]
    assert first["result"] == again["result"]
    assert first["shots"] == again["shots"]
    # ... and a different game index is a different game.
    other = tester.run_series(rand, ordered, config, "ordered", 1, seed=11, first_game=8)[0]
    assert other["shots"] != first["shots"]


def test_report_names_every_fault_and_counts_games():
    config = default_config()
    rand, _ = _factories()
    records = tester.run_series(_broken("move_repeat"), rand, config, "random", 3, seed=1)
    text = tester.report({"player": "p", "bot": "b"}, "b.py", config, 1,
                         {"random": records}, 3)
    assert "FAULTS   3 of 3 games" in text
    assert "illegal move in make_move" in text
    assert "already fired" in text
    assert "VERDICT  fix the faults first" in text


def test_report_on_a_clean_bot_says_so():
    config = default_config()
    rand, ordered = _factories()
    records = tester.run_series(rand, ordered, config, "ordered", 2, seed=1)
    text = tester.report({"player": "p", "bot": "b"}, "b.py", config, 1,
                         {"ordered": records}, 2)
    assert "FAULTS   0 of 2 games" in text
    assert "VERDICT  no faults" in text


def test_heatmap_grids_add_up():
    config = default_config()
    rand, ordered = _factories()
    records = tester.run_series(rand, ordered, config, "ordered", 4, seed=2)
    grids = tester.heatmap_grids(records, config)
    placed, raw_placed, layouts = grids["placements"]
    assert layouts == 4
    # 17 ship cells per layout, expressed as a % of layouts covering each cell.
    assert sum(sum(row) for row in raw_placed) == 17 * 4
    assert all(0 <= v <= 100 for row in placed for v in row)
    shot_pct, raw_shot, n = grids["guesses"]
    assert n == 4
    assert sum(sum(row) for row in raw_shot) == sum(len(r["shots"]) for r in records)
    order = grids["guess-order"][0]
    for r in range(10):
        for c in range(10):
            assert (order[r][c] is None) == (raw_shot[r][c] == 0)
    hits = grids["hits"][1]
    assert sum(sum(row) for row in hits) == sum(
        1 for rec in records for _, _, _, res in rec["shots"] if res in ("hit", "sunk"))


def test_debug_folder_has_replays_heatmaps_and_csv(tmp_path):
    config = default_config()
    rand, _ = _factories()
    records = tester.run_series(_broken("move_crash"), rand, config, "random", 2, seed=5)
    text = tester.report({"player": "p", "bot": "b"}, "b.py", config, 5, {"random": records}, 2)
    folder = tmp_path / "dbg"
    n_faults = tester.write_debug(str(folder), {"player": "p", "bot": "b"}, "b.py",
                                  config, {"random": records}, text)

    assert n_faults == 2
    assert (folder / "summary.txt").read_text().startswith("bot    : p/b")
    for slug in ("placements", "guesses", "guess-order", "hits"):
        assert (folder / "heatmaps" / f"{slug}.svg").read_text().startswith("<svg")
        assert (folder / "heatmaps" / f"{slug}.txt").read_text()
    index = (folder / "index.html").read_text()
    assert "<svg" in index and "random-game-0000.txt" in index

    replay = (folder / "faults" / "random-game-0000.txt").read_text()
    assert "--opponent random --game 0 --seed 5" in replay
    assert "ValueError: boom" in replay and "Traceback" in replay
    assert "move log" in replay

    rows = (folder / "games.csv").read_text().splitlines()
    assert rows[0].startswith("opponent,game,seed_key")
    assert len(rows) == 3
    assert "5:random:0" in rows[1] and "crash" in rows[1]


def test_debug_folder_for_a_clean_run_has_no_faults_dir(tmp_path):
    config = default_config()
    rand, ordered = _factories()
    records = tester.run_series(rand, ordered, config, "ordered", 2, seed=6)
    text = tester.report({"player": "p", "bot": "b"}, "b.py", config, 6, {"ordered": records}, 2)
    folder = tmp_path / "dbg"
    assert tester.write_debug(str(folder), {"player": "p", "bot": "b"}, "b.py", config,
                              {"ordered": records}, text) == 0
    assert not (folder / "faults").exists()
    assert "None &mdash; no illegal moves" in (folder / "index.html").read_text()


def test_cli_runs_end_to_end(tmp_path, capsys):
    out_dir = tmp_path / "dbg"
    code = tester.main([RANDOM_BOT, "--games", "2", "--seed", "4", "--debug", str(out_dir)])
    printed = capsys.readouterr().out
    assert code == 0
    assert "RESULTS" in printed and "FAULTS   0 of 4 games" in printed
    assert "TOTAL" in printed
    assert (out_dir / "index.html").exists()


def test_cli_single_game_prints_a_replay(capsys):
    code = tester.main([ORDERED_BOT, "--opponent", "random", "--game", "3", "--seed", "9"])
    printed = capsys.readouterr().out
    assert code == 0
    assert "games  : 1 per opponent, 1 total" in printed
    assert "GAME    vs random, game #3" in printed
    assert "move log" in printed
    assert "TOTAL" not in printed        # one opponent, so no cross-opponent total


def test_ship_letters_are_distinct():
    letters = dbg.ship_letters({"carrier", "cruiser", "battleship", "submarine", "destroyer"})
    assert len(set(letters.values())) == 5
    assert letters["carrier"] == "c" and letters["cruiser"] != "c"


def test_ascii_heatmap_shows_exact_values_and_blanks():
    grid = [[0.0, 5.0], [None, 10.0]]
    text = dbg.ascii_heatmap(grid)
    assert "max = 10" in text
    assert "exact values" in text
    assert "10" in text and "-" in text


def test_svg_heatmap_is_self_contained():
    svg = dbg.svg_heatmap("t", "s", [[1.0, 2.0], [3.0, None]])
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "http" not in svg.replace("http://www.w3.org/2000/svg", "")
    assert svg.count("<rect") >= 4


def test_move_log_truncates_after_the_fault_round():
    log = [{"round": r, "side": "b", "row": 0, "col": r, "result": "miss", "sunk_ship": None}
           for r in range(1, 20)]
    table = dbg.move_log_table(log, until=5)
    assert "round(s) hidden" in table
    assert "[0,5]" in table and "[0,6]" not in table
