"""Hardware probing: memory / CPU / OS / WSL2 / disk type (no fio dependency).

Multi-signal disk classification:
  1. /sys/block/*/queue/rotational (first tier, unreliable under virtual disks/WSL2 — hardware-probe.md §2.1)
  2. O_DIRECT 4K random-read micro-benchmark (second tier, a ~64MB temp file written into
     the bench directory, deleted afterwards)
  3. probe failure → conservative ssd
"""
from __future__ import annotations

import ctypes
import os
import platform
import random
import time
from dataclasses import dataclass, field

# IOPS tier thresholds (QD1-equivalent micro-benchmark, conservative lower bounds; the fio QD32 metric is in hardware-probe.md §2.2)
IOPS_HDD_MAX = 200        # < 200 → hdd
IOPS_NVME_MIN = 20000     # 200–20000 → ssd/cloud; > 20000 → nvme
BENCH_FILE_MB = 64


@dataclass
class HardwareInfo:
    total_ram_gb: float | None
    cpu_cores: int
    os_type: str                     # linux / windows / mac
    is_wsl2: bool
    disk_type: str                   # hdd / ssd / nvme / cloud
    disk_confidence: str             # high / medium / low
    rotational: dict = field(default_factory=dict)   # device name -> 0/1 (unreliable under WSL2, display only)
    bench_iops: float | None = None
    bench_path: str = ""
    bench_error: str | None = None
    disk_notes: list = field(default_factory=list)
    mem_available_gb: float | None = None   # runtime available memory (MemAvailable); total is not availability
    disk_free_gb: float | None = None       # free space on the data directory's filesystem (max_wal_size upper bound)


def classify_disk_by_iops(iops: float) -> str:
    """IOPS → disk-type tier (standalone pure function, easy to test and reuse)."""
    if iops < IOPS_HDD_MAX:
        return "hdd"
    if iops <= IOPS_NVME_MIN:
        return "ssd"   # 200–20000 covers ssd/cloud (cloud-disk baseline IOPS also fall in this tier)
    return "nvme"


def _total_ram_gb() -> float | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])          # kB; convention 1GB=1048576kB
                    return round(kb / 1048576, 2)
    except (OSError, ValueError):
        pass
    return None


def _mem_available_gb(meminfo_text: str | None = None) -> float | None:
    """Runtime available memory (/proc/meminfo MemAvailable). total_ram reflects physical
    capacity; on memory-constrained hosts (e.g. WSL2 with 6GB of resident dev tools)
    MemAvailable can be just 1/4 of it — the basis for the shared_buffers clamp (the cliff
    trigger in the heavy-load validation report §五.2). The text parameter lets tests inject."""
    if meminfo_text is None:
        try:
            with open("/proc/meminfo", encoding="ascii") as f:
                meminfo_text = f.read()
        except OSError:
            return None
    for line in meminfo_text.splitlines():
        if line.startswith("MemAvailable:"):
            try:
                return round(int(line.split()[1]) / 1048576, 2)
            except (IndexError, ValueError):
                return None
    return None


def disk_free_gb(path: str) -> float | None:
    """Free space of the filesystem holding the path (os.statvfs, f_bavail — the ordinary-user metric). Returns None on failure."""
    try:
        st = os.statvfs(path)
        return round(st.f_bavail * st.f_frsize / 1024 ** 3, 2)
    except OSError:
        return None


def _cpu_cores() -> int:
    return os.cpu_count() or 4


def _os_type() -> str:
    s = platform.system()
    if s == "Windows":
        return "windows"
    if s == "Darwin":
        return "mac"
    return "linux"


def _is_wsl2() -> bool:
    # the WSL2 kernel version string contains "microsoft" (e.g. ...microsoft-standard-WSL2)
    try:
        with open("/proc/version", encoding="ascii") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _read_rotational() -> dict:
    out = {}
    try:
        devs = os.listdir("/sys/block")
    except OSError:
        return out
    for dev in devs:
        try:
            with open(f"/sys/block/{dev}/queue/rotational", encoding="ascii") as f:
                out[dev] = int(f.read().strip())
        except (OSError, ValueError):
            continue
    return out


