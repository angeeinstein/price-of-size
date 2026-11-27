"""Read the node's own cgroup limits and derive concurrency from them.

Re-read every ~30 s, so RAM changed live in Proxmox takes effect: browser
slots are shed first, and running jobs finish instead of being OOM-killed.
"""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

GiB = 1024**3
BROWSER_RAM_PER_SLOT = int(1.25 * GiB)
BASE_RAM_RESERVE = int(0.5 * GiB)  # agent, Python, OS slack
HTTP_SLOTS_PER_CORE = 8
MAX_HTTP_SLOTS = 64


@dataclass(frozen=True)
class Limits:
    cpu: float  # usable cores
    mem_limit: int | None  # bytes; None = unlimited (fall back to host RAM)
    mem_used: int | None
    host_mem_total: int | None
    load: float | None = None
    disk_free: int | None = None

    @property
    def effective_mem(self) -> int | None:
        if self.mem_limit is not None:
            return self.mem_limit
        return self.host_mem_total

    @property
    def mem_free(self) -> int | None:
        total = self.effective_mem
        if total is None:
            return None
        return max(total - (self.mem_used or 0), 0)


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _host_mem_total() -> int | None:
    text = _read(Path("/proc/meminfo"))
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    return None


def read_limits(root: Path = Path("/sys/fs/cgroup"), state_dir: Path | None = None) -> Limits:
    host_cpus = float(os.cpu_count() or 1)
    cpu = host_cpus
    mem_limit = mem_used = None

    cpu_max = _read(root / "cpu.max")  # cgroup v2: "<quota> <period>" or "max <period>"
    if cpu_max:
        quota, _, period = cpu_max.partition(" ")
        if quota != "max" and period:
            cpu = min(host_cpus, int(quota) / int(period))
    else:  # cgroup v1
        q = _read(root / "cpu" / "cpu.cfs_quota_us") or _read(root / "cpu.cfs_quota_us")
        p = _read(root / "cpu" / "cpu.cfs_period_us") or _read(root / "cpu.cfs_period_us")
        if q and p and int(q) > 0:
            cpu = min(host_cpus, int(q) / int(p))

    if (m := _read(root / "memory.max")) is not None:
        mem_limit = None if m == "max" else int(m)
        if (u := _read(root / "memory.current")) is not None:
            mem_used = int(u)
    else:
        m1 = _read(root / "memory" / "memory.limit_in_bytes")
        if m1 is not None and int(m1) < 1 << 60:  # v1 reports a huge number for "unlimited"
            mem_limit = int(m1)
        if (u1 := _read(root / "memory" / "memory.usage_in_bytes")) is not None:
            mem_used = int(u1)

    host_total = _host_mem_total()
    if mem_limit is not None and host_total is not None:
        mem_limit = min(mem_limit, host_total)

    try:
        load = os.getloadavg()[0]
    except OSError:
        load = None
    disk_free = shutil.disk_usage(state_dir).free if state_dir and state_dir.exists() else None
    return Limits(
        cpu=max(cpu, 0.1),
        mem_limit=mem_limit,
        mem_used=mem_used,
        host_mem_total=host_total,
        load=load,
        disk_free=disk_free,
    )


@dataclass(frozen=True)
class Slots:
    http: int
    browser: int
    embed_batch: int


def compute_slots(
    limits: Limits,
    *,
    local_max_http: int | None = None,
    local_max_browser: int | None = None,
    remote_max_http: int | None = None,
    remote_max_browser: int | None = None,
    browser_capable: bool = False,
) -> Slots:
    http = max(1, min(MAX_HTTP_SLOTS, int(limits.cpu * HTTP_SLOTS_PER_CORE)))

    browser = 0
    if browser_capable:
        total = limits.effective_mem
        if total is not None:
            by_ram = (total - BASE_RAM_RESERVE) // BROWSER_RAM_PER_SLOT
            browser = int(max(0, min(by_ram, int(limits.cpu))))

    total = limits.effective_mem or 2 * GiB
    embed_batch = int(max(8, min(256, (total // GiB) * 16)))

    for cap in (remote_max_http, local_max_http):
        if cap is not None:
            http = min(http, cap)
    for cap in (remote_max_browser, local_max_browser):
        if cap is not None:
            browser = min(browser, cap)
    return Slots(http=max(http, 0), browser=max(browser, 0), embed_batch=embed_batch)
