#!/usr/bin/env python3
"""Local bot tester: run a bot against the two example bots for N games with zero
infrastructure (no server, no network), print exact results, and — with `--debug` —
write a folder of fault replays and placement / guess heatmaps.

    python test/test_bot.py bots/random_bot.py
    python test/test_bot.py mybot.py --games 200 --seed 1
    python test/test_bot.py mybot.py --debug debug/            # replays + heatmaps
    python test/test_bot.py mybot.py --opponent random --game 17 --seed 1   # one game

Every game is seeded from (seed, opponent, game index), so any game the run reports can
be replayed on its own with `--opponent NAME --game N --seed S`.
"""

import argparse
import csv
import math
import os
import random
import statistics
import sys
import time
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import debug_report as dbg                          # noqa: E402
from battleship.bot_api import load_bot             # noqa: E402
from battleship.config import default_config        # noqa: E402
from battleship.game import Game, validate_layout   # noqa: E402

EXAMPLES = {
    "random": os.path.join(REPO, "bots", "random_bot.py"),
    "ordered": os.path.join(REPO, "bots", "ordered_bot.py"),
}

TESTED, OPPONENT = "a", "b"   # the tested bot always plays side 'a'


# -- running one game ----------------------------------------------------------

def _diagnose_move(cell, rows, cols, already_shot):
    """Why the engine would reject `cell`, or None if it is legal."""
    if not isinstance(cell, (list, tuple)) or len(cell) != 2:
        return f"move is not a [row, col] pair: {cell!r}"
    try:
        r, c = int(cell[0]), int(cell[1])
    except (TypeError, ValueError):
        return f"move coordinates are not integers: {cell!r}"
    if not (0 <= r < rows and 0 <= c < cols):
        return f"move [{r},{c}] is off the {rows}x{cols} board"
    if (r, c) in already_shot:
        return f"cell [{r},{c}] was already fired at"
    return None


def play_instrumented(factory, opp_factory, config, seed_key):
    """Play one game and record what a debugging player needs: the first fault (with
    its traceback), both layouts, the move log, and per-call timings.

    Unlike the live server — and unlike `battleship.local_match`, which mirrors it — a
    fault here is captured rather than silently turned into a forfeit.
    """
    random.seed(seed_key)
    rows, cols = config["rows"], config["cols"]
    bots, layouts = {}, {}
    fault = None            # first thing the tested bot did wrong
    opp_fault = None
    place_ms = 0.0

    for side, fac in ((TESTED, factory), (OPPONENT, opp_factory)):
        cfg = dict(config)
        cfg["game_id"] = seed_key
        bot, elapsed = None, 0.0
        try:
            bot = fac(cfg)
            t0 = time.perf_counter()
            layouts[side] = bot.place_ships()
            elapsed = (time.perf_counter() - t0) * 1000
        except Exception:
            layouts[side] = None
            tb = traceback.format_exc()
            err = {"phase": "place_ships", "kind": "crash", "round": None,
                   "detail": tb.strip().splitlines()[-1], "traceback": tb}
            if side == TESTED:
                fault = err
            else:
                opp_fault = err
        bots[side] = bot
        if side == TESTED:
            place_ms = elapsed

    sizes = {ship["name"]: ship["size"] for ship in config["fleet"]}
    for side in (TESTED, OPPONENT):
        if layouts[side] is None:
            continue
        ok, reason, _ = validate_layout(layouts[side], rows, cols, sizes)
        if ok:
            continue
        err = {"phase": "place_ships", "kind": "illegal layout", "round": None,
               "detail": reason, "traceback": None}
        if side == TESTED and fault is None:
            fault = err
        elif side == OPPONENT and opp_fault is None:
            opp_fault = err

    game = Game(layouts[TESTED], layouts[OPPONENT], config)
    move_ms = []
    while not game.over:
        moves = {}
        for side, view in game.views().items():
            t0 = time.perf_counter()
            try:
                moves[side] = bots[side].make_move(view)
                crash = None
            except Exception:
                moves[side] = None
                crash = traceback.format_exc()
            elapsed = (time.perf_counter() - t0) * 1000
            if side == TESTED:
                move_ms.append(elapsed)
            if crash is not None:
                err = {"phase": "make_move", "kind": "crash", "round": game.current_round,
                       "detail": crash.strip().splitlines()[-1], "traceback": crash,
                       "view": view}
            else:
                why = _diagnose_move(moves[side], rows, cols, game.shots[side])
                err = None if why is None else {
                    "phase": "make_move", "kind": "illegal move", "round": game.current_round,
                    "detail": why, "traceback": None, "view": view,
                }
            if err is None:
                continue
            if side == TESTED and fault is None:
                fault = err
            elif side == OPPONENT and opp_fault is None:
                opp_fault = err
        game.resolve(moves)

    return {
        "game": game,
        "result": game.result(),
        "fault": fault,
        "opponent_fault": opp_fault,
        "layout": layouts[TESTED],
        "place_ms": place_ms,
        "move_ms": move_ms,
    }


