"""Tournament server: one asyncio process running the ZeroMQ ROUTER (bot comms), the
continuous tourney loop, the off-loop SQLite writer, and the aiohttp HTTP surface
(sync + db download; analytics endpoints are added in analytics.py). See PLAN.md.

Concurrency model:
- All bot messages flow through one ROUTER socket and `_recv_loop`.
- A synchronous round is one `Exchange`: the dispatcher sends a batch to each session
  and awaits their replies (routed in by `_recv_loop`) until the whole-request deadline.
- Every DB write runs on a single-thread executor (the writer), so the event loop never
  blocks on SQLite; reads use their own read-only connections via `asyncio.to_thread`.
"""

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import random
import socket
import time
from concurrent.futures import ThreadPoolExecutor

import zmq
import zmq.asyncio
from aiohttp import web

from . import config as cfg
from . import protocol as p
from . import scoring
from .db import SYNC_PAGE_ROWS, Database, connect, new_uuid
from .tourney import TourneyEngine

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

RUNNER_VERSION = "1"


def _lan_ip():
    """Best-effort LAN address, so printed links work from other machines. No packets
    are sent: connect() on a UDP socket just picks the outbound interface."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "localhost"
    finally:
        s.close()


def log(message):
    """One-line server log, wall-clock stamped so the console reads as a timeline."""
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


class Session:
    """A connected bot session: its ROUTER identity and logical-bot identity."""

    def __init__(self, uuid, identity, bot_uuid, player, bot_name):
        self.uuid = uuid
        self.identity = identity
        self.bot_uuid = bot_uuid
        self.player = player
        self.bot_name = bot_name
        self.alive = True
        self.last_seen = time.time()
        self.ping_misses = 0

    def as_participant(self):
        return {"session_uuid": self.uuid, "bot_uuid": self.bot_uuid, "player": self.player}


class Exchange:
    """Collects one batched round's replies, keyed by session uuid."""

    def __init__(self, expected, kind):
        self.expected = set(expected)
        self.kind = kind          # 'place' or 'move'
        self.replies = {}
        self.event = asyncio.Event()

    def add(self, session_uuid, data):
        self.replies[session_uuid] = data
        if self.expected <= set(self.replies):
            self.event.set()


class ZmqDispatcher:
    """Adapts the ROUTER socket to the engine's dispatcher interface."""

    def __init__(self, server):
        self.server = server

    async def request_placements(self, requests, config, deadline_ms):
        expected = await self.server.send_place_requests(requests, config, deadline_ms)
        return await self.server.collect(expected, deadline_ms, "place")

    async def request_moves(self, requests, deadline_ms):
        expected = await self.server.send_move_requests(requests, deadline_ms)
        return await self.server.collect(expected, deadline_ms, "move")


