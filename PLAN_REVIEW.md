# Plan Review — Open Concerns

Review of `PLAN.md` / `DATA_CONTRACTS.md` as of commit `a067d6d`. Ordered by impact.

## 1. Mid-tourney bot restart is unhandled

Every registration gets a fresh `uuid`/session (`DATA_CONTRACTS.md` §3, §4 `bot_sessions`).
The event's whole premise is players iterating live and re-running their runner script.
If a player restarts mid-tourney while their old session still has in-flight games, the
old session goes silent and those games forfeit via blackout (`DATA_CONTRACTS.md` §3
"Timeout & liveness"). There's no "reconnect resumes my current games" path — only
"new session waits for next tourney" (`PLAN.md` runner phase, implied by `prompt.md`
line 1). During a live event this means every bugfix-and-rerun costs the player all
currently-running games in that tourney.

**Decide explicitly:** either accept this as the tradeoff and tell players up front, or
add a short-grace reconnect that re-attaches a new session to the old session's
remaining games.

## 2. Pairing algorithm is unspecified

`tourney.py` is described only as "pairing algorithm + synchronous round engine"
(`PLAN.md` repo layout + phase 4). The actual constraint set — no same-player
pairings, ~50 games/bot target, presumably uneven bot counts as players join
mid-event — is a real combinatorial problem (regular-graph-ish generation with
forbidden edges), not a one-liner. Degenerate cases worth sketching before phase 4:

- Odd number of bots.
- A single player who owns most of the bots in the room.
- A bot that joins after several tourneys have already run (uneven total game counts).

## 3. SQLite writes could block the single event loop

The architecture is explicitly one asyncio loop for ZMQ + aiohttp (`PLAN.md`
"Architecture at a glance"), and latency is the top stated priority (`prompt.md`
line 5). `db.py` batches writes but doesn't say how. If it uses stdlib `sqlite3`
synchronously, every batched write blocks the same loop that's enforcing
`deadline_ms`. Commit now to `aiosqlite` or `asyncio.to_thread` for writes so a DB
flush never eats into the round deadline.

## 4. Game-termination rule has a gap

"If both sides are out, the game ends" (`DATA_CONTRACTS.md` §6) doesn't obviously
cover the common case of one side finishing normally and the other later timing
out or dropping — that's "one side out, one side already-finished-not-out," not
"both out." The rule actually needed is closer to: *end the game when no side has
a move left to make* (finished-normally and forfeited both count as "no move
left").

## Minor

- **Tie on double-DNF:** the win-rate tie rule doesn't say what happens when
  both sides fail to solve (probably a tie, but not stated in `DATA_CONTRACTS.md`
  §6).
- **`opponent_last` after opponent finishes:** once the opponent has solved and
  stops shooting, is `opponent_last` permanently `null` in later views, or does
  it repeat their last real shot? Likely obvious in implementation but worth one
  line in the bot API contract (`DATA_CONTRACTS.md` §2) since bots will rely on it.

## Not a concern

The wire protocol, UUID/seq dual-key schema, whole-batch deadline design, and
scoring definitions are solid and internally consistent — no changes suggested
there.

---

## Resolutions (folded into the contracts/plan)

1. **Mid-tourney restart:** accepted as a tradeoff, made cheap by short tourneys;
   re-registering a live logical bot now *immediately retires* the old session
   (no hanging opponents). Grace-window resume deferred (would need full-history
   resync). See `DATA_CONTRACTS.md` §3 "Session restart & re-registration".
2. **Pairing:** algorithm + the three degenerate cases now specified in `PLAN.md`
   "Key mechanisms > Pairing" (shuffle slot-multiset, swap out same-player collisions,
   drop the dominant-player tail).
3. **DB writes:** committed to off-loop — dedicated writer thread + queue via executor,
   separate WAL reader in `to_thread`. `PLAN.md` "Key mechanisms > DB writes".
4. **Termination:** rewritten to "ends when no side is still `solving`" (finished and
   forfeited both count as no-move-left). `DATA_CONTRACTS.md` §6.
5. **Double-DNF / both-forfeit:** no winner, loss for both; tie is only mutual success
   on the same round. `DATA_CONTRACTS.md` §6.
6. **`opponent_last` after opponent stops:** documented as `null` on any round the
   opponent didn't shoot. `DATA_CONTRACTS.md` §2.
