"""Tests for protocol encode/decode and message building."""

from battleship import protocol


def test_encode_decode_roundtrip():
    m = protocol.msg(protocol.MOVE_REPLY, tourney_id=4, moves={"101": [3, 4]})
    raw = protocol.encode(m)
    assert isinstance(raw, bytes)
    assert protocol.decode(raw) == m


def test_msg_sets_type():
    m = protocol.msg(protocol.REGISTER, player="will", bot="greedy")
    assert m["type"] == "register" and m["player"] == "will"


def test_encode_is_compact():
    raw = protocol.encode({"type": "ping", "t": 1})
    assert b" " not in raw
