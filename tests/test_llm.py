"""The LLM service with a fake Claude API client: budget, cache, validation, batches."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, SecretStr
from sqlalchemy import func, select

from pos_coordinator import runtime_settings
from pos_coordinator.budget import Priority
from pos_coordinator.llm import pricing, service
from pos_coordinator.llm.service import BudgetDeferred, LlmService, LlmUnavailable, Task
from pos_coordinator.models import LlmBatch, LlmCall


class Pick(BaseModel):
    category: str
    confidence: float


class Nested(BaseModel):
    pick: Pick
    alternatives: list[Pick]
    note: str | None


def _msg(text, stop="end_turn", model="claude-haiku-5-5", tokens=(1000, 50)):
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        stop_reason=stop,
        stop_details=SimpleNamespace(category="cyber") if stop == "refusal" else None,
        model=model,
        usage=SimpleNamespace(
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


class FakeClient:
    def __init__(self, answers=()):
        self.answers = list(answers)
        self.calls = []
        self.batches = {}
        client = self

        class Messages:
            async def create(self, **params):
                client.calls.append(("create", params))
                return client.answers.pop(0)

            class batches:  # noqa: N801
                @staticmethod
                async def create(requests):
                    bid = f"msgbatch_{len(client.batches) + 1}"
                    client.batches[bid] = {"requests": requests, "status": "in_progress"}
                    return SimpleNamespace(id=bid)

                @staticmethod
                async def retrieve(bid):
                    return SimpleNamespace(processing_status=client.batches[bid]["status"])

                @staticmethod
                async def results(bid):
                    async def gen():
                        for r in client.batches[bid]["results"]:
                            yield r

                    return gen()

        class Beta:
            class messages:  # noqa: N801
                @staticmethod
                async def create(**params):
                    client.calls.append(("beta", params))
                    return client.answers.pop(0)

        self.messages = Messages()
        self.beta = Beta()


def _task(prompt="Regal 80 x 40 cm", **kw):
    return Task(stage="test", system="Classify the product.", prompt=prompt, schema=Pick, **kw)


def test_strict_schema_closes_objects_and_inlines_references():
    s = service.strict_schema(Nested)
    assert s["additionalProperties"] is False
    assert s["required"] == ["pick", "alternatives", "note"]
    pick = s["properties"]["pick"]
    assert pick["type"] == "object" and pick["additionalProperties"] is False
    assert s["properties"]["alternatives"]["items"]["required"] == ["category", "confidence"]
    assert "$ref" not in str(s) and "title" not in str(s)


def test_pricing():
    assert pricing.cost_usd("claude-haiku-5-5", 50_000, 50_000) == Decimal("0.03")
    assert pricing.cost_usd("claude-haiku-5-5", 100_000, 0, batch=True) == Decimal("0.005")
    # beyond 100k prompt tokens Haiku 5.5 costs more
    assert pricing.cost_usd("claude-haiku-5-5", 200_000, 0) == Decimal("0.1")
    assert pricing.cost_usd("claude-sonnet-5-5", 0, 0, cache_read_tokens=1_000_000) == Decimal(
        "0.2"
    )
    assert pricing.cost_usd("claude-sonnet-5-5", 0, 0, cache_write_tokens=1_000_000) == Decimal(
        "2.5"
    )
    # an unknown model is never free
    assert pricing.cost_usd("claude-new-9", 1_000_000, 0) == pricing.MOST_EXPENSIVE.input


async def _ledger(sm):
    async with sm() as s:
        return (await s.execute(select(LlmCall).order_by(LlmCall.created_at))).scalars().all()


async def test_answers_are_validated_cached_and_recorded(sessionmaker):
    fake = FakeClient([_msg('{"category": "Regale", "confidence": 0.9}')])
    llm = LlmService(sessionmaker, lambda: fake)
    out = await llm.run(_task())
    assert out.status == "ok" and out.data.category == "Regale" and not out.cached
    params = fake.calls[0][1]
    assert params["model"] == "claude-haiku-5-5"
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert params["output_config"]["effort"] == "low"
    again = await llm.run(_task())  # the same input: from the ledger, no call
    assert again.cached and again.data.category == "Regale" and len(fake.calls) == 1
    (row,) = await _ledger(sessionmaker)
    assert row.stage == "test" and row.cost_usd == Decimal("0.000125")
    assert row.output["status"] == "ok"


async def test_an_invalid_answer_is_retried_once_then_flagged(sessionmaker):
    fake = FakeClient([_msg("not json"), _msg('{"category": "Regale", "confidence": 1}')])
    out = await LlmService(sessionmaker, lambda: fake).run(_task())
    assert out.status == "ok" and out.cost_usd == Decimal("0.000250")
    fake = FakeClient([_msg("{}"), _msg('{"cat": 1}', stop="max_tokens")])
    out = await LlmService(sessionmaker, lambda: fake).run(_task("other"))
    assert out.status == "invalid" and out.data is None
    statuses = [r.output["status"] for r in await _ledger(sessionmaker)]
    assert statuses == ["invalid", "ok", "invalid", "invalid"]  # every call is paid for


async def test_refusals_and_the_fallback_path(sessionmaker):
    fake = FakeClient([_msg("", stop="refusal")])
    out = await LlmService(sessionmaker, lambda: fake).run(_task())
    assert out.status == "refused" and "cyber" in out.detail
    # a model with the server-side fallback goes through it
    fake = FakeClient([_msg('{"category": "x", "confidence": 0.5}', model="claude-sonnet-5")])
    out = await LlmService(sessionmaker, lambda: fake).run(_task(model_role="category_mapping"))
    kind, params = fake.calls[0]
    assert kind == "beta" and params["fallbacks"] == "default"
    assert params["betas"] == [service.FALLBACK_BETA] and params["model"] == "claude-sonnet-5-5"
    assert out.model == "claude-sonnet-5"  # answered by the fallback model


async def test_the_budget_defers_work(sessionmaker):
    async with sessionmaker() as s:
        await runtime_settings.put(s, "llm_budget_hard_cap_usd", 0, "test")
        await s.commit()
    fake = FakeClient()
    with pytest.raises(BudgetDeferred):
        await LlmService(sessionmaker, lambda: fake).run(_task(priority=Priority.high))
    assert fake.calls == []


async def test_without_a_key_the_service_is_unavailable(sessionmaker, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def no_key():
        raise LlmUnavailable("no key")

    llm = LlmService(sessionmaker, no_key)
    assert not llm.available()
    with pytest.raises(LlmUnavailable):
        await llm.run(_task())


async def test_batches_are_submitted_within_budget_and_collected(sessionmaker):
    got = []

    @service.register("test_batch", Pick)
    async def handler(session, outcome):
        got.append((outcome.status, outcome.meta["i"], outcome.cached,
                    outcome.data.category if outcome.data else None))  # fmt: skip

    fake = FakeClient([_msg('{"category": "cached", "confidence": 1}')])
    llm = LlmService(sessionmaker, lambda: fake)

    def task(i, prompt):
        return Task(stage="test_batch", system="S", prompt=prompt, schema=Pick, meta={"i": i},
                    priority=Priority.high)  # fmt: skip

    await llm.run(task(0, "known"))  # answered before: no batch request needed
    batch_id, left = await llm.submit_batch([task(0, "known"), task(1, "a"), task(2, "b")])
    assert left == [] and got == [("ok", 0, True, "cached")]
    (bid,) = fake.batches
    reqs = fake.batches[bid]["requests"]
    assert [r["custom_id"] for r in reqs] == ["0", "1"]
    assert reqs[0]["params"]["messages"][0]["content"] == "a"
    assert await llm.poll_batches() == 0  # still running
    fake.batches[bid]["status"] = "ended"
    fake.batches[bid]["results"] = [
        SimpleNamespace(
            custom_id="0",
            result=SimpleNamespace(
                type="succeeded", message=_msg('{"category": "A", "confidence": 1}')
            ),
        ),  # fmt: skip
        SimpleNamespace(custom_id="1", result=SimpleNamespace(type="expired")),
    ]
    assert await llm.poll_batches() == 2
    assert got[1:] == [("ok", 1, False, "A"), ("error", 2, False, None)]
    assert await llm.poll_batches() == 0  # collected once
    async with sessionmaker() as s:
        row = await s.get(LlmBatch, batch_id)
        assert row.status == "ended" and row.request_count == 2
        batch_rows = (await s.execute(select(LlmCall).where(LlmCall.batch))).scalars().all()
    assert [r.cost_usd for r in batch_rows] == [Decimal("0.000063")]  # half price


async def test_running_batches_count_against_the_budget(sessionmaker):
    @service.register("test_budget", Pick)
    async def handler(session, outcome):
        pass

    async with sessionmaker() as s:
        await runtime_settings.put(s, "llm_budget_hard_cap_usd", 0.01, "test")
        await s.commit()
    fake = FakeClient()
    llm = LlmService(sessionmaker, lambda: fake)
    tasks = [
        Task(
            stage="test_budget",
            system="S" * 3000,
            prompt=f"p{i}",
            schema=Pick,
            priority=Priority.high,
            max_tokens=4096,
        )  # fmt: skip
        for i in range(100)
    ]
    _, left = await llm.submit_batch(tasks)
    assert 0 < len(left) < 100  # only what the budget covers went in
    assert left == tasks[100 - len(left) :]
    batch_id, again = await llm.submit_batch(left)
    assert batch_id is None and again == left  # the running batch's estimate uses the rest


class Titled(BaseModel):
    title: str
    default: int


def test_fields_named_like_schema_metadata_stay():
    s = service.strict_schema(Titled)
    assert set(s["properties"]) == {"title", "default"}
    assert s["required"] == ["title", "default"]


async def test_running_batches_are_reserved_for_synchronous_calls(sessionmaker):
    async with sessionmaker() as s:
        await runtime_settings.put(s, "llm_budget_hard_cap_usd", 1, "test")
        s.add(LlmBatch(remote_id="msgbatch_x", stage="x", model="claude-haiku-5-5",
                       request_count=1, estimated_cost_usd=Decimal("0.9999")))  # fmt: skip
        await s.commit()
    fake = FakeClient()
    with pytest.raises(BudgetDeferred):
        await LlmService(sessionmaker, lambda: fake).run(_task(priority=Priority.high))
    assert fake.calls == []


async def test_a_retry_is_checked_against_the_budget_again(sessionmaker):
    async with sessionmaker() as s:
        await runtime_settings.put(s, "llm_budget_hard_cap_usd", 0.0015, "test")
        await s.commit()
    # the first, invalid answer costs more than the whole budget
    fake = FakeClient(
        [_msg("nope", tokens=(20_000, 10)), _msg('{"category": "x", "confidence": 1}')]
    )
    with pytest.raises(BudgetDeferred):
        await LlmService(sessionmaker, lambda: fake).run(
            _task(priority=Priority.high, max_tokens=100)
        )
    assert len(fake.calls) == 1  # no second paid attempt
    assert [r.output["status"] for r in await _ledger(sessionmaker)] == ["invalid"]


async def test_an_answer_from_a_fallback_model_is_reused(sessionmaker):
    fake = FakeClient([_msg('{"category": "x", "confidence": 0.5}', model="claude-sonnet-5")])
    llm = LlmService(sessionmaker, lambda: fake)
    await llm.run(_task(model_role="category_mapping"))
    again = await llm.run(_task(model_role="category_mapping"))
    assert again.cached and again.data.category == "x" and len(fake.calls) == 1


class Gate:
    """A fake API call that waits until released: calls in flight at the same time."""

    def __init__(self, fake):
        self.fake = fake
        self.release = asyncio.Event()
        self.entered = 0
        inner = fake.messages.create
        inner_batch = fake.messages.batches.create

        async def create(**params):
            self.entered += 1
            await self.release.wait()
            return await inner(**params)

        async def create_batch(requests):
            self.entered += 1
            await self.release.wait()
            return await inner_batch(requests)

        fake.messages.create = create
        fake.messages.batches.create = create_batch


async def test_concurrent_calls_cannot_spend_the_same_money(sessionmaker):
    task = _task(priority=Priority.high, max_tokens=1000)
    est = service.estimate_cost("claude-haiku-5-5", task, batch=False)
    async with sessionmaker() as s:  # room for one call, not two
        await runtime_settings.put(s, "llm_budget_hard_cap_usd", float(est * 3 / 2), "test")
        await s.commit()
    fake = FakeClient([_msg('{"category": "a", "confidence": 1}')] * 2)
    gate = Gate(fake)
    llm = LlmService(sessionmaker, lambda: fake)
    first = asyncio.create_task(llm.run(task))
    while gate.entered == 0:  # the first call is in flight, its estimate reserved
        await asyncio.sleep(0.01)
    with pytest.raises(BudgetDeferred):
        await llm.run(_task("another", priority=Priority.high, max_tokens=1000))
    gate.release.set()
    assert (await first).status == "ok"
    (row,) = await _ledger(sessionmaker)  # the reservation became the actual cost
    assert row.output["status"] == "ok" and row.cost_usd == Decimal("0.000125")


async def test_a_batch_being_submitted_is_already_reserved(sessionmaker):
    @service.register("test_reserve", Pick)
    async def handler(session, outcome):
        pass

    async with sessionmaker() as s:
        await runtime_settings.put(s, "llm_budget_hard_cap_usd", 0.01, "test")
        await s.commit()
    fake = FakeClient()
    gate = Gate(fake)
    llm = LlmService(sessionmaker, lambda: fake)
    tasks = [Task(stage="test_reserve", system="S" * 3000, prompt=f"p{i}", schema=Pick,
                  priority=Priority.high, max_tokens=4096) for i in range(100)]  # fmt: skip
    first = asyncio.create_task(llm.submit_batch(tasks))
    while gate.entered == 0:  # the request is on its way, the row not yet submitted
        await asyncio.sleep(0.01)
    with pytest.raises(BudgetDeferred):  # nor may a synchronous call use that money
        await llm.run(Task(stage="test_reserve", system="S" * 3000, prompt="sync",
                           schema=Pick, priority=Priority.high, max_tokens=4096))  # fmt: skip
    bid, left = await llm.submit_batch(tasks[50:])
    assert bid is None and left == tasks[50:]
    gate.release.set()
    batch_id, _ = await first
    async with sessionmaker() as s:
        row = await s.get(LlmBatch, batch_id)
    assert row.status == "submitted" and row.remote_id == "msgbatch_1"


async def test_cached_answers_reach_the_handler_without_budget(sessionmaker):
    got = []

    @service.register("test_cached", Pick)
    async def handler(session, outcome):
        got.append((outcome.meta["i"], outcome.cached))

    fake = FakeClient([_msg('{"category": "x", "confidence": 1}')])
    llm = LlmService(sessionmaker, lambda: fake)

    def task(i, prompt):
        return Task(stage="test_cached", system="S", prompt=prompt, schema=Pick, meta={"i": i})

    await llm.run(task(0, "known"))
    async with sessionmaker() as s:
        await runtime_settings.put(s, "llm_budget_hard_cap_usd", 0, "test")
        await s.commit()
    new = task(2, "new")
    bid, left = await llm.submit_batch([task(1, "known"), new])
    assert bid is None and left == [new] and got == [(1, True)]  # no budget: no batch
    assert fake.batches == {}


class ApiError(Exception):
    status_code = 529


async def test_failed_calls_release_or_keep_their_reservation(sessionmaker):
    fake = FakeClient()
    llm = LlmService(sessionmaker, lambda: fake)

    async def refuse(**params):
        raise ApiError("overloaded")

    fake.messages.create = refuse
    with pytest.raises(ApiError):
        await llm.run(_task("a", priority=Priority.high))

    async def lost(**params):
        raise TimeoutError

    fake.messages.create = lost
    with pytest.raises(TimeoutError):
        await llm.run(_task("b", priority=Priority.high))
    answered, unknown = await _ledger(sessionmaker)
    assert answered.cost_usd == 0 and answered.output["status"] == "error"
    assert unknown.cost_usd > 0 and "unknown" in unknown.output["detail"]  # may be billed


async def test_an_interrupted_batch_submission_keeps_its_estimate(sessionmaker):
    @service.register("test_interrupted", Pick)
    async def handler(session, outcome):
        pass

    fake = FakeClient()
    llm = LlmService(sessionmaker, lambda: fake)

    async def rejected(requests):
        raise ApiError("invalid")

    fake.messages.batches.create = rejected
    t = Task(stage="test_interrupted", system="S", prompt="p", schema=Pick,
             priority=Priority.high)  # fmt: skip
    with pytest.raises(ApiError):
        await llm.submit_batch([t])

    async def lost(requests):
        raise TimeoutError

    fake.messages.batches.create = lost
    with pytest.raises(TimeoutError):
        await llm.submit_batch([t])
    async with sessionmaker() as s:
        rows = (await s.execute(select(LlmBatch).order_by(LlmBatch.created_at))).scalars().all()
        assert [r.status for r in rows] == ["failed", "submitting"]
        rows[1].created_at -= service.STALE_SUBMISSION * 2
        await s.commit()
    assert await llm.poll_batches() == 0
    async with sessionmaker() as s:
        assert (await s.get(LlmBatch, rows[1].id)).status == "failed"
    (row,) = await _ledger(sessionmaker)
    assert row.batch and row.cost_usd == rows[1].estimated_cost_usd


def test_the_estimate_counts_the_answer_schema():
    class Wide(BaseModel):
        model_config = {"json_schema_extra": {"description": "x" * 30_000}}
        a: str

    small = service.estimate_cost("claude-haiku-5-5", _task(), batch=False)
    task = Task(stage="test", system="Classify the product.", prompt="Regal 80 x 40 cm",
                schema=Wide)  # fmt: skip
    # 30,000 more characters of schema: about 10,000 more input tokens
    assert service.estimate_cost("claude-haiku-5-5", task, batch=False) - small >= Decimal("0.0009")


async def test_the_application_key_is_used(settings, monkeypatch):
    from pos_coordinator.app import create_app

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("POS_ANTHROPIC_API_KEY", raising=False)
    configured = settings.model_copy(update={"anthropic_api_key": SecretStr("sk-ant-test")})
    app = create_app(configured, run_maintenance=False)
    async with app.router.lifespan_context(app):
        assert app.state.llm.available()
        assert app.state.llm.client.api_key == "sk-ant-test"
    app = create_app(settings, run_maintenance=False)
    async with app.router.lifespan_context(app):
        assert not app.state.llm.available()


async def test_a_batch_holds_one_model_role_and_priority(sessionmaker):
    llm = LlmService(sessionmaker, lambda: FakeClient())
    with pytest.raises(ValueError):
        await llm.submit_batch([_task(), _task("other", model_role="category_mapping")])
    with pytest.raises(ValueError):
        await llm.submit_batch([_task(), _task("other", priority=Priority.high)])


async def test_collecting_a_batch_waits_for_the_budget_lock(sessionmaker):
    @service.register("test_lock", Pick)
    async def handler(session, outcome):
        pass

    fake = FakeClient()
    llm = LlmService(sessionmaker, lambda: fake)
    t = Task(stage="test_lock", system="S", prompt="p", schema=Pick, priority=Priority.high)
    await llm.submit_batch([t])
    (bid,) = fake.batches
    fake.batches[bid]["status"] = "ended"
    fake.batches[bid]["results"] = [
        SimpleNamespace(
            custom_id="0",
            result=SimpleNamespace(
                type="succeeded", message=_msg('{"category": "a", "confidence": 1}')
            ),
        )
    ]
    async with sessionmaker() as holder:  # an admission in progress
        await holder.execute(select(func.pg_advisory_xact_lock(service.BUDGET_LOCK)))
        poll = asyncio.create_task(llm.poll_batches())
        await asyncio.sleep(0.3)
        assert not poll.done()
        await holder.commit()
    assert await poll == 1


def test_the_estimate_covers_a_cache_write():
    task = Task(stage="test", system="S" * 300_000, prompt="p", schema=Pick, max_tokens=100,
                model_role="category_mapping")  # fmt: skip
    worst = pricing.cost_usd("claude-sonnet-5-5", 1, 100, cache_write_tokens=100_000)
    assert service.estimate_cost("claude-sonnet-5-5", task, batch=False) >= worst


class TreeNode(BaseModel):
    name: str
    children: list["TreeNode"]


def test_a_recursive_schema_is_refused_not_expanded_forever():
    with pytest.raises(ValueError, match="recursive"):
        service.strict_schema(TreeNode)

    # the same model twice side by side is not recursion
    class Pair(BaseModel):
        a: Pick
        b: Pick

    s = service.strict_schema(Pair)
    assert s["properties"]["a"] == s["properties"]["b"]