# -- a series against one opponent ---------------------------------------------

def run_series(factory, opp_factory, config, opponent, games, seed, first_game=0):
    """Play `games` games with the tested bot as side 'a'. One record per game."""
    records = []
    for i in range(first_game, first_game + games):
        played = play_instrumented(factory, opp_factory, config, f"{seed}:{opponent}:{i}")
        game = played["game"]
        records.append({
            "opponent": opponent,
            "index": i,
            "seed_key": f"{seed}:{opponent}:{i}",
            "result": played["result"],
            "fault": played["fault"],
            "opponent_fault": played["opponent_fault"],
            "layout": played["layout"],
            "shots": [(m["round"], m["row"], m["col"], m["result"])
                      for m in game.moves_log if m["side"] == TESTED],
            "incoming": {(m["row"], m["col"]): m["result"]
                         for m in game.moves_log if m["side"] == OPPONENT},
            "place_ms": played["place_ms"],
            "move_ms": played["move_ms"],
            "game": game,
        })
    return records


def _stats(values):
    if not values:
        return None
    ordered = sorted(values)
    return {
        "n": len(values),
        "avg": statistics.mean(values),
        "med": statistics.median(values),
        "min": ordered[0],
        "max": ordered[-1],
        "sd": statistics.pstdev(values) if len(values) > 1 else 0.0,
        "p95": ordered[min(len(ordered) - 1, int(math.ceil(0.95 * len(ordered))) - 1)],
    }


def summarize(records):
    """Aggregate a set of game records into the numbers the report prints."""
    n = len(records)
    solver = [r["result"]["a_solved_round"] for r in records
              if r["result"]["a_solved_round"] is not None]
    layout = [r["result"]["b_solved_round"] for r in records
              if r["result"]["b_solved_round"] is not None]
    wins = sum(r["result"]["winner"] == TESTED for r in records)
    ties = sum(r["result"]["winner"] == "tie" for r in records)
    return {
        "games": n,
        "wins": wins,
        "ties": ties,
        "losses": sum(r["result"]["winner"] == OPPONENT for r in records),
        "forfeits": sum(r["result"]["a_outcome"] == "forfeit" for r in records),
        "win_rate": (wins + 0.5 * ties) / n if n else 0.0,
        "solver": _stats(solver),
        "layout": _stats(layout),
        "uncracked": sum(r["result"]["b_solved_round"] is None for r in records),
        "move_ms": _stats([ms for r in records for ms in r["move_ms"]]),
        "place_ms": _stats([r["place_ms"] for r in records]),
    }


# -- textual report ------------------------------------------------------------

STAT_HEAD = f"{'opponent':<10}{'n':>6}{'avg':>9}{'med':>8}{'min':>7}{'max':>7}{'sd':>8}"


def _stat_row(label, stats, extra=""):
    if stats is None:
        return f"{label:<10}{0:>6}{'-':>9}{'-':>8}{'-':>7}{'-':>7}{'-':>8}" + extra
    return (f"{label:<10}{stats['n']:>6}{stats['avg']:>9.2f}{stats['med']:>8.1f}"
            f"{stats['min']:>7.0f}{stats['max']:>7.0f}{stats['sd']:>8.2f}") + extra


