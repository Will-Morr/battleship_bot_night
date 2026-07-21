"""Bot runner: connects a bot file to a live server and plays. Handles all comms so a
bot file only implements game logic.

    python -m battleship.run_bot bots/random_bot.py --server 192.168.1.10:5555
    python -m battleship.run_bot mybot.py --server host:5555 --mode threads

Modes (how a round's batch of moves is computed):
    single   one move at a time (default; simplest, lowest overhead)
    threads  a thread pool over the batch (helps bots that release the GIL / do I/O)

Both modes work on Linux, WSL, and macOS. The runner uploads the bot's full source at
registration, answers idle pings, and re-registers automatically if the session drops.
"""

import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor

import zmq

from . import protocol as p
from .bot_api import load_bot

RUNNER_VERSION = "1"
RESYNC_AFTER = 5.0  # seconds of silence before assuming the session is stale


def _timed(fn, *args):
    """Run fn(*args), returning (result, elapsed_ms). Exceptions become (None, ms)."""
    start = time.perf_counter()
    try:
        result = fn(*args)
    except Exception as exc:
        print(f"  ! bot error: {exc}")
        result = None
    return result, (time.perf_counter() - start) * 1000.0


class Runner:
    def __init__(self, botfile, server, mode="single", workers=None):
        self.botfile = botfile
        self.endpoint = f"tcp://{server}"
        self.mode = mode
        self.meta, self.factory = load_bot(botfile)
        with open(botfile, "r") as fh:
            self.code = fh.read()
        self.ctx = zmq.Context.instance()
        self.sock = None
        self.pool = ThreadPoolExecutor(max_workers=workers) if mode == "threads" else None
        self.config = None
        self.bots = {}          # handle -> per-game bot instance for the current tourney
        self.last_seen = 0.0

    # -- connection -------------------------------------------------------------

    def _connect(self):
        if self.sock is not None:
            self.sock.close(0)
        self.sock = self.ctx.socket(zmq.DEALER)
        self.sock.setsockopt(zmq.HEARTBEAT_IVL, 2000)
        self.sock.setsockopt(zmq.HEARTBEAT_TIMEOUT, 6000)
        self.sock.connect(self.endpoint)
        self._register()

    def _register(self):
        self._send(p.msg(p.REGISTER, player=self.meta["player"], bot=self.meta["bot"],
                         code=self.code, code_filename=os.path.basename(self.botfile),
                         runner_version=RUNNER_VERSION))
        self.last_seen = time.time()

    def _send(self, message):
        self.sock.send(p.encode(message))

    # -- move computation -------------------------------------------------------

    def _compute(self, calls):
        """calls: list of (handle, fn, arg). Returns {handle: (result, ms)}."""
        if self.pool is None:
            return {h: _timed(fn, arg) for h, fn, arg in calls}
        futures = {h: self.pool.submit(_timed, fn, arg) for h, fn, arg in calls}
        return {h: fut.result() for h, fut in futures.items()}

    # -- message handling -------------------------------------------------------

    def _handle(self, message):
        mtype = message.get("type")
        if mtype == p.REGISTERED:
            self.config = message["config"]
            print(f"registered as {self.meta['player']}/{self.meta['bot']} "
                  f"(session {message['uuid'][:8]})")
        elif mtype == p.PING:
            self._send(p.msg(p.PONG, t=message.get("t")))
        elif mtype == p.PLACE_REQUEST:
            self._on_place(message)
        elif mtype == p.MOVE_REQUEST:
            self._on_move(message)
        elif mtype == p.TOURNEY_START:
            self.bots = {}
        elif mtype == p.TOURNEY_END:
            print(f"tourney {message.get('tourney_id')} complete")
        elif mtype == p.KICK:
            print(f"kicked: {message.get('reason')} — re-registering")
            self._register()

    def _on_place(self, message):
        cfg = message["config"]
        placements = {}
        timings = {}
        for handle in message["games"]:
            bot = self.factory(dict(cfg, game_id=handle))
            self.bots[handle] = bot
            layout, ms = _timed(bot.place_ships)
            if layout is not None:
                placements[str(handle)] = layout
            timings[str(handle)] = ms
        self._send(p.msg(p.PLACE_REPLY, tourney_id=message["tourney_id"],
                         placements=placements, compute_ms=timings))

    def _on_move(self, message):
        calls = []
        for handle, view in message["views"].items():
            bot = self.bots.get(int(handle))
            if bot is not None:
                calls.append((handle, bot.make_move, view))
        computed = self._compute(calls)
        moves = {h: r for h, (r, _ms) in computed.items() if r is not None}
        timings = {h: ms for h, (_r, ms) in computed.items()}
        self._send(p.msg(p.MOVE_REPLY, tourney_id=message["tourney_id"],
                         moves=moves, compute_ms=timings))

    # -- main loop --------------------------------------------------------------

    def run(self):
        self._connect()
        print(f"connected to {self.endpoint} (mode={self.mode}); waiting for tourneys...")
        poller = zmq.Poller()
        poller.register(self.sock, zmq.POLLIN)
        try:
            while True:
                events = dict(poller.poll(1000))
                if self.sock in events:
                    self._handle(p.decode(self.sock.recv()))
                    self.last_seen = time.time()
                elif time.time() - self.last_seen > RESYNC_AFTER:
                    # Silence for too long: assume the session lapsed and re-register.
                    self._register()
        except KeyboardInterrupt:
            print("\nstopping")
            try:
                self._send(p.msg(p.BYE))
            except Exception:
                pass
        finally:
            if self.pool is not None:
                self.pool.shutdown(wait=False)
            self.sock.close(0)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run a battleship bot against the server.")
    ap.add_argument("botfile", help="path to the bot file")
    ap.add_argument("--server", default=os.environ.get("BATTLESHIP_SERVER", "localhost:5555"),
                    help="server address host:port (default localhost:5555 / $BATTLESHIP_SERVER)")
    ap.add_argument("--mode", choices=("single", "threads"), default="single")
    ap.add_argument("--workers", type=int, default=None, help="thread pool size for --mode threads")
    args = ap.parse_args(argv)

    Runner(args.botfile, args.server, mode=args.mode, workers=args.workers).run()


if __name__ == "__main__":
    main()
