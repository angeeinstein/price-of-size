"""Calls to the Claude API: budgeted, cached by input, strictly structured.

Every stage that needs the LLM describes its work as a `Task`: stable instructions
(cached by the API), the varying input, and a pydantic schema the answer must
satisfy. The service

* answers from the ledger when the same input was answered before (no cost),
* checks the monthly budget for the task's priority and raises `BudgetDeferred`
  when the work has to wait (the caller leaves it queued),
* asks for JSON in the schema (structured outputs), validates it, retries an
  invalid answer once and then returns it flagged (`invalid`), never guessed,
* records every call in the cost ledger in its own transaction: money spent stays
  spent even when the caller's transaction rolls back,
* runs bulk work through the Message Batches API at half price (`submit_batch`;
  `poll_batches` hands the results to the stage's registered handler).

A refusal is a result too (`refused`); with models that support it, the API's
server-side fallback answers on another model first.
"""

import hashlib
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pos_coordinator import budget, runtime_settings
from pos_coordinator.budget import Priority
from pos_coordinator.llm import pricing
from pos_coordinator.models import LlmBatch, LlmCall

log = logging.getLogger(__name__)

# Models with the API's server-side refusal fallback (not in batches).
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_BATCH_REQUESTS = 5_000
CHARS_PER_TOKEN = 3.0  # conservative, for estimates before a call


class LlmUnavailable(RuntimeError):
    """No API key configured: LLM stages stay idle."""


class BudgetDeferred(RuntimeError):
    """The budget does not allow this priority now; the work waits."""


@dataclass
class Task:
    stage: str  # e.g. "classify", "category_mapping", "price_review"
    system: str  # stable instructions: cached, keep them identical across tasks
    prompt: str  # the varying input
    schema: type[BaseModel]
    model_role: str = "default"  # key of the llm_models runtime setting
    priority: Priority = Priority.normal
    source: str | None = None
    job_id: uuid.UUID | None = None
    max_tokens: int = 4096
    effort: str | None = "low"
    meta: dict[str, Any] = field(default_factory=dict)  # carried to batch handlers


@dataclass
class Outcome:
    status: str  # ok | invalid | refused | error
    data: BaseModel | None
    model: str
    stage: str = ""
    cached: bool = False
    cost_usd: Decimal = Decimal(0)
    detail: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


# stage -> (answer schema, handler(session, outcome)) for batch results
BatchHandler = Callable[[AsyncSession, Outcome], Awaitable[None]]
HANDLERS: dict[str, tuple[type[BaseModel], BatchHandler]] = {}


def register(stage: str, schema: type[BaseModel]):
    def deco(fn: BatchHandler) -> BatchHandler:
        HANDLERS[stage] = (schema, fn)
        return fn

    return deco


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The pydantic model as a JSON schema for structured outputs: references inlined,
    every object closed (additionalProperties false) with all properties required."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def fix(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return fix(defs[node["$ref"].rsplit("/", 1)[-1]])
            out = {k: fix(v) for k, v in node.items() if k not in ("title", "default")}
            if out.get("type") == "object" and "properties" in out:
                out["additionalProperties"] = False
                out["required"] = list(out["properties"])
            return out
        if isinstance(node, list):
            return [fix(x) for x in node]
        return node

    return fix(raw)


