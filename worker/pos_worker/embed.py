"""The `embed` job: text embeddings computed on the worker (no paid API).

The model files (see pos_shared.embedding) are downloaded once into the worker's
state directory, checked against their SHA-256, and loaded lazily on the first
job; a node that never gets embed jobs never loads them. One job at a time per
worker: inference already uses every core.
"""

import asyncio
import hashlib
import logging
import os
import threading
from pathlib import Path

import httpx
import numpy as np

from pos_shared import api, embedding
from pos_worker.handlers import JobContext, done, handler

log = logging.getLogger(__name__)
HF_URL = "https://huggingface.co/{repo}/resolve/{rev}/{path}"
MODEL_DIR: Path = Path(os.environ.get("POS_WORKER_STATE_DIR", "/var/lib/pos-worker")) / "models"
_LOCK = threading.Lock()
_SEM: asyncio.Semaphore | None = None
_MODEL = None


def set_model_dir(path: Path) -> None:
    global MODEL_DIR
    MODEL_DIR = path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_files(base: Path | None = None) -> Path:
    """Download (once) and verify the pinned model files; returns their directory."""
    root = (base or MODEL_DIR) / f"{embedding.REPO.replace('/', '__')}@{embedding.REVISION}"
    for rel, digest in embedding.FILES.items():
        path = root / rel
        if path.exists() and _sha256(path) == digest:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        url = HF_URL.format(repo=embedding.REPO, rev=embedding.REVISION, path=rel)
        log.info("downloading %s", url)
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        if _sha256(tmp) != digest:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"{rel}: checksum mismatch, download rejected")
        tmp.replace(path)
    return root


class Model:
    def __init__(self, root: Path) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.tok = Tokenizer.from_file(str(root / "onnx/tokenizer.json"))
        self.tok.enable_truncation(max_length=embedding.MAX_TOKENS)
        self.tok.enable_padding(pad_id=1, pad_token="<pad>")
        opts = ort.SessionOptions()
        self.session = ort.InferenceSession(
            str(root / "onnx/model.onnx"), opts, providers=["CPUExecutionProvider"]
        )
        self.inputs = {i.name for i in self.session.get_inputs()}

    def embed(self, texts: list[str]) -> np.ndarray:
        """Mean-pooled, L2-normalised (n x DIM)."""
        enc = self.tok.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feeds["token_type_ids"] = np.zeros_like(ids)
        hidden = self.session.run(None, feeds)[0]
        pooled = (hidden * mask[..., None]).sum(1) / np.maximum(mask.sum(1, keepdims=True), 1)
        return pooled / np.linalg.norm(pooled, axis=1, keepdims=True)


def load() -> Model:
    global _MODEL
    with _LOCK:
        if _MODEL is None:
            _MODEL = Model(ensure_files())
        return _MODEL


@handler("embed")
async def embed(job: api.LeasedJob, ctx: JobContext) -> api.JobResult:
    global _SEM
    if job.payload.get("model") != embedding.MODEL_KEY:
        # another release's model: say so, the coordinator re-queues for a matching node
        return api.JobResult(
            job_id=job.id,
            attempt=job.attempt,
            outcome=api.JobOutcome.failed,
            error=f"model mismatch: this worker embeds {embedding.MODEL_KEY}",
            retryable=True,
        )
    items = job.payload.get("items") or []
    if not isinstance(items, list) or not 0 < len(items) <= embedding.MAX_ITEMS_PER_JOB:
        raise ValueError("embed job needs 1..64 items")
    texts = [str((it or {}).get("text") or "")[: embedding.MAX_TEXT_CHARS] for it in items]
    if _SEM is None:
        _SEM = asyncio.Semaphore(1)
    async with _SEM:
        model = await asyncio.to_thread(load)
        vectors = await asyncio.to_thread(model.embed, texts)
    return done(
        job,
        {
            "model": embedding.MODEL_KEY,
            "ids": [str((it or {}).get("id")) for it in items],
            "vectors": embedding.encode(vectors),
        },
    )
