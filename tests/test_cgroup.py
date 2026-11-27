import os

from pos_worker import cgroup

GiB = cgroup.GiB


def _v2(root, cpu_max="max 100000", mem_max="max", mem_cur="104857600"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "cpu.max").write_text(cpu_max + "\n")
    (root / "memory.max").write_text(mem_max + "\n")
    (root / "memory.current").write_text(mem_cur + "\n")
    return root


def test_reads_cgroup_v2_limits(tmp_path):
    root = _v2(tmp_path / "cg", cpu_max="200000 100000", mem_max=str(4 * GiB))
    lim = cgroup.read_limits(root)
    assert lim.cpu == min(2.0, float(os.cpu_count()))  # never more than the host has
    assert lim.mem_limit is not None and lim.mem_limit <= 4 * GiB
    assert lim.mem_used == 104857600


def test_unlimited_v2_falls_back_to_host(tmp_path):
    lim = cgroup.read_limits(_v2(tmp_path / "cg"))
    assert lim.mem_limit is None
    assert lim.effective_mem == lim.host_mem_total


def test_reads_cgroup_v1(tmp_path):
    root = tmp_path / "cg"
    (root / "cpu").mkdir(parents=True)
    (root / "memory").mkdir()
    (root / "cpu" / "cpu.cfs_quota_us").write_text("50000")
    (root / "cpu" / "cpu.cfs_period_us").write_text("100000")
    (root / "memory" / "memory.limit_in_bytes").write_text(str(2 * GiB))
    (root / "memory" / "memory.usage_in_bytes").write_text("1000")
    lim = cgroup.read_limits(root)
    assert lim.cpu == 0.5
    assert lim.mem_used == 1000


def _limits(cpu, mem_gib):
    return cgroup.Limits(cpu=cpu, mem_limit=int(mem_gib * GiB), mem_used=0, host_mem_total=None)


def test_browser_slots_follow_ram_and_cpu():
    assert cgroup.compute_slots(_limits(4, 8), browser_capable=True).browser == 4  # cpu-bound
    assert cgroup.compute_slots(_limits(4, 3), browser_capable=True).browser == 2  # ram-bound
    # RAM reduced live (Proxmox): browser slots are shed first
    assert cgroup.compute_slots(_limits(4, 1.5), browser_capable=True).browser == 0
    assert cgroup.compute_slots(_limits(4, 1.5), browser_capable=True).http == 32
    assert cgroup.compute_slots(_limits(4, 8), browser_capable=False).browser == 0


def test_local_limits_win_over_remote():
    s = cgroup.compute_slots(_limits(4, 8), local_max_http=3, remote_max_http=10)
    assert s.http == 3
    s = cgroup.compute_slots(_limits(4, 8), local_max_http=10, remote_max_http=5)
    assert s.http == 5  # the stricter cap applies; the coordinator can never raise a local cap
