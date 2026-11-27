"""The agent's persisted state (`<data_dir>/state.json`)."""

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pos_shared import exchange

SLOTS = ("a", "b")


def other(slot: str) -> str:
    return "b" if slot == "a" else "a"


@dataclass
class Slot:
    version: str | None = None
    image: str | None = None


@dataclass
class AgentState:
    active: str | None = None
    slots: dict[str, Slot] = field(default_factory=lambda: {s: Slot() for s in SLOTS})
    # version -> {"at": unix time, "error": str}; not retried before failed_retry_after_s
    failed: dict[str, dict] = field(default_factory=dict)
    extra_images: dict[str, str] = field(default_factory=dict)  # e.g. {"caddy": ref}
    update_state: str = "idle"
    update_target: str | None = None
    update_error: str | None = None

    @property
    def active_slot(self) -> Slot | None:
        return self.slots[self.active] if self.active else None

    def may_try(self, version: str, retry_after_s: float, now: float | None = None) -> bool:
        f = self.failed.get(version)
        return f is None or (now or time.time()) - f["at"] >= retry_after_s

    def record_failure(self, version: str, error: str, now: float | None = None) -> None:
        self.failed[version] = {"at": now or time.time(), "error": error[:2000]}
        self.update_state, self.update_target, self.update_error = "failed", version, error[:2000]

    @classmethod
    def load(cls, path: Path) -> "AgentState":
        raw = exchange.read_json(path)
        if not isinstance(raw, dict):
            return cls()
        slots = {s: Slot(**raw.get("slots", {}).get(s, {})) for s in SLOTS}
        return cls(
            active=raw.get("active"),
            slots=slots,
            failed=raw.get("failed", {}),
            extra_images=raw.get("extra_images", {}),
            update_state=raw.get("update_state", "idle"),
            update_target=raw.get("update_target"),
            update_error=raw.get("update_error"),
        )

    def save(self, path: Path) -> None:
        exchange.write_json(path, asdict(self))
