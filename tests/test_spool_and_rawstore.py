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


async def test_interrupted_upload_is_completed_by_the_next_one(app, admin, tmp_path, sessionmaker):
    """A file without its database row (upload interrupted between the two) must not
    make the next uploader skip the upload."""
    import zstandard
    from sqlalchemy import select

    from pos_coordinator.models import RawObject
    from tests.helpers import make_agent

    worker = await make_agent(app, admin, tmp_path / "w", "w")
    blob = zstandard.ZstdCompressor().compress(b"evidence")
    sha = hashlib.sha256(blob).hexdigest()
    app.state.rawstore.put("snippets", sha, blob)  # file written, row never committed
    await worker.client.upload_raw("snippets", sha, blob)
    async with sessionmaker() as s:
        assert (await s.execute(select(RawObject.sha256))).scalar_one() == sha
    await worker.client.aclose()


def test_rawstore_identity_guards_against_an_unmounted_nas(tmp_path):
    import pytest

    from pos_coordinator.rawstore import RawStore, StoreUnavailable

    nas = RawStore(tmp_path / "raw")
    store_id = nas.bind(None)  # first start: a new store
    data = b"x"
    sha = hashlib.sha256(data).hexdigest()
    assert nas.put("snippets", sha, data) and nas.available()
    # later: the NAS is not mounted, the coordinator sees an empty mount point
    empty = RawStore(tmp_path / "mountpoint")
    (tmp_path / "mountpoint").mkdir()
    assert empty.bind(store_id) == store_id and not empty.available()
    with pytest.raises(StoreUnavailable):
        empty.put("snippets", sha, data)
    with pytest.raises(StoreUnavailable):
        empty.exists("snippets", sha)
    assert not list((tmp_path / "mountpoint").iterdir())  # nothing written to local disk
    # a restart with the NAS back adopts nothing new and works
    again = RawStore(tmp_path / "raw")
    assert again.bind(store_id) == store_id and again.exists("snippets", sha)
