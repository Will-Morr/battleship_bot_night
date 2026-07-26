"""SQLite persistence. See DATA_CONTRACTS.md §4.

`Database` methods are plain synchronous SQLite calls. In the server every write runs
on a single dedicated writer thread (one connection, batched transactions) so the event
loop never blocks; reads use their own short-lived connections. WAL mode lets readers
run concurrently with the writer.

Two id columns per table: `uuid` (join key) and `seq` (INTEGER PRIMARY KEY = rowid),
which is a monotonic cursor for incremental sync since rows are never deleted.
"""

import json
import sqlite3
import uuid as uuidlib

# Rows per table per /sync response. A mirror pages until `more` is False. Bounded
# because each row becomes a dict then JSON: ~1.3 KB of peak RSS per move row, so an
# unbounded moves table (millions of rows) is several GB in a single request.
SYNC_PAGE_ROWS = 20_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS tourneys (
  seq INTEGER PRIMARY KEY,
  uuid TEXT UNIQUE NOT NULL,
  started_at REAL NOT NULL,
  ended_at REAL,
  status TEXT NOT NULL,
  num_games INTEGER,
  config_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bots (
  seq INTEGER PRIMARY KEY,
  uuid TEXT UNIQUE NOT NULL,
  player TEXT NOT NULL,
  name TEXT NOT NULL,
  first_tourney_uuid TEXT,
  last_tourney_uuid TEXT,
  first_seen_at REAL NOT NULL,
  last_seen_at REAL NOT NULL,
  metadata_json TEXT,
  UNIQUE(player, name)
);

CREATE TABLE IF NOT EXISTS bot_sessions (
  seq INTEGER PRIMARY KEY,
  uuid TEXT UNIQUE NOT NULL,
  bot_uuid TEXT NOT NULL REFERENCES bots(uuid),
  code TEXT NOT NULL,
  code_filename TEXT,
  code_hash TEXT NOT NULL,
  runner_version TEXT,
  registered_at REAL NOT NULL,
  disconnected_at REAL
);

CREATE TABLE IF NOT EXISTS games (
  seq INTEGER PRIMARY KEY,
  uuid TEXT UNIQUE NOT NULL,
  tourney_uuid TEXT NOT NULL REFERENCES tourneys(uuid),
  a_bot_uuid TEXT NOT NULL REFERENCES bots(uuid),
  b_bot_uuid TEXT NOT NULL REFERENCES bots(uuid),
  a_session_uuid TEXT NOT NULL REFERENCES bot_sessions(uuid),
  b_session_uuid TEXT NOT NULL REFERENCES bot_sessions(uuid),
  a_layout_json TEXT NOT NULL,
  b_layout_json TEXT NOT NULL,
  a_solved_round INTEGER,
  b_solved_round INTEGER,
  winner TEXT,
  a_outcome TEXT,
  b_outcome TEXT,
  end_reason TEXT,
  total_rounds INTEGER,
  started_at REAL NOT NULL,
  ended_at REAL,
  a_resp_min_ms REAL, a_resp_max_ms REAL, a_resp_avg_ms REAL,
  b_resp_min_ms REAL, b_resp_max_ms REAL, b_resp_avg_ms REAL
);

CREATE TABLE IF NOT EXISTS moves (
  seq INTEGER PRIMARY KEY,
  uuid TEXT UNIQUE NOT NULL,
  game_uuid TEXT NOT NULL REFERENCES games(uuid),
  round INTEGER NOT NULL,
  side TEXT NOT NULL,
  row INTEGER NOT NULL,
  col INTEGER NOT NULL,
  result TEXT NOT NULL,
  sunk_ship TEXT,
  response_ms REAL
);

CREATE INDEX IF NOT EXISTS idx_moves_game ON moves(game_uuid, round);
CREATE INDEX IF NOT EXISTS idx_games_tourney ON games(tourney_uuid);
CREATE INDEX IF NOT EXISTS idx_games_a_bot ON games(a_bot_uuid);
CREATE INDEX IF NOT EXISTS idx_games_b_bot ON games(b_bot_uuid);
-- Separate (bot, seq) indexes, under new names so an existing DB picks them up on the
-- next start: the leaderboard wants each bot's most recent N games, which these serve
-- as a bounded backward index scan instead of a scan-and-sort.
CREATE INDEX IF NOT EXISTS idx_games_a_bot_seq ON games(a_bot_uuid, seq);
CREATE INDEX IF NOT EXISTS idx_games_b_bot_seq ON games(b_bot_uuid, seq);
CREATE INDEX IF NOT EXISTS idx_sessions_bot ON bot_sessions(bot_uuid);
"""

GAME_COLS = [
    "uuid", "tourney_uuid", "a_bot_uuid", "b_bot_uuid", "a_session_uuid", "b_session_uuid",
    "a_layout_json", "b_layout_json", "a_solved_round", "b_solved_round", "winner",
    "a_outcome", "b_outcome", "end_reason", "total_rounds", "started_at", "ended_at",
    "a_resp_min_ms", "a_resp_max_ms", "a_resp_avg_ms",
    "b_resp_min_ms", "b_resp_max_ms", "b_resp_avg_ms",
]


def new_uuid():
    return uuidlib.uuid4().hex


def connect(path, readonly=False):
    """Open a tuned connection. Readers use `query_only` rather than URI `mode=ro`: a
    strict read-only handle cannot attach the WAL shared-memory index across processes
    and would then read a stale main file, so we open a normal handle and forbid writes."""
    conn = sqlite3.connect(path, check_same_thread=False)
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    else:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _resp_stats(moves, side):
    """(min, max, avg) response_ms for one side's moves, or (None, None, None)."""
    vals = [m["response_ms"] for m in moves if m["side"] == side and m.get("response_ms") is not None]
    if not vals:
        return None, None, None
    return min(vals), max(vals), sum(vals) / len(vals)


class Database:
    def __init__(self, path):
        self.path = str(path)
        self.conn = connect(self.path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # -- writes -----------------------------------------------------------------

    def register_session(self, *, player, bot_name, session_uuid, code, code_filename,
                         code_hash, runner_version, now):
        """Upsert the logical bot and insert a new session row. Returns the bot_uuid."""
        cur = self.conn.cursor()
        row = cur.execute("SELECT uuid FROM bots WHERE player=? AND name=?",
                          (player, bot_name)).fetchone()
        if row:
            bot_uuid = row["uuid"]
            cur.execute("UPDATE bots SET last_seen_at=? WHERE uuid=?", (now, bot_uuid))
        else:
            bot_uuid = new_uuid()
            cur.execute(
                "INSERT INTO bots(uuid, player, name, first_seen_at, last_seen_at) "
                "VALUES(?,?,?,?,?)", (bot_uuid, player, bot_name, now, now))
        cur.execute(
            "INSERT INTO bot_sessions(uuid, bot_uuid, code, code_filename, code_hash, "
            "runner_version, registered_at) VALUES(?,?,?,?,?,?,?)",
            (session_uuid, bot_uuid, code, code_filename, code_hash, runner_version, now))
        self.conn.commit()
        return bot_uuid

    def close_session(self, session_uuid, now):
        with self.conn:
            self.conn.execute("UPDATE bot_sessions SET disconnected_at=? WHERE uuid=?",
                              (now, session_uuid))

    def start_tourney(self, tourney_uuid, config, now):
        with self.conn:
            self.conn.execute(
                "INSERT INTO tourneys(uuid, started_at, status, config_json) VALUES(?,?,?,?)",
                (tourney_uuid, now, "running", json.dumps(config)))

    def finish_tourney(self, tourney_uuid, num_games, now):
        with self.conn:
            self.conn.execute(
                "UPDATE tourneys SET ended_at=?, status='complete', num_games=? WHERE uuid=?",
                (now, num_games, tourney_uuid))

    def mark_tourney_bots(self, tourney_uuid, bot_uuids):
        """Record tourney participation on each logical bot (first/last tourney)."""
        with self.conn:
            for bot_uuid in bot_uuids:
                self.conn.execute(
                    "UPDATE bots SET last_tourney_uuid=?, "
                    "first_tourney_uuid=COALESCE(first_tourney_uuid, ?) WHERE uuid=?",
                    (tourney_uuid, tourney_uuid, bot_uuid))

    def record_games(self, games):
        """Insert a batch of finished games and their moves in one transaction. Each
        game is a dict of the game fields plus a `moves` list; response-time aggregates
        and move uuids are filled in here."""
        with self.conn:
            for g in games:
                moves = g["moves"]
                amin, amax, aavg = _resp_stats(moves, "a")
                bmin, bmax, bavg = _resp_stats(moves, "b")
                values = (
                    g["uuid"], g["tourney_uuid"], g["a_bot_uuid"], g["b_bot_uuid"],
                    g["a_session_uuid"], g["b_session_uuid"],
                    json.dumps(g["a_layout"]), json.dumps(g["b_layout"]),
                    g["a_solved_round"], g["b_solved_round"], g["winner"],
                    g["a_outcome"], g["b_outcome"], g["end_reason"], g["total_rounds"],
                    g["started_at"], g["ended_at"],
                    amin, amax, aavg, bmin, bmax, bavg,
                )
                placeholders = ",".join("?" * len(GAME_COLS))
                self.conn.execute(
                    f"INSERT INTO games({','.join(GAME_COLS)}) VALUES({placeholders})", values)
                self.conn.executemany(
                    "INSERT INTO moves(uuid, game_uuid, round, side, row, col, result, "
                    "sunk_ship, response_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                    [(new_uuid(), g["uuid"], m["round"], m["side"], m["row"], m["col"],
                      m["result"], m["sunk_ship"], m.get("response_ms")) for m in moves])

    # -- reads ------------------------------------------------------------------

    def query(self, sql, params=(), conn=None):
        """Run a read query, returning a list of plain dicts."""
        conn = conn or self.conn
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def code_by_hash(self, code_hash, conn=None):
        """The bot source behind a `code_hash`, or None. Sessions sharing a hash share
        the identical source, so one row answers for all of them."""
        rows = self.query(
            "SELECT code, code_filename, code_hash FROM bot_sessions WHERE code_hash=? LIMIT 1",
            (code_hash,), conn)
        return rows[0] if rows else None

    def sync_since(self, games_cursor=0, moves_cursor=0, limit=SYNC_PAGE_ROWS, conn=None,
                   include_code=False):
        """Incremental payload for a synced mirror. Small mutable tables (tourneys,
        bots, bot_sessions) are sent whole; the big append-only tables (games, moves)
        are sent by `seq` cursor, at most `limit` rows each. Returns rows, the new
        cursors to store, and `more`: True if another page is waiting, so a mirror
        starting from cursor 0 pages through instead of asking for the whole table
        in one response (a full moves table is gigabytes once materialized).

        `bot_sessions.code` holds each bot's full source and never changes once written,
        yet the whole table ships on every poll -- measured live at 856 KB of identical
        source per request, 2.9x duplicated across sessions, on polls carrying zero new
        games. It is blanked by default and fetched on demand via `code_by_hash`; pass
        include_code=True for the old behaviour. `code_hash` still ships on every row,
        so the source is always retrievable."""
        conn = conn or self.conn
        sessions_cols = "*" if include_code else (
            "seq, uuid, bot_uuid, '' AS code, code_filename, code_hash, runner_version, "
            "registered_at, disconnected_at")
        games = self.query(
            "SELECT * FROM games WHERE seq>? ORDER BY seq LIMIT ?", (games_cursor, limit), conn)
        moves = self.query(
            "SELECT * FROM moves WHERE seq>? ORDER BY seq LIMIT ?", (moves_cursor, limit), conn)
        return {
            "tourneys": self.query("SELECT * FROM tourneys ORDER BY seq", (), conn),
            "bots": self.query("SELECT * FROM bots ORDER BY seq", (), conn),
            "bot_sessions": self.query(
                f"SELECT {sessions_cols} FROM bot_sessions ORDER BY seq", (), conn),
            "games": games,
            "moves": moves,
            "cursors": {
                "games": games[-1]["seq"] if games else games_cursor,
                "moves": moves[-1]["seq"] if moves else moves_cursor,
            },
            "more": len(games) == limit or len(moves) == limit,
        }

    def snapshot(self, dest_path):
        """Write a consistent copy of the DB to `dest_path` (for the /db download)."""
        self.conn.execute("VACUUM INTO ?", (str(dest_path),))
