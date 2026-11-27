"""Text embeddings: the model, the texts it sees, the wire format of the vectors.

Workers compute embeddings locally (no paid API): multilingual-e5-small, pinned to
one revision of its ONNX export and checked by hash, so every node computes the same
vectors. The key `MODEL_KEY` names model, revision and text format: changing either
gives a new key, and vectors under the old key are computed again, never mixed.

E5 wants a prefix; "query: " on both sides suits matching products to categories.
Categories are embedded with their English and German paths together: shop texts
are often German, and the pair matches them clearly better than English alone.
"""

import base64
import math
from typing import Any

import numpy as np

REPO = "intfloat/multilingual-e5-small"
REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
FILES = {
    "onnx/model.onnx": "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665",
    "onnx/tokenizer.json": "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
}
DIM = 384
MAX_TOKENS = 256
TEXT_VERSION = "t1"
MODEL_KEY = f"e5-small@{REVISION[:7]}/{TEXT_VERSION}"
MAX_TEXT_CHARS = 2_000
MAX_ITEMS_PER_JOB = 64
PREFIX = "query: "


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def product_text(
    title: str | None,
    category_path: list[str] | None = None,
    dims_m: tuple[float | None, ...] = (),
    mass_kg: float | None = None,
    brand: str | None = None,
) -> str:
    """'VITTSJÖ Regal ... | Produkte > Regale | 175 x 100 x 36 cm | 27.6 kg'."""
    parts = [_clean(" ".join(x for x in (brand, title) if x))]
    if category_path:
        parts.append(" > ".join(_clean(c) for c in category_path if _clean(c)))
    sizes = [d for d in dims_m if d]
    if sizes:
        parts.append(" x ".join(_fmt(d * 100) for d in sizes) + " cm")
    if mass_kg:
        parts.append(f"{_fmt(mass_kg)} kg")
    return (PREFIX + " | ".join(p for p in parts if p))[:MAX_TEXT_CHARS]


def node_text(path_en: str, path_de: str | None = None) -> str:
    text = _clean(path_en)
    if path_de and _clean(path_de) != text:
        text = f"{text} | {_clean(path_de)}"
    return (PREFIX + text)[:MAX_TEXT_CHARS]


def _fmt(x: float) -> str:
    return f"{x:.3g}" if x < 1000 else f"{x:.0f}"


def encode(vectors: np.ndarray) -> str:
    """Unit vectors (n x DIM) -> base64 of little-endian float16: compact on the wire,
    precise enough for cosine similarity."""
    return base64.b64encode(np.asarray(vectors, dtype="<f2").tobytes()).decode()


def decode(data: str, count: int) -> list[list[float]]:
    """Untrusted (it comes from a worker): exact size, finite, roughly unit length."""
    try:
        raw = base64.b64decode(data, validate=True)
    except ValueError as e:
        raise ValueError(f"bad vector encoding: {e}") from e
    if len(raw) != count * DIM * 2:
        raise ValueError(f"expected {count} vectors of {DIM}, got {len(raw)} bytes")
    arr = np.frombuffer(raw, dtype="<f2").astype(np.float32).reshape(count, DIM)
    out = []
    for row in arr:
        norm = float(np.linalg.norm(row))
        if not math.isfinite(norm) or not 0.9 <= norm <= 1.1:
            raise ValueError("vector is not a finite unit vector")
        out.append([float(x) for x in row])
    return out
