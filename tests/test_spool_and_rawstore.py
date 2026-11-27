import hashlib

import pytest

from pos_coordinator.rawstore import HashMismatch, RawStore
from pos_worker.spool import Spool


def test_spool_survives_reopen(tmp_path):
    path = tmp_path / "spool.db"
    s = Spool(path, max_results=2)
    s.add_result("a", 1, {"job_id": "a"})
    s.add_result("b", 1, {"job_id": "b"})
    assert s.full
    s.close()
    s = Spool(path, max_results=2)
    assert [r["job_id"] for r in s.peek_results(10)] == ["a", "b"]
    s.remove_results([("a", 1)])
    assert s.result_count() == 1


def test_spool_remove_keeps_newer_attempt(tmp_path):
    s = Spool(":memory:", max_results=10)
    s.add_result("a", 1, {"v": 1})
    s.add_result("a", 2, {"v": 2})  # newer attempt replaces the older pending one
    s.remove_results([("a", 1)])  # ack for the batch that carried attempt 1
    assert s.peek_results(10) == [{"v": 2}]


def test_spool_commands_at_most_once(tmp_path):
    s = Spool(":memory:", max_results=10)
    assert not s.command_seen("c1")
    s.record_command("c1", True, {"x": 1})
    s.record_command("c1", False, "again")  # ignored
    assert s.command_seen("c1")
    assert s.unreported_commands() == [{"id": "c1", "ok": True, "output": {"x": 1}}]
    s.mark_commands_reported(["c1"])
    assert s.unreported_commands() == []


def test_rawstore_content_addressed(tmp_path):
    store = RawStore(tmp_path)
    data = b"evidence"
    h = hashlib.sha256(data).hexdigest()
    assert store.put("snippets", h, data) is True
    assert store.put("snippets", h, data) is False
    assert store.get("snippets", h) == data
    assert store.path_for("snippets", h).relative_to(tmp_path).parts[:3] == (
        "snippets",
        h[:2],
        h[2:4],
    )
    with pytest.raises(HashMismatch):
        store.put("snippets", "0" * 64, data)
    with pytest.raises(ValueError):
        store.path_for("snippets", "../../etc/passwd")