class Server:
    def __init__(self, db_path, zmq_port=5555, http_port=8080, target=cfg.TARGET_GAMES,
                 gap_ms=cfg.TOURNEY_GAP_MS, seed=None):
        self.db = Database(db_path)
        self.config = cfg.default_config()
        self.zmq_port = zmq_port
        self.http_port = http_port
        self.target = target
        self.gap = gap_ms / 1000.0
        self.rng = random.Random(seed)

        self.ctx = zmq.asyncio.Context.instance()
        self.router = None
        self.writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-writer")
        self.snapshot_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)) or ".",
                                         "snapshots")
        os.makedirs(self.snapshot_dir, exist_ok=True)
        self._clear_snapshots()

        self.sessions = {}          # session_uuid -> Session
        self.by_identity = {}       # identity bytes -> session_uuid
        self._exchange = None       # the in-flight Exchange, if any
        self._tourney_wire_id = 0   # compact per-tourney id sent on the wire
        self._ping_window = 0.15
        self._max_ping_misses = 3
        self._sse_clients = set()   # asyncio.Queue per connected SSE viewer
        self._bg_writes = set()     # in-flight fire-and-forget writes (GC anchor)
        self.running = False

    def _active_bot_uuids(self):
        return frozenset(s.bot_uuid for s in self.sessions.values())

    # -- write/read plumbing ----------------------------------------------------

    async def _write(self, fn):
        """Run a DB write on the single writer thread."""
        return await asyncio.get_running_loop().run_in_executor(self.writer, fn, self.db)

    def _write_soon(self, fn):
        """Fire-and-forget a DB write; keeps a reference so the task isn't GC'd."""
        task = asyncio.create_task(self._write(fn))
        self._bg_writes.add(task)
        task.add_done_callback(self._bg_writes.discard)

    async def send(self, identity, message):
        await self.router.send_multipart([identity, p.encode(message)])

    # -- ROUTER receive ---------------------------------------------------------

    async def _recv_loop(self):
        while self.running:
            try:
                identity, raw = await self.router.recv_multipart()
            except (asyncio.CancelledError, zmq.ContextTerminated):
                break
            try:
                message = p.decode(raw)
            except Exception:
                continue
            self._handle(identity, message)

    def _handle(self, identity, message):
        mtype = message.get("type")
        if mtype == p.REGISTER:
            asyncio.create_task(self._register(identity, message))
            return
        session = self.sessions.get(self.by_identity.get(identity))
        if session is None:
            return  # unknown/retired identity; ignore (must re-register)
        session.last_seen = time.time()
        session.ping_misses = 0
        if mtype in (p.MOVE_REPLY, p.PLACE_REPLY):
            self._on_reply(session.uuid, message, "move" if mtype == p.MOVE_REPLY else "place")
        elif mtype == p.BYE:
            self._retire(session.uuid, "bye")
        # PONG needs nothing beyond the last_seen bump above.

    def _on_reply(self, session_uuid, message, kind):
        ex = self._exchange
        if ex is None or ex.kind != kind or session_uuid not in ex.expected:
            return
        if kind == "move":
            compute = message.get("compute_ms", {})
            data = {}
            for handle, cell in message.get("moves", {}).items():
                data[int(handle)] = (cell, compute.get(handle))
        else:
            data = {int(handle): layout for handle, layout in message.get("placements", {}).items()}
        ex.add(session_uuid, data)

    # -- registration / session lifecycle --------------------------------------

    async def _register(self, identity, message):
        player, bot = message.get("player"), message.get("bot")
        if not player or not bot:
            await self.send(identity, p.msg(p.KICK, reason="register needs player and bot"))
            return
        # Re-registering a live logical bot retires its old session (DATA_CONTRACTS §3).
        for existing in [s for s in self.sessions.values()
                         if s.alive and s.player == player and s.bot_name == bot]:
            self._retire(existing.uuid, "superseded")

        code = message.get("code", "")
        session_uuid = new_uuid()
        now = time.time()
        bot_uuid = await self._write(lambda db: db.register_session(
            player=player, bot_name=bot, session_uuid=session_uuid, code=code,
            code_filename=message.get("code_filename"),
            code_hash=hashlib.sha256(code.encode("utf-8")).hexdigest(),
            runner_version=message.get("runner_version"), now=now))

        session = Session(session_uuid, identity, bot_uuid, player, bot)
        self.sessions[session_uuid] = session
        self.by_identity[identity] = session_uuid
        await self.send(identity, p.msg(p.REGISTERED, uuid=session_uuid, bot_uuid=bot_uuid,
                                        config=self.config))
        log(f"connected: {player}/{bot} ({len(self.sessions)} bot(s) online)")

    def _retire(self, session_uuid, reason):
        session = self.sessions.pop(session_uuid, None)
        if session is None:
            return
        session.alive = False
        self.by_identity.pop(session.identity, None)
        now = time.time()
        self._write_soon(lambda db: db.close_session(session_uuid, now))

    # -- batched exchange (used by ZmqDispatcher) -------------------------------

    async def send_move_requests(self, requests, deadline_ms):
        expected = []
        for session_uuid, games in requests.items():
            session = self.sessions.get(session_uuid)
            if session is None or not session.alive:
                continue  # retired/dead -> no reply -> the engine forfeits its games
            views = {str(handle): view for handle, view in games.items()}
            await self.send(session.identity, p.msg(
                p.MOVE_REQUEST, tourney_id=self._tourney_wire_id,
                deadline_ms=deadline_ms, views=views))
            expected.append(session_uuid)
        return expected

    async def send_place_requests(self, requests, config, deadline_ms):
        expected = []
        for session_uuid, handles in requests.items():
            session = self.sessions.get(session_uuid)
            if session is None or not session.alive:
                continue
            await self.send(session.identity, p.msg(
                p.PLACE_REQUEST, tourney_id=self._tourney_wire_id,
                config=config, games=list(handles)))
            expected.append(session_uuid)
        return expected

    async def collect(self, expected, deadline_ms, kind):
        if not expected:
            return {}
        ex = Exchange(expected, kind)
        self._exchange = ex
        try:
            await asyncio.wait_for(ex.event.wait(), timeout=deadline_ms / 1000.0)
        except asyncio.TimeoutError:
            pass
        finally:
            self._exchange = None
        return ex.replies

    # -- continuous tourney loop ------------------------------------------------

    async def _gather_participants(self):
        """Ping everyone, wait briefly, prune non-responders, and return the alive set."""
        sessions = list(self.sessions.values())
        if not sessions:
            return []
        for s in sessions:
            await self.send(s.identity, p.msg(p.PING, t=time.time()))
        pinged_at = time.time()
        await asyncio.sleep(self._ping_window)
        alive = []
        for s in sessions:
            if s.uuid not in self.sessions:
                continue
            if s.last_seen >= pinged_at:
                s.ping_misses = 0
                alive.append(s)
            else:
                s.ping_misses += 1
                if s.ping_misses >= self._max_ping_misses:
                    self._retire(s.uuid, "unresponsive")
        return alive

    async def _tourney_loop(self):
        while self.running:
            participants = await self._gather_participants()
            if len(participants) < 2:
                await asyncio.sleep(self.gap)
                continue
            await self._run_tourney(participants)
            await asyncio.sleep(self.gap)

    async def _run_tourney(self, participants):
        tourney_uuid = new_uuid()
        self._tourney_wire_id += 1
        wire_id = self._tourney_wire_id
        await self._write(lambda db: db.start_tourney(tourney_uuid, self.config, time.time()))
        for s in participants:
            await self.send(s.identity, p.msg(p.TOURNEY_START, tourney_id=wire_id))

        self._broadcast({"type": "tourney_start", "tourney_id": wire_id})
        roster = ", ".join(sorted(f"{s.player}/{s.bot_name}" for s in participants))
        log(f"tourney #{wire_id} start: {len(participants)} bots [{roster}]")
        engine = TourneyEngine(tourney_uuid, [s.as_participant() for s in participants],
                               self.config, self.target, self.rng,
                               blackout_grace=cfg.BLACKOUT_GRACE)
        records = await engine.run(ZmqDispatcher(self))

        await self._write(lambda db: db.mark_tourney_bots(
            tourney_uuid, list(engine.participant_bot_uuids())))
        if records:
            await self._write(lambda db: db.record_games(records))
        await self._write(lambda db: db.finish_tourney(tourney_uuid, engine.num_games, time.time()))

        for dead in engine.dead_sessions:
            self._retire(dead, "blackout")
        for s in participants:
            if s.uuid in self.sessions:
                await self.send(s.identity, p.msg(p.TOURNEY_END, tourney_id=wire_id))
        self._broadcast({"type": "tourney_end", "tourney_id": wire_id,
                         "games": engine.num_games})

    # -- HTTP -------------------------------------------------------------------

    def http_app(self):
        app = web.Application()
        app.add_routes([
            web.get("/", self._h_index),
            web.get("/projector", self._h_projector),
            web.get("/health", self._h_health),
            web.get("/sync", self._h_sync),
            web.get("/db", self._h_db),
            web.get("/events", self._h_events),
            web.get("/api/rankings", self._h_rankings),
            web.get("/api/bots", self._h_bots),
            web.get("/api/bot/{uuid}", self._h_bot),
            web.get("/api/bot/{uuid}/games", self._h_bot_games),
            web.get("/api/game/{uuid}", self._h_game),
            web.get("/api/compare", self._h_compare),
            web.get("/api/pairings", self._h_pairings),
            web.get("/api/tourneys", self._h_tourneys),
            web.get("/api/code/{code_hash}", self._h_code),
        ])
        if os.path.isdir(WEB_DIR):
            app.router.add_static("/static/", WEB_DIR)
        return app

    async def _read(self, fn):
        """Run a read query on a throwaway read-only connection off the event loop."""
        return await asyncio.to_thread(self._read_blocking, fn)

    def _read_blocking(self, fn):
        conn = connect(self.db.path, readonly=True)
        try:
            return fn(conn)
        finally:
            conn.close()

    async def _h_health(self, request):
        return web.json_response({"ok": True, "sessions": len(self.sessions)})

    async def _h_index(self, request):
        return web.FileResponse(os.path.join(WEB_DIR, "index.html"))

    async def _h_projector(self, request):
        return web.FileResponse(os.path.join(WEB_DIR, "projector.html"))

    async def _h_rankings(self, request):
        active = self._active_bot_uuids()
        return web.json_response(await self._read(lambda c: scoring.rankings(c, active)))

    async def _h_bots(self, request):
        active = self._active_bot_uuids()
        rows = await self._read(lambda c: [dict(r) for r in c.execute(
            "SELECT uuid, player, name, first_tourney_uuid, last_tourney_uuid, "
            "first_seen_at, last_seen_at FROM bots ORDER BY player, name")])
        for r in rows:
            r["active"] = r["uuid"] in active
        return web.json_response(rows)

    async def _h_bot(self, request):
        uuid = request.match_info["uuid"]
        return web.json_response(await self._read(lambda c: scoring.bot_detail(c, uuid)))

    async def _h_bot_games(self, request):
        uuid = request.match_info["uuid"]
        try:
            limit = max(1, min(50, int(request.query.get("limit", 10))))
        except ValueError:
            return web.json_response({"error": "limit must be an integer"}, status=400)
        return web.json_response(
            await self._read(lambda c: scoring.recent_games(c, uuid, limit)))

    async def _h_game(self, request):
        uuid = request.match_info["uuid"]
        # ?bot= decides which seat is rendered as "you"; it is optional.
        bot = request.query.get("bot")
        replay = await self._read(lambda c: scoring.game_replay(c, uuid, bot))
        if replay is None:
            return web.json_response({"error": "unknown game"}, status=404)
        return web.json_response(replay)

    async def _h_compare(self, request):
        a, b = request.query.get("a"), request.query.get("b")
        if not a or not b:
            return web.json_response({"error": "need ?a= and ?b= bot uuids"}, status=400)
        return web.json_response(await self._read(lambda c: scoring.head_to_head(c, a, b)))

    async def _h_pairings(self, request):
        return web.json_response(await self._read(scoring.pairings))

    async def _h_tourneys(self, request):
        rows = await self._read(lambda c: [dict(r) for r in c.execute(
            "SELECT uuid, started_at, ended_at, status, num_games FROM tourneys "
            "ORDER BY seq DESC LIMIT 100")])
        return web.json_response(rows)

    async def _h_events(self, request):
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        })
        await resp.prepare(request)
        queue = asyncio.Queue()
        self._sse_clients.add(queue)
        try:
            await resp.write(b": connected\n\n")
            while True:
                data = await queue.get()
                await resp.write(f"data: {data}\n\n".encode())
        except (asyncio.CancelledError, ConnectionError):
            pass
        finally:
            self._sse_clients.discard(queue)
        return resp

    def _broadcast(self, payload):
        data = json.dumps(payload)
        for queue in list(self._sse_clients):
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                pass

    async def _h_sync(self, request):
        try:
            games = int(request.query.get("games", 0))
            moves = int(request.query.get("moves", 0))
        except ValueError:
            raise web.HTTPBadRequest(reason="games and moves must be integers")
        include_code = request.query.get("code") == "1"
        data = await asyncio.to_thread(self._read_sync, games, moves, include_code)
        return web.json_response(data)

    def _read_sync(self, games_cursor, moves_cursor, include_code=False):
        conn = connect(self.db.path, readonly=True)
        try:
            return self.db.sync_since(games_cursor, moves_cursor, conn=conn,
                                      include_code=include_code)
        finally:
            conn.close()

    async def _h_code(self, request):
        """One bot's source by content hash. Sync payloads carry `code_hash` but not the
        source, so this is how a mirror resolves it -- once per distinct bot, not once
        per poll."""
        row = await self._read(
            lambda conn: self.db.code_by_hash(request.match_info["code_hash"], conn))
        if row is None:
            raise web.HTTPNotFound(reason="unknown code_hash")
        return web.json_response(row)

    async def _h_db(self, request):
        """Stream a consistent snapshot of the DB.

        A snapshot is a full copy, so it is unlinked the moment it is open: the fd keeps
        the bytes readable while the download runs, and nothing is left behind even if
        the server dies mid-transfer. (FileResponse can't do this -- it stats the path
        during prepare, after the handler returns.) Reads happen off the event loop so a
        multi-GB download can't stall the tournament's round deadlines."""
        dest = os.path.join(self.snapshot_dir, f"snapshot-{new_uuid()}.db")
        await self._write(lambda db: db.snapshot(dest))
        handle = open(dest, "rb")
        try:
            with contextlib.suppress(OSError):
                os.remove(dest)
            resp = web.StreamResponse(headers={
                "Content-Disposition": "attachment; filename=battleship.db",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(os.fstat(handle.fileno()).st_size),
            })
            await resp.prepare(request)
            while True:
                chunk = await asyncio.to_thread(handle.read, 1 << 20)
                if not chunk:
                    break
                await resp.write(chunk)
            await resp.write_eof()
            return resp
        finally:
            handle.close()

    def _clear_snapshots(self):
        """Drop snapshots orphaned by an earlier crash or kill. Only ever deletes inside
        the snapshots dir, and never the live DB itself -- this runs at startup against a
        production database, so it refuses to touch anything it did not create."""
        live = os.path.realpath(self.db.path)
        for name in os.listdir(self.snapshot_dir):
            if not (name.startswith("snapshot-") and name.endswith(".db")):
                continue
            path = os.path.join(self.snapshot_dir, name)
            if os.path.realpath(path) == live:
                continue
            with contextlib.suppress(OSError):
                os.remove(path)

    # -- lifecycle --------------------------------------------------------------

    async def serve(self):
        self.running = True
        self.router = self.ctx.socket(zmq.ROUTER)
        self.router.setsockopt(zmq.HEARTBEAT_IVL, 2000)     # ZMTP keepalive backstop
        self.router.setsockopt(zmq.HEARTBEAT_TIMEOUT, 6000)
        self.router.bind(f"tcp://*:{self.zmq_port}")

        runner = web.AppRunner(self.http_app())
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.http_port)
        await site.start()

        host = _lan_ip()
        log(f"server: ZMQ tcp://{host}:{self.zmq_port}  HTTP http://{host}:{self.http_port}")
        log(f"  web UI:  http://{host}:{self.http_port}/")
        log(f"  API:     http://{host}:{self.http_port}/api/tourneys")
        try:
            await asyncio.gather(self._recv_loop(), self._tourney_loop())
        finally:
            self.running = False
            await runner.cleanup()
            self.router.close(0)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Battleship tournament server.")
    ap.add_argument("--db", default="battleship.db", help="SQLite path (default battleship.db)")
    ap.add_argument("--zmq-port", type=int, default=5555)
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--target", type=int, default=cfg.TARGET_GAMES, help="games/bot/tourney")
    ap.add_argument("--gap-ms", type=int, default=cfg.TOURNEY_GAP_MS, help="pause between tourneys")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args(argv)

    server = Server(args.db, zmq_port=args.zmq_port, http_port=args.http_port,
                    target=args.target, gap_ms=args.gap_ms, seed=args.seed)
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        print("\nshutting down")


if __name__ == "__main__":
    main()
