"""The LLM service with a fake Claude API client: budget, cache, validation, batches."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from sqlalchemy import select

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
    batch_id, taken = await llm.submit_batch([task(0, "known"), task(1, "a"), task(2, "b")])
    assert taken == 3 and got == [("ok", 0, True, "cached")]
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
    _, first = await llm.submit_batch(tasks)
    assert 0 < first < 100  # only what the budget covers
    _, second = await llm.submit_batch(tasks[first:])
    assert second == 0  # the running batch's estimate already uses the rest
