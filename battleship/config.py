"""Tournament defaults, all tunable in one place.

The game `config` dict (rows, cols, fleet, move_time_ms) is what gets sent to bots
and passed into `Bot.__init__`; the module constants below are server-side knobs.
"""

# Board dimensions.
ROWS = 10
COLS = 10

# Standard fleet as (name, size). Names are unique so a sink can report which ship
# went down — note the two size-3 ships have distinct names.
FLEET = [
    {"name": "carrier", "size": 5},
    {"name": "battleship", "size": 4},
    {"name": "cruiser", "size": 3},
    {"name": "submarine", "size": 3},
    {"name": "destroyer", "size": 2},
]

# One whole-request wall-clock budget per round (bot compute + comms). See
# DATA_CONTRACTS.md §3. Optionally scaled by batch size.
MOVE_TIME_MS = 250
DEADLINE_PER_GAME_MS = 3.0        # deadline = MOVE_TIME_MS + this * num_games
PLACE_TIME_MS = 1000              # whole-batch budget for layouts (once per tourney)
BLACKOUT_GRACE = 1                # consecutive full blackouts tolerated before drop

# Tourney sizing / cadence.
TARGET_GAMES = 50                 # games per bot per tourney (a target, not a guarantee)
TOURNEY_GAP_MS = 1000             # pause between back-to-back tourneys


def make_config(rows=ROWS, cols=COLS, fleet=None, move_time_ms=MOVE_TIME_MS):
    """Build a game-config dict. Fleet is deep-copied so callers can't mutate FLEET."""
    fleet = fleet if fleet is not None else FLEET
    return {
        "rows": rows,
        "cols": cols,
        "fleet": [dict(ship) for ship in fleet],
        "move_time_ms": move_time_ms,
    }


def default_config():
    """The standard 10x10 game config."""
    return make_config()


def fleet_sizes(fleet):
    """{ship_name: size} for quick lookups."""
    return {ship["name"]: ship["size"] for ship in fleet}


def total_ship_cells(fleet):
    """How many cells must be hit to solve a board (17 for the standard fleet)."""
    return sum(ship["size"] for ship in fleet)


def deadline_ms(num_games, move_time_ms=MOVE_TIME_MS, per_game_ms=DEADLINE_PER_GAME_MS):
    """Whole-request deadline for a batch of `num_games` moves."""
    return move_time_ms + per_game_ms * num_games