def _bench_iops(directory: str, seconds: float = 0.5, max_reads: int = 20000,
                page: int = 4096) -> tuple[float | None, str | None]:
    """O_DIRECT 4K random-read micro-benchmark. Returns (iops, error message). The test
    file is deleted right after writing/running.

    O_DIRECT requires: a page-aligned buffer (manual alignment via ctypes) and offsets in
    whole-page multiples — os.preadv writes directly into the caller-provided buffer,
    avoiding the misaligned internal allocation of os.pread."""
    if not hasattr(os, "O_DIRECT"):
        return None, "当前平台无 os.O_DIRECT"
    path = os.path.join(directory, f".pgtuner_bench_{os.getpid()}")
    try:
        try:
            chunk = os.urandom(page) * (1024 * 1024 // page)   # 1MB of random content
            with open(path, "wb") as f:
                for _ in range(BENCH_FILE_MB):
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            return None, f"写入测试文件失败: {e}"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECT)
        except OSError as e:
            return None, f"O_DIRECT 打开失败: {e}"
        try:
            raw = ctypes.create_string_buffer(2 * page)
            addr = ctypes.addressof(raw)
            buf = (ctypes.c_char * page).from_address(addr + (-addr % page))
            npages = BENCH_FILE_MB * 1024 * 1024 // page
            offsets = [random.randrange(npages) * page for _ in range(4096)]
            for i in range(16):                                # warm-up
                os.preadv(fd, [buf], offsets[i])
            t0 = time.perf_counter()
            n = i = 0
            while n < max_reads:
                os.preadv(fd, [buf], offsets[i])
                i = (i + 1) % len(offsets)
                n += 1
                if n % 64 == 0 and time.perf_counter() - t0 >= seconds:
                    break
            elapsed = time.perf_counter() - t0
            return (n / elapsed if elapsed > 0 else None), None
        finally:
            os.close(fd)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _decide_disk(bench_path: str, is_wsl2: bool) -> tuple[str, str, dict, float | None, str | None, list]:
    """Combine multiple signals into disk_type and confidence. Returns (type, confidence, rotational, iops, err, notes)."""
    notes: list = []
    rotational = _read_rotational()
    if is_wsl2:
        notes.append("WSL2：/sys/block rotational 不可信（虚拟盘全部报 rotational=1），仅以微基准为准")
    if rotational and not is_wsl2:
        notes.append("rotational: " + ", ".join(f"{k}={v}" for k, v in sorted(rotational.items())))
    iops, err = _bench_iops(bench_path)
    if iops is None:
        # fallback: on probe failure assume conservative ssd (never aggressive on "might be SSD/NVMe")
        return "ssd", "low", rotational, None, err or "微基准失败", notes + ["探测失败，保守取 ssd"]
    disk = classify_disk_by_iops(iops)
    if is_wsl2:
        conf = "medium" if disk != "hdd" else "low"
        if disk == "hdd":
            notes.append("WSL2 下 hdd 判定存疑（rotational 失效、盘多为 NVMe 背书 VHDX）")
    else:
        rots = set(rotational.values())
        agree = (disk == "hdd" and rots == {1}) or (disk in ("ssd", "nvme") and rots == {0})
        conf = "high" if agree else "medium"
        if not agree and rots:
            notes.append("rotational 标志与微基准结果不一致，以微基准为准")
    notes.append(f"O_DIRECT 4K 随机读微基准: {iops:.0f} IOPS（{bench_path}，QD1 等价，阈值低于 fio QD32 口径）")
    return disk, conf, rotational, iops, None, notes


def probe(bench_path: str = "/tmp", data_dir: str = "/tmp") -> HardwareInfo:
    is_wsl2 = _is_wsl2()
    disk_type, confidence, rotational, iops, err, notes = _decide_disk(bench_path, is_wsl2)
    return HardwareInfo(
        total_ram_gb=_total_ram_gb(),
        cpu_cores=_cpu_cores(),
        os_type=_os_type(),
        is_wsl2=is_wsl2,
        disk_type=disk_type,
        disk_confidence=confidence,
        rotational=rotational,
        bench_iops=iops,
        bench_path=bench_path,
        bench_error=err,
        disk_notes=notes,
        mem_available_gb=_mem_available_gb(),
        disk_free_gb=disk_free_gb(data_dir),
    )
