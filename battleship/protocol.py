"""Wire protocol: message type names + JSON (de)serialization. See DATA_CONTRACTS.md §3.

Every message is a single JSON object with a "type" field. ZeroMQ ROUTER identity
frames are handled by the socket layer and never appear in these payloads.
"""

import json

# client -> server
REGISTER = "register"
PLACE_REPLY = "place_reply"
MOVE_REPLY = "move_reply"
PONG = "pong"
BYE = "bye"

# server -> client
REGISTERED = "registered"
TOURNEY_START = "tourney_start"
PLACE_REQUEST = "place_request"
MOVE_REQUEST = "move_request"
GAME_RESULT = "game_result"
TOURNEY_END = "tourney_end"
IDLE = "idle"
PING = "ping"
KICK = "kick"


def msg(type_, **fields):
    """Build a message dict with its type set."""
    fields["type"] = type_
    return fields


def encode(message):
    """Message dict -> compact UTF-8 bytes for the wire."""
    return json.dumps(message, separators=(",", ":")).encode("utf-8")


def decode(raw):
    """Wire bytes (or str) -> message dict."""
    return json.loads(raw)
