import datetime as dt

import msgpack

from agent.host.streams import _ts


def test_ts_accepts_msgpack_timestamp() -> None:
    raw = msgpack.Timestamp(1_788_183_933, 123_456_789)

    parsed = _ts(raw)

    assert parsed == dt.datetime(
        2026, 8, 31, 13, 45, 33, 123_456, tzinfo=dt.timezone.utc
    )
