"""Calls to the Claude API: budgeted, cached by input, strictly structured.

Every stage that needs the LLM describes its work as a `Task`: stable instructions
(cached by the API), the varying input, and a pydantic schema the answer must
satisfy. The service

* answers from the ledger when the same input was answered before (no cost),
* checks the monthly budget for the task's priority and raises `BudgetDeferred`
  when the work has to wait (the caller leaves it queued); admission and the
  reservation of the call's estimate happen in one step, serialised across
  coordinators, so concurrent calls never spend the same remaining money twice,
* asks for JSON in the schema (structured outputs), validates it, retries an
  invalid answer once and then returns it flagged (`invalid`), never guessed,
* records every call in the cost ledger in its own transaction: money spent stays
  spent even when the caller's transaction rolls back,
* runs bulk work through the Message Batches API at half price (`submit_batch`;
  `poll_batches` hands the results to the stage's registered handler).

A refusal is a result too (`refused`); with models that support it, the API's
server-side fallback answers on another model first.
"""

import asyncio
import hashlib
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select, update
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
# what the API adds to a request with structured output (its own instructions),
# generously: estimates must not fall short
REQUEST_OVERHEAD_TOKENS = 500
# Advisory lock that serialises budget admission (any coordinator): b"pos_llm".
BUDGET_LOCK = 0x706F735F6C6C6D
# Batch rows that still hold a reservation: being submitted, or running remotely.
RESERVING = ("submitting", "submitted")
# A submission that never got its remote id by then was interrupted (the SDK gives
# up on a request long before).
STALE_SUBMISSION = timedelta(hours=2)


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
            out = {}
            for k, v in node.items():
                if k in ("title", "default"):
                    continue  # schema metadata
                if k == "properties" and isinstance(v, dict):
                    out[k] = {name: fix(sub) for name, sub in v.items()}  # field names stay
                else:
                    out[k] = fix(v)
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
    """An upper estimate before the call: everything sent (instructions, input, the
    answer schema, the API's own instructions for structured output) and the full
    `max_tokens` answered."""
    chars = len(task.system) + len(task.prompt) + len(json.dumps(strict_schema(task.schema)))
    tokens_in = int(chars / CHARS_PER_TOKEN) + REQUEST_OVERHEAD_TOKENS
    return pricing.cost_usd(model, tokens_in, task.max_tokens, batch=batch)