def report(meta, botfile, config, seed, by_opponent, games):
    """The whole run as plain text: exact counts, exact distributions, every fault."""
    total = [r for records in by_opponent.values() for r in records]
    summaries = {name: summarize(recs) for name, recs in by_opponent.items()}
    if len(by_opponent) > 1:
        summaries["TOTAL"] = summarize(total)

    fleet = "  ".join(f"{s['name']}({s['size']})" for s in config["fleet"])
    out = [
        f"bot    : {meta['player']}/{meta['bot']}   ({botfile})",
        f"games  : {games} per opponent, {len(total)} total",
        f"board  : {config['rows']}x{config['cols']}   fleet: {fleet}",
        f"seed   : {seed}   (rerun this exact set with --seed {seed})",
        "",
        "RESULTS",
    ]
    head = f"{'opponent':<10}{'games':>6}{'win':>6}{'tie':>6}{'loss':>6}{'forfeit':>9}{'win%':>8}"
    out += [head, "-" * len(head)]
    for name, s in summaries.items():
        out.append(f"{name:<10}{s['games']:>6}{s['wins']:>6}{s['ties']:>6}{s['losses']:>6}"
                   f"{s['forfeits']:>9}{s['win_rate'] * 100:>7.1f}%")

    out += ["", "SOLVER   rounds you took to clear the opponent (lower is better)",
            STAT_HEAD, "-" * len(STAT_HEAD)]
    for name, s in summaries.items():
        out.append(_stat_row(name, s["solver"]))

    out += ["", "LAYOUT   rounds the opponent took to crack you (higher is better)",
            STAT_HEAD + f"{'uncracked':>11}", "-" * (len(STAT_HEAD) + 11)]
    for name, s in summaries.items():
        out.append(_stat_row(name, s["layout"], extra=f"{s['uncracked']:>11}"))

    out += ["", "TIMING   ms of your own compute (the live budget is "
            f"{config['move_time_ms']} ms for a whole batch of moves)"]
    thead = (f"{'opponent':<10}{'moves':>8}{'avg':>9}{'p95':>9}{'max':>9}"
             f"{'place avg':>11}{'place max':>11}")
    out += [thead, "-" * len(thead)]
    for name, s in summaries.items():
        m, p = s["move_ms"], s["place_ms"]
        if m is None or p is None:
            continue
        out.append(f"{name:<10}{m['n']:>8}{m['avg']:>9.3f}{m['p95']:>9.3f}{m['max']:>9.3f}"
                   f"{p['avg']:>11.3f}{p['max']:>11.3f}")

    faults = [r for r in total if r["fault"]]
    out += ["", f"FAULTS   {len(faults)} of {len(total)} games"]
    if faults:
        kinds = {}
        for r in faults:
            kinds.setdefault(f"{r['fault']['kind']} in {r['fault']['phase']}", []).append(r)
        for kind, rs in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
            out.append(f"  {kind:<30}{len(rs):>5}")
        out.append("  replay one with --opponent NAME --game N --seed SEED:")
        for r in faults[:10]:
            f = r["fault"]
            where = f"round {f['round']}" if f["round"] else "placement"
            out.append(f"    vs {r['opponent']:<8} game #{r['index']:<5} {where:<14} {f['detail']}")
        if len(faults) > 10:
            out.append(f"    ... and {len(faults) - 10} more")
    opp_faults = [r for r in total if r["opponent_fault"]]
    if opp_faults:
        out.append(f"  note: the example bot faulted in {len(opp_faults)} game(s) — that is a "
                   "harness bug, please report it")

    out.append("")
    if faults:
        out.append("VERDICT  fix the faults first — every one of them is a game lost live. "
                   "Re-run with --debug DIR for full replays.")
    else:
        out.append("VERDICT  no faults: legal in every game. The numbers above are your baseline.")
    return "\n".join(out)


# -- per-game pictures ---------------------------------------------------------

def game_picture(record, config):
    """Both boards and the move log for one game, as text."""
    rows, cols = config["rows"], config["cols"]
    sizes = {s["name"]: s["size"] for s in config["fleet"]}
    ok, reason, cells = validate_layout(record["layout"], rows, cols, sizes)
    _, _, opp_cells = validate_layout(record["game"].layout[OPPONENT], rows, cols, sizes)
    letters = dbg.ship_letters(sizes)
    out = ["ships: " + "  ".join(f"{letters[n]}={n}" for n in sorted(sizes)),
           "",
           "your layout (lowercase = ship cell, uppercase = the opponent hit it, "
           "* = their miss)"]
    if ok:
        out.append(dbg.board_picture(rows, cols, cells, record["incoming"]))
    else:
        out += [f"  ILLEGAL LAYOUT: {reason}", f"  raw: {record['layout']!r}"]
    fault_round = record["fault"]["round"] if record["fault"] else None
    out += ["",
            "the board you shot at (X = your hit, o = your miss, lowercase = ship cell "
            "you never found)",
            dbg.shot_picture(rows, cols,
                             {(r, c): res for _, r, c, res in record["shots"]}, opp_cells),
            "", "move log",
            dbg.move_log_table(record["game"].moves_log, until=fault_round)]
    return "\n".join(out)


