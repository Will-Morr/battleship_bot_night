"""End-to-end server test over real ZeroMQ: two DEALER clients register and play a
full tourney against the live server, and we verify games land in the DB with saved
code and response times. This exercises registration, the dispatcher, the tourney loop,
and the off-loop writer together."""

import asyncio
import contextlib
import os
import socket
import time

import aiohttp
import zmq
import zmq.asyncio

from battleship import protocol as p
from battleship.bot_api import load_bot
from battleship.db import connect
from battleship.server import Server

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RANDOM_BOT = os.path.join(REPO, "bots", "random_bot.py")
ORDERED_BOT = os.path.join(REPO, "bots", "ordered_bot.py")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def run_client(port, botfile, player, botname, stop):
    """A minimal in-test runner: register, then answer placement/move/ping until told
    to stop. Mirrors what run_bot.py will do."""
    ctx = zmq.asyncio.Context.instance()
    sock = ctx.socket(zmq.DEALER)
    sock.connect(f"tcp://127.0.0.1:{port}")
    _, factory = load_bot(botfile)
    code = open(botfile).read()
    await sock.send(p.encode(p.msg(p.REGISTER, player=player, bot=botname, code=code,
                                   code_filename=os.path.basename(botfile), runner_version="test")))
    bots = {}
    try:
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(sock.recv(), 0.1)
            except asyncio.TimeoutError:
                continue
            m = p.decode(raw)
            t = m.get("type")
            if t == p.PING:
                await sock.send(p.encode(p.msg(p.PONG, t=m.get("t"))))
            elif t == p.PLACE_REQUEST:
                cfg = m["config"]
                placements = {}
                for h in m["games"]:
                    bot = factory(dict(cfg, game_id=h))
                    bots[h] = bot
                    placements[str(h)] = bot.place_ships()
                await sock.send(p.encode(p.msg(p.PLACE_REPLY, tourney_id=m["tourney_id"],
                                               placements=placements, compute_ms={})))
            elif t == p.MOVE_REQUEST:
                moves, compute = {}, {}
                for h, view in m["views"].items():
                    bot = bots.get(int(h))
                    if bot is not None:
                        moves[h] = bot.make_move(view)
                        compute[h] = 0.5
                await sock.send(p.encode(p.msg(p.MOVE_REPLY, tourney_id=m["tourney_id"],
                                               moves=moves, compute_ms=compute)))
            elif t == p.KICK:
                break
    finally:
        sock.close(0)


def test_server_end_to_end(tmp_path):
    db_path = str(tmp_path / "srv.db")
    zmq_port, http_port = free_port(), free_port()

    async def scenario():
        server = Server(db_path, zmq_port=zmq_port, http_port=http_port,
                        target=3, gap_ms=50, seed=1)
        serve = asyncio.create_task(server.serve())
        await asyncio.sleep(0.3)  # let sockets bind
        stop = asyncio.Event()
        clients = [
            asyncio.create_task(run_client(zmq_port, RANDOM_BOT, "alice", "rand", stop)),
            asyncio.create_task(run_client(zmq_port, ORDERED_BOT, "bob", "ord", stop)),
        ]

        # Poll a read-only connection until a tourney has completed with games.
        reader = connect(db_path, readonly=True)
        deadline = time.time() + 15
        games = 0
        try:
            while time.time() < deadline:
                await asyncio.sleep(0.2)
                done = reader.execute("SELECT COUNT(*) c FROM tourneys WHERE status='complete'").fetchone()["c"]
                games = reader.execute("SELECT COUNT(*) c FROM games").fetchone()["c"]
                if done >= 1 and games >= 3:
                    break
        finally:
            reader.close()

        # HTTP surface: health, incremental sync, and full-db download.
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{http_port}/health") as r:
                assert (await r.json())["ok"] is True
            async with http.get(f"http://127.0.0.1:{http_port}/sync") as r:
                payload = await r.json()
                assert len(payload["games"]) >= 3
                assert payload["cursors"]["games"] > 0
                assert len(payload["bots"]) == 2
            async with http.get(f"http://127.0.0.1:{http_port}/db") as r:
                blob = await r.read()
                assert blob.startswith(b"SQLite format 3")
            # analytics API
            async with http.get(f"http://127.0.0.1:{http_port}/api/rankings") as r:
                board = await r.json()
                assert len(board) == 2
                assert {b["player"] for b in board} == {"alice", "bob"}
                assert all("combined" in b and "win_rate" in b for b in board)
                first_bot = board[0]["bot_uuid"]
            async with http.get(f"http://127.0.0.1:{http_port}/api/pairings") as r:
                assert len(await r.json()) == 1        # one pair (alice vs bob)
            async with http.get(f"http://127.0.0.1:{http_port}/api/bot/{first_bot}") as r:
                detail = await r.json()
                assert detail["games"] >= 3 and "solver_hist" in detail
            # static pages render
            for route in ("/", "/projector", "/static/app.js"):
                async with http.get(f"http://127.0.0.1:{http_port}{route}") as r:
                    assert r.status == 200

        stop.set()
        await asyncio.gather(*clients)
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serve
        server.writer.shutdown(wait=True)
        return db_path, games

    _, games = asyncio.run(scenario())
    assert games >= 3

    # Inspect the persisted data on a fresh connection.
    conn = connect(db_path, readonly=True)
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM games").fetchall()]
        assert all(g["end_reason"] == "complete" for g in rows), rows
        assert all(g["winner"] in ("a", "b", "tie") for g in rows)
        # response times were reported (0.5 ms) and aggregated per side.
        assert any(g["a_resp_avg_ms"] == 0.5 for g in rows)
        # both example players registered and their code was saved.
        players = {r["player"] for r in conn.execute("SELECT player FROM bots")}
        assert players == {"alice", "bob"}
        codes = [r["code"] for r in conn.execute("SELECT code FROM bot_sessions")]
        assert all(c and "BOT" in c for c in codes)
        # every game's moves were logged.
        moves = conn.execute("SELECT COUNT(*) c FROM moves").fetchone()["c"]
        assert moves > 0
    finally:
        conn.close()