def default_client(settings: Any = None) -> Any:
    """A client with the key from `settings` (the application's), else the
    process settings, else ANTHROPIC_API_KEY."""
    if settings is None:
        from pos_coordinator.config import get_settings

        settings = get_settings()
    key = settings.anthropic_api_key
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
        session: AsyncSession,
        *,
        model: str,
        task: Task,
        usage: dict[str, int],
        cost: Decimal,
        h: str,
        output: dict[str, Any],
        batch: bool = False,
    ) -> None:
        session.add(
            LlmCall(
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
        )

    async def _cached(self, h: str) -> dict[str, Any] | None:
        """The last valid answer to the same input. The hash includes the requested
        model; the row's model is the one that answered (a fallback model may)."""
        async with self.sm() as s:
            out = (
                await s.execute(
                    select(LlmCall.output)
                    .where(LlmCall.input_hash == h, LlmCall.output["status"].astext == "ok")
                    .order_by(LlmCall.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        return out

    @staticmethod
    async def _locked_budget(s: AsyncSession) -> tuple[budget.BudgetStatus, Decimal]:
        """Take the budget lock (until `s` commits) and return the budget status and
        what batches hold reserved (they are not in the ledger until their results
        come back). Synchronous calls in flight are in the ledger already, at their
        estimate."""
        await s.execute(select(func.pg_advisory_xact_lock(BUDGET_LOCK)))
        status = await budget.status(s)
        reserved = (
            await s.execute(
                select(func.coalesce(func.sum(LlmBatch.estimated_cost_usd), 0)).where(
                    LlmBatch.status.in_(RESERVING)
                )
            )
        ).scalar_one()
        return status, Decimal(reserved)

    async def _reserve(self, task: Task, model: str, h: str) -> uuid.UUID:
        """Admit one paid attempt and reserve its estimate in the same transaction: a
        pending ledger row that every later admission counts. `_settle` replaces the
        estimate with the actual cost."""
        async with self.sm() as s:
            status, reserved = await self._locked_budget(s)
            if not status.allows(task.priority):
                raise BudgetDeferred(f"budget {status.state}: {task.priority} work deferred")
            est = estimate_cost(model, task, batch=False)
            if status.remaining_usd - reserved < est:
                raise BudgetDeferred("the remaining budget does not cover this call")
            row = LlmCall(
                model=model,
                stage=task.stage,
                source=task.source,
                job_id=task.job_id,
                cost_usd=est,
                input_hash=h,
                output={"status": "pending"},
            )
            s.add(row)
            await s.commit()
            return row.id

    async def _settle(self, row_id: uuid.UUID, **values: Any) -> None:
        async with self.sm() as s:
            await s.execute(update(LlmCall).where(LlmCall.id == row_id).values(**values))
            await s.commit()

    async def _settle_failure(self, row_id: uuid.UUID, e: BaseException) -> None:
        """The API answered with an error: nothing was generated, nothing to pay.
        Anything else (timeout, lost connection, cancellation) may have been
        processed: the estimate stays in the ledger."""
        if getattr(e, "status_code", None) is not None:
            await self._settle(row_id, cost_usd=Decimal(0),
                               output={"status": "error", "detail": str(e)[:500]})  # fmt: skip
        else:
            detail = f"outcome unknown ({type(e).__name__}): cost estimated"
            await self._settle(row_id, output={"status": "error", "detail": detail})

    # --- one call --------------------------------------------------------------------

    async def run(self, task: Task) -> Outcome:
        model = await self.model_for(task.model_role)
        h = input_hash(model, task)
        hit = await self._cached(h)
        if hit is not None:
            try:
                return Outcome(
                    "ok", task.schema.model_validate(hit["data"]), model, task.stage, cached=True
                )
            except ValidationError:
                pass  # the schema changed since: ask again
        client = self.client
        total = Decimal(0)
        detail = None
        for attempt in (1, 2):
            # admitted before every paid attempt: the first one may have used the rest
            rid = await self._reserve(task, model, h)
            params = _params(model, task)
            try:
                if model in FALLBACK_MODELS:
                    msg = await client.beta.messages.create(
                        **params, betas=[FALLBACK_BETA], fallbacks="default"
                    )
                else:
                    msg = await client.messages.create(**params)
            except BaseException as e:
                await asyncio.shield(self._settle_failure(rid, e))
                raise
            served = getattr(msg, "model", None) or model
            usage = _usage(msg)
            cost = _cost(model, served, usage, batch=False)
            total += cost
            text = _text(msg)
            stop = getattr(msg, "stop_reason", None)

            async def settle(output: dict[str, Any], rid=rid, served=served, usage=usage,
                             cost=cost) -> None:  # fmt: skip
                await self._settle(rid, model=served, cost_usd=cost, output=output, **usage)

            if stop == "refusal":
                details = getattr(msg, "stop_details", None)
                detail = f"refusal: {getattr(details, 'category', None)}"
                await settle({"status": "refused", "detail": detail})
                return Outcome("refused", None, served, task.stage, cost_usd=total, detail=detail)
            try:
                if stop == "max_tokens" or text is None:
                    raise ValueError(f"no complete answer (stop_reason {stop})")
                data = task.schema.model_validate_json(text)
            except (ValidationError, ValueError) as e:
                detail = f"attempt {attempt}: {str(e)[:500]}"
                await settle({"status": "invalid", "detail": detail, "text": (text or "")[:2000]})
                continue
            await settle({"status": "ok", "data": data.model_dump(mode="json")})
            return Outcome("ok", data, served, task.stage, cost_usd=total)
        return Outcome("invalid", None, model, task.stage, cost_usd=total, detail=detail)

    # --- batches ---------------------------------------------------------------------

    async def submit_batch(self, tasks: list[Task]) -> tuple[uuid.UUID | None, list[Task]]:
        """Submit tasks of one stage as a Message Batch (half price; results within
        24 h). Tasks answered before are handed to the handler at once, whatever the
        budget. Returns the batch id (None if nothing was submitted) and the tasks left
        over: those the budget does not allow now, or beyond one batch's size. The
        caller keeps them for later."""
        if not tasks:
            return None, []
        stage = tasks[0].stage
        if any(t.stage != stage for t in tasks) or stage not in HANDLERS:
            raise ValueError(f"one registered stage per batch (got {stage!r})")
        model = await self.model_for(tasks[0].model_role)
        misses: list[tuple[str, Task]] = []
        for task in tasks:
            h = input_hash(model, task)
            hit = await self._cached(h)
            if hit is not None:
                try:
                    data = task.schema.model_validate(hit["data"])
                except ValidationError:
                    data = None  # the schema changed since: ask again
                if data is not None:
                    await self._deliver(
                        Outcome("ok", data, model, stage, cached=True, meta=task.meta)
                    )
                    continue
            misses.append((h, task))
        if not misses:
            return None, []
        client = self.client
        priority = tasks[0].priority
        chosen: list[tuple[str, Task]] = []
        async with self.sm() as s:
            status, reserved = await self._locked_budget(s)
            if not status.allows(priority):
                return None, [t for _, t in misses]
            # spend at most today's allowance (the remaining budget for high priority)
            room = (
                status.remaining_usd
                if priority == Priority.high
                else min(status.remaining_usd, status.daily_allowance_usd - status.spent_today_usd)
            ) - reserved
            spent = Decimal(0)
            for h, task in misses[:MAX_BATCH_REQUESTS]:
                est = estimate_cost(model, task, batch=True)
                if spent + est > room:
                    break
                spent += est
                chosen.append((h, task))
            if not chosen:
                return None, [t for _, t in misses]
            # the reservation is committed (with the lock released) before the request
            row = LlmBatch(
                stage=stage,
                model=model,
                status="submitting",
                request_count=len(chosen),
                estimated_cost_usd=spent,
                requests={
                    str(i): {"hash": h, "meta": task.meta, "source": task.source}
                    for i, (h, task) in enumerate(chosen)
                },
            )
            s.add(row)
            await s.commit()
            batch_id = row.id
        left = [t for _, t in misses[len(chosen) :]]
        requests = [
            {"custom_id": str(i), "params": _params(model, task)}
            for i, (_, task) in enumerate(chosen)
        ]
        try:
            remote = await client.messages.batches.create(requests=requests)
        except BaseException as e:
            if getattr(e, "status_code", None) is not None:
                # rejected by the API: no batch exists, the reservation is released
                await asyncio.shield(self._set_batch(batch_id, status="failed",
                                                     ended_at=datetime.now(UTC)))  # fmt: skip
            # otherwise it may exist: it stays reserved until it counts as interrupted
            raise
        await self._set_batch(batch_id, remote_id=remote.id, status="submitted")
        return batch_id, left

    async def _set_batch(self, batch_id: uuid.UUID, **values: Any) -> None:
        async with self.sm() as s:
            await s.execute(update(LlmBatch).where(LlmBatch.id == batch_id).values(**values))
            await s.commit()

    async def _expire_submissions(self) -> None:
        """A submission interrupted between reserving and learning its remote id may
        or may not have created a batch whose results can never be collected. Its
        estimate moves into the ledger (money possibly spent stays counted)."""
        cutoff = datetime.now(UTC) - STALE_SUBMISSION
        async with self.sm() as s:
            stale = (
                (
                    await s.execute(
                        select(LlmBatch)
                        .where(LlmBatch.status == "submitting", LlmBatch.created_at < cutoff)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for b in stale:
                log.warning("LLM batch %s: submission interrupted; its estimate is kept", b.id)
                b.status = "failed"
                b.ended_at = datetime.now(UTC)
                s.add(
                    LlmCall(
                        model=b.model,
                        stage=b.stage,
                        batch=True,
                        cost_usd=b.estimated_cost_usd,
                        output={"status": "error", "detail": "batch submission interrupted: "
                                "cost estimated"},
                    )
                )  # fmt: skip
            await s.commit()

    async def _deliver(self, outcome: Outcome) -> None:
        _, handler = HANDLERS[outcome.stage]
        async with self.sm() as s:
            await handler(s, outcome)
            await s.commit()

    async def poll_batches(self) -> int:
        """Collect ended batches: ledger rows (batch price) and the stage handler per
        result, one transaction per batch. Returns the number of results handled."""
        await self._expire_submissions()
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
            await self._record(s, model=served, task=task, usage=usage, cost=cost, h=req["hash"],
                               output={"status": "refused"}, batch=True)  # fmt: skip
            return Outcome("refused", None, served, stage, cost_usd=cost, meta=meta)
        try:
            if schema is None or text is None:
                raise ValueError("no answer")
            data = schema.model_validate_json(text)
        except (ValidationError, ValueError) as e:
            await self._record(s, model=served, task=task, usage=usage, cost=cost, h=req["hash"],
                               output={"status": "invalid", "detail": str(e)[:500]},
                               batch=True)  # fmt: skip
            return Outcome(
                "invalid", None, served, stage, cost_usd=cost, detail=str(e)[:500], meta=meta
            )
        await self._record(s, model=served, task=task, usage=usage, cost=cost, h=req["hash"],
                           output={"status": "ok", "data": data.model_dump(mode="json")},
                           batch=True)  # fmt: skip
        return Outcome("ok", data, served, stage, cost_usd=cost, meta=meta)


async def _aiter(results: Any):
    if hasattr(results, "__aiter__"):
        async for r in results:
            yield r
    else:
        for r in results:
            yield r
