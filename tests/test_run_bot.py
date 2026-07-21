"""Launches the real `battleship.server` and two `battleship.run_bot` processes and
verifies they play tourneys that land in the DB. Exercises the actual CLI entrypoints
end-to-end (registration, play loop, DB writes) as separate OS processes."""

import os
import socket
import subprocess
import sys
import time

from battleship.db import connect

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RANDOM_BOT = os.path.join(REPO, "bots", "random_bot.py")
ORDERED_BOT = os.path.join(REPO, "bots", "ordered_bot.py")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def spawn(args, log):
    env = {**os.environ, "PYTHONPATH": REPO}
    return subprocess.Popen([sys.executable, "-m", *args], cwd=REPO, env=env,
                            stdout=log, stderr=subprocess.STDOUT)


def test_real_processes_play_tourneys(tmp_path):
    db = str(tmp_path / "live.db")
    zmq_port, http_port = free_port(), free_port()
    logs = {name: open(tmp_path / f"{name}.log", "w") for name in ("server", "r1", "r2")}

    server = spawn(["battleship.server", "--db", db, "--zmq-port", str(zmq_port),
                    "--http-port", str(http_port), "--target", "5", "--gap-ms", "100",
                    "--seed", "1"], logs["server"])
    time.sleep(1.0)  # let it bind
    runners = [
        spawn(["battleship.run_bot", RANDOM_BOT, "--server", f"localhost:{zmq_port}"], logs["r1"]),
        spawn(["battleship.run_bot", ORDERED_BOT, "--server", f"localhost:{zmq_port}"], logs["r2"]),
    ]
    procs = [server, *runners]
    try:
        games = 0
        deadline = time.time() + 20
        while time.time() < deadline:
            time.sleep(0.3)
            if any(pr.poll() is not None for pr in procs):
                break  # something died; assert below will surface the logs
            if os.path.exists(db):
                try:
                    conn = connect(db, readonly=True)
                    games = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
                    conn.close()
                except Exception:
                    pass
                if games >= 5:
                    break
        if games < 5:
            for name, fh in logs.items():
                fh.flush()
                print(f"--- {name} ---\n{open(tmp_path / f'{name}.log').read()}")
        assert games >= 5, f"only {games} games recorded"
    finally:
        for pr in procs:
            pr.terminate()
        for pr in procs:
            try:
                pr.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pr.kill()
        for fh in logs.values():
            fh.close()