def game_replay(record, config, botfile):
    """One game spelled out: how to reproduce it, the fault if any, then the picture."""
    f = record["fault"]
    base_seed = record["seed_key"].split(":")[0]
    out = [
        f"{'FAULT' if f else 'GAME '}   vs {record['opponent']}, game #{record['index']}",
        "",
        f"reproduce:  python test/test_bot.py {botfile} --opponent {record['opponent']} "
        f"--game {record['index']} --seed {base_seed}",
        "",
    ]
    if f:
        out += [
            f"phase   : {f['phase']}",
            f"kind    : {f['kind']}",
            f"round   : {f['round'] if f['round'] else '- (before the first round)'}",
            f"detail  : {f['detail']}",
        ]
        if f.get("view") is not None:
            out += ["", "the view your bot was answering:", f"  {f['view']}"]
        if f.get("traceback"):
            out += ["", "traceback:", f["traceback"].rstrip()]
    else:
        out.append("no fault — the bot played this game legally")
    out += ["", f"result  : {record['result']}", ""]
    out.append(game_picture(record, config))
    return "\n".join(out)


# -- debug folder --------------------------------------------------------------

def heatmap_grids(records, config):
    """Placement, guess-frequency, guess-order and hit grids over every game played."""
    rows, cols = config["rows"], config["cols"]
    sizes = {s["name"]: s["size"] for s in config["fleet"]}
    placed = [[0] * cols for _ in range(rows)]
    shot = [[0] * cols for _ in range(rows)]
    order_sum = [[0] * cols for _ in range(rows)]
    hits = [[0] * cols for _ in range(rows)]
    layouts = 0
    for rec in records:
        ok, _, cell_to_ship = validate_layout(rec["layout"], rows, cols, sizes)
        if ok:
            layouts += 1
            for (r, c) in cell_to_ship:
                placed[r][c] += 1
        for rnd, r, c, result in rec["shots"]:
            shot[r][c] += 1
            order_sum[r][c] += rnd
            if result in ("hit", "sunk"):
                hits[r][c] += 1
    n = len(records)
    pct = lambda count, denom: 100.0 * count / denom if denom else 0.0  # noqa: E731
    return {
        "placements": ([[pct(placed[r][c], layouts) for c in range(cols)] for r in range(rows)],
                       placed, layouts),
        "guesses": ([[pct(shot[r][c], n) for c in range(cols)] for r in range(rows)], shot, n),
        "guess-order": ([[(order_sum[r][c] / shot[r][c]) if shot[r][c] else None
                          for c in range(cols)] for r in range(rows)], shot, n),
        "hits": ([[float(hits[r][c]) for c in range(cols)] for r in range(rows)], hits, n),
    }


def _heatmap_specs(grids, n_games):
    layouts = grids["placements"][2]
    return [
        ("placements", "Placements — where your ships sit",
         f"% of your {layouts} legal layouts covering each cell", "of layouts"),
        ("guesses", "Guesses — where you shoot",
         f"% of {n_games} games in which you fired at each cell", "of games"),
        ("guess-order", "Guess order — when you shoot there",
         "average round the cell was fired at (blank = never fired)", "avg round"),
        ("hits", "Hits — where you found ships",
         f"hits scored on each cell across {n_games} games", "hits"),
    ]


