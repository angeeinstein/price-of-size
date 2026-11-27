import time

from pos_shared.ids import uuid7, uuid7_time_ms
from pos_shared.version import is_compatible


def test_uuid7_layout():
    u = uuid7()
    assert u.version == 7
    assert u.variant == "specified in RFC 4122"


def test_uuid7_time_and_order():
    before = time.time_ns() // 1_000_000
    ids = [uuid7() for _ in range(10_000)]
    after = time.time_ns() // 1_000_000
    assert ids == sorted(ids), "ids from one process must be monotonic"
    assert len(set(ids)) == len(ids)
    assert before <= uuid7_time_ms(ids[0]) <= after + 5


def test_api_compatibility():
    assert is_compatible("1.0", "1.0")
    assert is_compatible("1.0", "1.3")  # coordinator added endpoints
    assert not is_compatible("1.4", "1.3")  # worker expects something newer
    assert not is_compatible("2.0", "1.9")
    assert not is_compatible("garbage", "1.0")