def input_hash(model: str, task: Task) -> str:
    payload = json.dumps(
        {
            "model": model,
            "system": task.system,
            "prompt": task.prompt,
            "schema": strict_schema(task.schema),
            "effort": task.effort,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _text(message: Any) -> str | None:
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return block.text
    return None


def _usage(message: Any) -> dict[str, int]:
    u = getattr(message, "usage", None)
    return {
        "input_tokens": int(getattr(u, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(u, "output_tokens", 0) or 0),
        "cache_read_tokens": int(getattr(u, "cache_read_input_tokens", 0) or 0),
        "cache_write_tokens": int(getattr(u, "cache_creation_input_tokens", 0) or 0),
    }


def _cost(requested: str, served: str | None, usage: dict[str, int], batch: bool) -> Decimal:
    """Priced at the dearer of the requested and the serving model (a fallback)."""
    return max(pricing.cost_usd(m, **usage, batch=batch) for m in {requested, served or requested})


def _params(model: str, task: Task) -> dict[str, Any]:
    output_config: dict[str, Any] = {
        "format": {"type": "json_schema", "schema": strict_schema(task.schema)}
    }
    if task.effort:
        output_config["effort"] = task.effort
    return {
        "model": model,
        "max_tokens": task.max_tokens,
        # the stable part first and cached; the varying input after it
        "system": [{"type": "text", "text": task.system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": task.prompt}],
        "output_config": output_config,
    }


def estimate_cost(model: str, task: Task, batch: bool) -> Decimal:
    tokens_in = int((len(task.system) + len(task.prompt)) / CHARS_PER_TOKEN) + 50
    return pricing.cost_usd(model, tokens_in, task.max_tokens, batch=batch)


def default_client() -> Any:
    from pos_coordinator.config import get_settings

    key = get_settings().anthropic_api_key
    secret = key.get_secret_value() if key is not None else os.environ.get("ANTHROPIC_API_KEY")
    if not secret:
        raise LlmUnavailable("no Anthropic API key (POS_ANTHROPIC_API_KEY)")
    import anthropic

    return anthropic.AsyncAnthropic(api_key=secret, max_retries=3)


class LlmService:
    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        client_factory: Callable[[], Any] = default_client,
    ) -> None:
        self.sm = sm
        self._factory = client_factory
        self._client: Any = None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._factory()
        return self._client

    def available(self) -> bool:
        try:
            _ = self.client
            return True
        except LlmUnavailable:
            return False

    async def model_for(self, role: str) -> str:
        async with self.sm() as s:
            models = await runtime_settings.get(s, "llm_models")
        return models.get(role) or models["default"]

    # --- ledger --------------------------------------------------------------------

    async def _record(
        self,
        *,
        model: str,
        task: Task,
        usage: dict[str, int],
        cost: Decimal,
        h: str,
        output: dict[str, Any],
        batch: bool = False,
        session: AsyncSession | None = None,
    ) -> None:
        row = LlmCall(
            model=model,
            stage=task.stage,
            source=task.source,
            job_id=task.job_id,
            batch=batch,
            cost_usd=cost,
            input_hash=h,
            output=output,
            **usage,
        )
        if session is not None:
            session.add(row)
            return
        async with self.sm() as s:
            s.add(row)
            await s.commit()

    async def _cached(self, h: str, model: str) -> dict[str, Any] | None:
        async with self.sm() as s:
            out = (
                await s.execute(
                    select(LlmCall.output)
                    .where(
                        LlmCall.input_hash == h,
                        LlmCall.model == model,
                        LlmCall.output["status"].astext == "ok",
                    )
                    .order_by(LlmCall.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        return out

    # --- one call --------------------------------------------------------------------

    async def run(self, task: Task) -> Outcome:
        model = await self.model_for(task.model_role)
        h = input_hash(model, task)
        hit = await self._cached(h, model)
        if hit is not None:
            try:
                return Outcome(
                    "ok", task.schema.model_validate(hit["data"]), model, task.stage, cached=True
                )
            except ValidationError:
                pass  # the schema changed since: ask again
        async with self.sm() as s:
            status = await budget.status(s)
        if not status.allows(task.priority):
            raise BudgetDeferred(f"budget {status.state}: {task.priority} work deferred")
        if status.remaining_usd < estimate_cost(model, task, batch=False):
            raise BudgetDeferred("the remaining budget does not cover this call")
        client = self.client
        total = Decimal(0)
        detail = None
        for attempt in (1, 2):
            params = _params(model, task)
            if model in FALLBACK_MODELS:
                msg = await client.beta.messages.create(
                    **params, betas=[FALLBACK_BETA], fallbacks="default"
                )
            else:
                msg = await client.messages.create(**params)
            served = getattr(msg, "model", None) or model
            usage = _usage(msg)
            cost = _cost(model, served, usage, batch=False)
            total += cost
            text = _text(msg)
            stop = getattr(msg, "stop_reason", None)
            if stop == "refusal":
                details = getattr(msg, "stop_details", None)
                detail = f"refusal: {getattr(details, 'category', None)}"
                await self._record(
                    model=served, task=task, usage=usage, cost=cost, h=h,
                    output={"status": "refused", "detail": detail},
                )  # fmt: skip
                return Outcome("refused", None, served, task.stage, cost_usd=total, detail=detail)
            try:
                if stop == "max_tokens" or text is None:
                    raise ValueError(f"no complete answer (stop_reason {stop})")
                data = task.schema.model_validate_json(text)
            except (ValidationError, ValueError) as e:
                detail = f"attempt {attempt}: {str(e)[:500]}"
                await self._record(
                    model=served, task=task, usage=usage, cost=cost, h=h,
                    output={"status": "invalid", "detail": detail, "text": (text or "")[:2000]},
                )  # fmt: skip
                continue
            await self._record(
                model=served, task=task, usage=usage, cost=cost, h=h,
                output={"status": "ok", "data": data.model_dump(mode="json")},
            )  # fmt: skip
            return Outcome("ok", data, served, task.stage, cost_usd=total)
        return Outcome("invalid", None, model, task.stage, cost_usd=total, detail=detail)

    # --- batches ---------------------------------------------------------------------

    async def submit_batch(self, tasks: list[Task]) -> tuple[uuid.UUID | None, int]:
        """Submit tasks of one stage as a Message Batch (half price; results within
        24 h). Tasks answered before are handed to the handler at once. Returns the
        batch id and how many tasks went in: as many as the budget allows now (the
        rest stays with the caller for later)."""
        if not tasks:
            return None, 0
        stage = tasks[0].stage
        if any(t.stage != stage for t in tasks) or stage not in HANDLERS:
            raise ValueError(f"one registered stage per batch (got {stage!r})")
        model = await self.model_for(tasks[0].model_role)
        async with self.sm() as s:
            status = await budget.status(s)
            # batches still running are not in the ledger yet: count their estimate
            pending = (
                await s.execute(
                    select(func.coalesce(func.sum(LlmBatch.estimated_cost_usd), 0)).where(
                        LlmBatch.status == "submitted"
                    )
                )
            ).scalar_one()
        if not status.allows(tasks[0].priority):
            raise BudgetDeferred(f"budget {status.state}: batch deferred")
        # spend at most today's allowance (the remaining budget for high priority)
        room = (
            status.remaining_usd
            if tasks[0].priority == Priority.high
            else min(status.remaining_usd, status.daily_allowance_usd - status.spent_today_usd)
        ) - Decimal(pending)
        chosen: list[tuple[str, Task]] = []
        spent = Decimal(0)
        handled = 0
        for task in tasks[:MAX_BATCH_REQUESTS]:
            h = input_hash(model, task)
            hit = await self._cached(h, model)
            if hit is not None:
                try:
                    data = task.schema.model_validate(hit["data"])
                except ValidationError:
                    data = None  # the schema changed since: ask again
                if data is not None:
                    await self._deliver(
                        Outcome("ok", data, model, stage, cached=True, meta=task.meta)
                    )
                    handled += 1
                    continue
            est = estimate_cost(model, task, batch=True)
            if spent + est > room:
                break
            spent += est
            chosen.append((h, task))
        if not chosen:
            return None, handled
        requests = [
            {"custom_id": str(i), "params": _params(model, task)}
            for i, (_, task) in enumerate(chosen)
        ]
        remote = await self.client.messages.batches.create(requests=requests)
        row = LlmBatch(
            remote_id=remote.id,
            stage=stage,
            model=model,
            status="submitted",
            request_count=len(chosen),
            estimated_cost_usd=spent,
            requests={
                str(i): {"hash": h, "meta": task.meta, "source": task.source}
                for i, (h, task) in enumerate(chosen)
            },
        )
        async with self.sm() as s:
            s.add(row)
            await s.commit()
        return row.id, handled + len(chosen)

    async def _deliver(self, outcome: Outcome) -> None:
        _, handler = HANDLERS[outcome.stage]
        async with self.sm() as s:
            await handler(s, outcome)
            await s.commit()

    async def poll_batches(self) -> int:
        """Collect ended batches: ledger rows (batch price) and the stage handler per
        result, one transaction per batch. Returns the number of results handled."""
        async with self.sm() as s:
            pending = (
                (await s.execute(select(LlmBatch).where(LlmBatch.status == "submitted")))
                .scalars()
                .all()
            )
        handled = 0
        for b in pending:
            remote = await self.client.messages.batches.retrieve(b.remote_id)
            if getattr(remote, "processing_status", None) != "ended":
                continue
            schema, handler = HANDLERS.get(b.stage, (None, None))
            async with self.sm() as s:
                row = await s.get(LlmBatch, b.id, with_for_update=True)
                if row is None or row.status != "submitted":
                    continue  # another coordinator took it
                results = self.client.messages.batches.results(b.remote_id)
                if hasattr(results, "__await__"):
                    results = await results
                async for r in _aiter(results):
                    req = (row.requests or {}).get(r.custom_id)
                    if req is None:
                        continue
                    outcome = await self._batch_outcome(s, row, req, r, schema)
                    if handler is not None:
                        await handler(s, outcome)
                    handled += 1
                row.status = "ended"
                row.ended_at = datetime.now(UTC)
                await s.commit()
        return handled

    async def _batch_outcome(self, s, row: LlmBatch, req: dict, r: Any, schema) -> Outcome:
        meta = req.get("meta") or {}
        task = Task(stage=row.stage, system="", prompt="", schema=schema or BaseModel,
                    source=req.get("source"))  # fmt: skip
        kind = getattr(r.result, "type", None)
        stage = row.stage
        if kind != "succeeded":
            return Outcome(
                "error", None, row.model, stage, detail=f"batch result {kind}", meta=meta
            )
        msg = r.result.message
        usage = _usage(msg)
        served = getattr(msg, "model", None) or row.model
        cost = _cost(row.model, served, usage, batch=True)
        text = _text(msg)
        if getattr(msg, "stop_reason", None) == "refusal":
            await self._record(model=served, task=task, usage=usage, cost=cost, h=req["hash"],
                               output={"status": "refused"}, batch=True, session=s)  # fmt: skip
            return Outcome("refused", None, served, stage, cost_usd=cost, meta=meta)
        try:
            if schema is None or text is None:
                raise ValueError("no answer")
            data = schema.model_validate_json(text)
        except (ValidationError, ValueError) as e:
            await self._record(model=served, task=task, usage=usage, cost=cost, h=req["hash"],
                               output={"status": "invalid", "detail": str(e)[:500]},
                               batch=True, session=s)  # fmt: skip
            return Outcome(
                "invalid", None, served, stage, cost_usd=cost, detail=str(e)[:500], meta=meta
            )
        await self._record(model=served, task=task, usage=usage, cost=cost, h=req["hash"],
                           output={"status": "ok", "data": data.model_dump(mode="json")},
                           batch=True, session=s)  # fmt: skip
        return Outcome("ok", data, served, stage, cost_usd=cost, meta=meta)


async def _aiter(results: Any):
    if hasattr(results, "__aiter__"):
        async for r in results:
            yield r
    else:
        for r in results:
            yield r