def write_debug(folder, meta, botfile, config, by_opponent, summary_text):
    """Write the debug folder: summary, per-game csv, fault replays, heatmaps, index."""
    records = [r for recs in by_opponent.values() for r in recs]
    os.makedirs(folder, exist_ok=True)
    dbg.write_text(os.path.join(folder, "summary.txt"), summary_text)

    with open(os.path.join(folder, "games.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["opponent", "game", "seed_key", "winner", "a_outcome", "b_outcome",
                    "solver_rounds", "layout_rounds", "total_rounds", "end_reason",
                    "fault_kind", "fault_phase", "fault_round", "fault_detail", "max_move_ms"])
        for r in records:
            res, f = r["result"], r["fault"]
            w.writerow([r["opponent"], r["index"], r["seed_key"], res["winner"],
                        res["a_outcome"], res["b_outcome"], res["a_solved_round"],
                        res["b_solved_round"], res["total_rounds"], res["end_reason"],
                        f["kind"] if f else "", f["phase"] if f else "",
                        f["round"] if f else "", f["detail"] if f else "",
                        f"{max(r['move_ms']):.3f}" if r["move_ms"] else ""])

    fault_files = []
    faults = [r for r in records if r["fault"]]
    for r in faults:
        name = f"{r['opponent']}-game-{r['index']:04d}.txt"
        dbg.write_text(os.path.join(folder, "faults", name), game_replay(r, config, botfile))
        fault_files.append((name, f"{r['fault']['kind']} in {r['fault']['phase']}: "
                                  f"{r['fault']['detail']}"))

    grids = heatmap_grids(records, config)
    svgs = []
    for slug, title, subtitle, unit in _heatmap_specs(grids, len(records)):
        grid, raw, _ = grids[slug]
        fmt = (lambda v: f"{v:.0f}")

        def tip(r, c, v, raw=raw, unit=unit):
            return f"[{r},{c}]  {v:.1f} {unit}  (raw count {raw[r][c]})"

        svg = dbg.svg_heatmap(title, subtitle, grid, fmt, tip)
        dbg.write_text(os.path.join(folder, "heatmaps", slug + ".svg"), svg)
        dbg.write_text(os.path.join(folder, "heatmaps", slug + ".txt"),
                       f"{title}\n{subtitle}\n\n" + dbg.ascii_heatmap(grid, fmt))
        svgs.append(svg)

    dbg.write_index(
        os.path.join(folder, "index.html"),
        f"{meta['player']}/{meta['bot']} — local test",
        f"{len(records)} games vs the example bots ({botfile})",
        summary_text, svgs, fault_files,
    )
    return len(faults)


# -- cli -----------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Test a bot against the example bots, locally.")
    ap.add_argument("botfile", help="path to the bot file to test")
    ap.add_argument("--games", type=int, default=100, help="games per opponent (default 100)")
    ap.add_argument("--seed", type=int, default=None,
                    help="base seed; every game derives from it (default: random, printed)")
    ap.add_argument("--opponent", choices=sorted(EXAMPLES) + ["both"], default="both",
                    help="which example bot to play (default both)")
    ap.add_argument("--game", type=int, metavar="N", default=None,
                    help="play only game index N and print its full replay")
    ap.add_argument("--debug", metavar="DIR", default=None,
                    help="write fault replays, heatmaps and a per-game csv to DIR")
    args = ap.parse_args(argv)

    seed = args.seed if args.seed is not None else random.randrange(10 ** 9)
    try:
        meta, factory = load_bot(args.botfile)
    except Exception:
        print(f"Failed to load {args.botfile}:\n")
        traceback.print_exc()
        return 1

    config = default_config()
    opponents = sorted(EXAMPLES) if args.opponent == "both" else [args.opponent]
    games = 1 if args.game is not None else args.games
    first = args.game or 0

    by_opponent = {}
    for name in opponents:
        _, opp_factory = load_bot(EXAMPLES[name])
        by_opponent[name] = run_series(factory, opp_factory, config, name, games, seed,
                                       first_game=first)

    text = report(meta, args.botfile, config, seed, by_opponent, games)
    print(text)

    if args.game is not None:
        # Single-game mode: that one game is the point, so always show the replay.
        for recs in by_opponent.values():
            for rec in recs:
                print()
                print(game_replay(rec, config, args.botfile))

    if args.debug:
        n_faults = write_debug(args.debug, meta, args.botfile, config, by_opponent, text)
        print(f"""
debug output: {os.path.abspath(args.debug)}
  index.html    summary + heatmaps on one page (open it in a browser)
  summary.txt   the report above
  games.csv     one row per game
  heatmaps/     placements, guesses, guess-order, hits (.svg and .txt)
  faults/       {n_faults} replay file(s)""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
