"""UUIDv7 ids (RFC 9562): time-ordered, so B-tree inserts stay append-like.

Python 3.14 ships uuid.uuid7; this keeps 3.12/3.13 support. Ids generated in
the same millisecond by one process are monotonic (12-bit counter in rand_a).
"""

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = 0
_counter = 0


def uuid7() -> uuid.UUID:
    global _last_ms, _counter
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms > _last_ms:
            _last_ms = ms
            _counter = int.from_bytes(os.urandom(2)) & 0x3FF  # leave headroom
        else:
            ms = _last_ms
            _counter += 1
            if _counter > 0xFFF:  # counter exhausted: borrow the next millisecond
                _last_ms += 1
                ms = _last_ms
                _counter = 0
        counter = _counter
    rand_b = int.from_bytes(os.urandom(8)) & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= counter << 64
    value |= 0b10 << 62
    value |= rand_b
    return uuid.UUID(int=value)


def uuid7_time_ms(u: uuid.UUID) -> int:
    """Unix time in milliseconds encoded in a UUIDv7."""
    return u.int >> 80
