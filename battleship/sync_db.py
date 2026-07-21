"""Keep a local SQLite mirror of the server's game data, synced incrementally over HTTP.
Everyone can run this and query their own copy, so live query load never hits the server.

    python -m battleship.sync_db --server host:8080 --db mirror.db            # loop
    python -m battleship.sync_db --server host:8080 --db mirror.db --once     # one pull

The mirror uses the server's `seq` values as-is, so the sync cursor is simply
MAX(seq) in the mirror — no extra bookkeeping.
"""

import argparse
import json
import time
import urllib.request

from .db import Database


def _cursor(conn, table):
    return conn.execute(f"SELECT COALESCE(MAX(seq), 0) FROM {table}").fetchone()[0]


def _apply(conn, table, rows, replace):
    if not rows:
        return
    cols = list(rows[0].keys())
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    sql = f"{verb} INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
    conn.executemany(sql, [[row[c] for c in cols] for row in rows])


def apply_payload(mirror, payload):
    """Merge a /sync payload into the mirror. Small mutable tables are replaced; the big
    append-only tables are ignore-inserted (seq dedupes)."""
    conn = mirror.conn
    with conn:
        for table in ("tourneys", "bots", "bot_sessions"):
            _apply(conn, table, payload.get(table, []), replace=True)
        for table in ("games", "moves"):
            _apply(conn, table, payload.get(table, []), replace=False)


def sync_once(mirror, base_url, timeout=30):
    """Pull everything newer than the mirror's cursors. Returns rows applied."""
    games = _cursor(mirror.conn, "games")
    moves = _cursor(mirror.conn, "moves")
    url = f"{base_url}/sync?games={games}&moves={moves}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        payload = json.load(resp)
    apply_payload(mirror, payload)
    return {"games": len(payload.get("games", [])), "moves": len(payload.get("moves", []))}


def base_url(server):
    return server if server.startswith("http") else f"http://{server}"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sync a local mirror of the game database.")
    ap.add_argument("--server", default="localhost:8080", help="server HTTP host:port")
    ap.add_argument("--db", default="mirror.db", help="local mirror path")
    ap.add_argument("--once", action="store_true", help="sync once and exit")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between pulls")
    args = ap.parse_args(argv)

    mirror = Database(args.db)
    mirror.conn.execute("PRAGMA foreign_keys=OFF")  # trusted mirror; skip FK ordering
    url = base_url(args.server)

    while True:
        try:
            added = sync_once(mirror, url)
            total = _cursor(mirror.conn, "games")
            print(f"synced +{added['games']} games, +{added['moves']} moves "
                  f"(mirror now up to game seq {total})")
        except Exception as exc:
            print(f"sync failed: {exc}")
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
