"""Deployment-level configuration (.pgtuner/config.json) — introduced in v0.6.1.

Why: benchmarks measured (S3 [exp-261113-211331-0e83b2ae]) that the CLI group
had to pass `--instance rel19` on every command, plus one dedicated `instance
list` call inside the loop — with a deployment-level default set once (by the
admin/orchestrator via `pgtuner config set default-instance <name>`), all
data-plane commands drop `--instance` and `lab clone` drops the instance
positional argument.

Precedence: explicit flag > environment variable PGTUNER_INSTANCE > config
file. The file lives under .pgtuner/ at the repository root (same anchor as
the ledger, paths.REPO_ROOT — never forks with cwd).

A corrupt (invalid JSON) config is treated as empty, with a one-line warning
on stderr (once per process — stdout stays machine-readable). save() writes
atomically (same-dir tmp + fsync + os.replace) so a crash/disk-full mid-write
can never leave a truncated config behind.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from .paths import PGTUNER_DIR

CONFIG_FILE = PGTUNER_DIR / "config.json"

# Whether the corrupt-config warning has already been printed in this process
# (load() may be called several times per command; warn once, not per call).
_corrupt_warned = False


def _atomic_write_text(path: Path, text: str) -> None:
    """Crash-safe stand-in for Path.write_text: write a same-directory tmp file,
    flush + fsync, then os.replace it onto the target. A crash or disk-full
    mid-write can never truncate an existing config; the tmp file is removed
    on failure. An existing target keeps its permissions; new files get 0o644,
    matching the previous write_text behavior."""
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o644
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load() -> dict:
    """Read the config; a missing file is a normal empty config (silent), while a
    corrupt file (invalid JSON) warns once per process on stderr — the user who
    hand-edited config.json badly should see why every default looks unset."""
    global _corrupt_warned
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        if not _corrupt_warned:
            _corrupt_warned = True
            print(f"警告:配置文件损坏(非法 JSON),已按空配置处理:{CONFIG_FILE}", file=sys.stderr)
        return {}
    except (OSError, ValueError):
        return {}


def save(key: str, value) -> dict:
    cfg = load()
    cfg[key] = value
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(CONFIG_FILE, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    return cfg


def default_instance() -> str | None:
    """Deployment-level default instance name (env overrides the file; neither set → None, caller reports the error as needed)."""
    return os.environ.get("PGTUNER_INSTANCE") or load().get("default_instance")
