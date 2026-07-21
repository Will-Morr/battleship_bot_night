#!/usr/bin/env python3
"""Quick local test: run a bot in N games against the two example bots, with zero
infrastructure, and print a win-rate / solver / layout summary. Use it to sanity-check
a bot before deploying it live.

    python test/test_bot.py bots/random_bot.py
    python test/test_bot.py path/to/mybot.py --games 200 --seed 1
"""

import argparse
import os
import random
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from battleship.bot_api import load_bot          # noqa: E402
from battleship.config import default_config     # noqa: E402
from battleship.local_match import play_game     # noqa: E402

EXAMPLES = {
    "random": os.path.join(REPO, "bots", "random_bot.py"),
    "ordered": os.path.join(REPO, "bots", "ordered_bot.py"),
}


def _mean(values):
    return statistics.mean(values) if values else None


def run_series(factory, opp_factory, config, games):
    """Play `games` games with the tested bot as side 'a'. Returns summary stats."""
    wins = ties = losses = forfeits = 0
    solver_turns = []      # rounds 'a' took to solve (lower better)
    layout_turns = []      # rounds the opponent took to crack 'a' (higher better)
    layout_uncracked = 0   # games where the opponent never solved 'a' (best layout)
    for i in range(games):
        result = play_game(factory, opp_factory, config, game_id=i).result()
        winner = result["winner"]
        wins += winner == "a"
        losses += winner == "b"
        ties += winner == "tie"
        forfeits += result["a_outcome"] == "forfeit"
        if result["a_solved_round"] is not None:
            solver_turns.append(result["a_solved_round"])
        if result["b_solved_round"] is not None:
            layout_turns.append(result["b_solved_round"])
        else:
            layout_uncracked += 1
    return {
        "games": games,
        "wins": wins, "ties": ties, "losses": losses, "forfeits": forfeits,
        "win_rate": (wins + 0.5 * ties) / games if games else 0.0,
        "solver_avg": _mean(solver_turns),
        "layout_avg": _mean(layout_turns),
        "layout_uncracked": layout_uncracked,
    }


def fmt(x):
    return "-" if x is None else f"{x:.1f}"


def main():
    ap = argparse.ArgumentParser(description="Test a bot against the example bots.")
    ap.add_argument("botfile", help="path to the bot file to test")
    ap.add_argument("--games", type=int, default=100, help="games per opponent (default 100)")
    ap.add_argument("--seed", type=int, default=None, help="RNG seed for reproducibility")
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    try:
        meta, factory = load_bot(args.botfile)
    except Exception as exc:
        print(f"Failed to load bot: {exc}")
        return 1

    config = default_config()
    print(f"Testing {meta['player']}/{meta['bot']}  ({args.botfile})")
    print(f"{args.games} games vs each example  |  board {config['rows']}x{config['cols']}\n")

    header = f"{'opponent':<10}{'win%':>7}{'W-T-L':>12}{'solver↓':>10}{'layout↑':>10}{'forfeit':>9}"
    print(header)
    print("-" * len(header))
    for name, path in EXAMPLES.items():
        _, opp_factory = load_bot(path)
        s = run_series(factory, opp_factory, config, args.games)
        wtl = f"{s['wins']}-{s['ties']}-{s['losses']}"
        print(f"{name:<10}{s['win_rate'] * 100:>6.1f}%{wtl:>12}"
              f"{fmt(s['solver_avg']):>10}{fmt(s['layout_avg']):>10}{s['forfeits']:>9}")

    print("\nsolver↓ = avg turns to clear the opponent (lower is better)")
    print("layout↑ = avg turns the opponent took to crack you (higher is better)")
    print("forfeit = games lost to an illegal move / crash (should be 0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
