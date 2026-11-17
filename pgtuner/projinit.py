"""projinit: distribution-layer engine (v0.10.0) — installs pgtuner's knowledge and state
skeleton into **any project** (or the user's global scope).

Why: the tool repo's self-contained knowledge layer (AGENTS.md/CLAUDE.md/skills) only takes
effect for agents "working inside the pgtuner repo"; a new user opening an agent in their own
project starts with zero knowledge. init is the distribution layer: the single style source =
the packaged assets in pgtuner/assets (byte-for-byte copies of the repo-root originals —
tests/test_packaging.py asserts the sync), copied/merged into the target project; SKILL.md is
an open cross-agent standard format — dropped into the skills directory of .zcode/.claude it
is auto-discovered by ZCode/Claude Code (ecosystem-compatible with `npx skills add`-style
installers). Packaging the assets (v0.11.0) made init work from wheel/sdist installs too;
missing assets = a broken installation, error out explicitly.

v0.11.0 --global seven-piece set: skills×3 + user-level rule marker blocks×3 + PATH wiring.
All three vendors' user-level landing spots are verified: `~/.claude/CLAUDE.md`,
`~/.codex/AGENTS.md` (the codex binary ships a global AGENTS.md loader), and
`~/.zcode/AGENTS.md` (zcode runtime reads `$HOME/.zcode/AGENTS.md`); skills directories
`~/.zcode|~/.claude|~/.codex/skills`. Existing rule files only get a marker block appended;
original content (e.g. @import shims) is left untouched.

Idempotent: reruns only fill gaps — AGENTS.md appends/replaces via the
<!-- pgtuner:begin/end --> marker block (no duplicate appends), skills are skipped when
content matches (--force only needed when different), .gitignore is skipped when it already
has .pgtuner/.

All user-file writes go through _atomic_write_text (same-dir tmp + fsync + os.replace):
a crash or disk-full mid-write can never truncate a pre-existing user file (~/.bashrc,
rule files, AGENTS.md, skills).
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from . import paths

AGENTS_BEGIN = "<!-- pgtuner:begin -->"
AGENTS_END = "<!-- pgtuner:end -->"
CLAUDE_IMPORT = "@AGENTS.md"

# Distribution assets (packaged in pgtuner/assets since v0.11.0 so non-editable installs
# work; kept in sync with the repo-root originals by tests/test_packaging.py)
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
ASSET_AGENTS = ASSETS_DIR / "AGENTS.md"
ASSET_SKILL = ASSETS_DIR / "SKILL.md"
SKILL_DIRS = (".zcode", ".claude")             # project-level skills landing spots (codex uses the project-root AGENTS.md)
GLOBAL_SKILL_DIRS = (".zcode", ".claude", ".codex")   # user-level skills landing spots (codex has ~/.codex/skills)
# User-level rule files (agent → path relative to ~/; all three verified to be read)
GLOBAL_RULE_FILES = ((".claude", "CLAUDE.md"), (".codex", "AGENTS.md"), (".zcode", "AGENTS.md"))

# User-level rules digest (condensed version applicable to all projects; full version in the tool repo's AGENTS.md)
USER_RULES = """\
# pgtuner —— PG 实验铁律(用户级摘要;完整版见 pgtuner 工具仓 AGENTS.md,双语)

1. 性能实验只用 pgtuner 命令(bench / rebench / planbench / lab),禁止裸 psql 计时下性能结论;
   功能性检查/数据探查用 `pgtuner sql`(只读强制、不输出计时)。
2. 每次实验必须落实验账本(experiment-id 由工具自动生成,`pgtuner results list/diff/conclude`);
   结论必须引用 `results diff` 的证据,低于噪声地板的差异表述为"不显著"。
3. 慢通道 rebench(含重启)先报预计时长征得确认;跑完必须确认输出含 `restored: True`。
4. 性能结论只认 release 实例(instance list 里 build_type=release);debug 实例只做功能验证。
5. 开工先 `pgtuner doctor`(❌ 清零再干活);一次性实验库用 `pgtuner lab clone --template <tpl>`
   克隆,绝不自己 initdb,用完 lab release 归还。

未装工具的机器:见 pgtuner 工具仓 README 安装节(`bash scripts/setup.sh` 一键)。
在某个项目启用完整知识层+状态骨架:`cd <项目> && pgtuner init`。
"""


def _step(name: str, changed: bool, note: str) -> dict:
    return {"name": name, "changed": changed, "note": note}


def _atomic_write_text(path: Path, text: str) -> None:
    """Crash-safe stand-in for Path.write_text: write a same-directory tmp file,
    flush + fsync, then os.replace it onto the target. A crash or disk-full
    mid-write can never truncate an existing user file; the tmp file is removed
    on failure. An existing target keeps its permissions (os.replace would
    otherwise carry the tmp file's mode); new files get 0o644, matching the
    previous write_text behavior."""
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


def _assets() -> tuple[str, str]:
    """Read the distribution assets; error when missing (editable-install premise, no silent degradation)."""
    if not ASSET_AGENTS.is_file() or not ASSET_SKILL.is_file():
        raise ValueError(
            f"发行资产缺失(安装损坏:包内 assets 目录不完整,请重装 pgtuner): {ASSET_AGENTS} / {ASSET_SKILL}")
    return (ASSET_AGENTS.read_text(encoding="utf-8"),
            ASSET_SKILL.read_text(encoding="utf-8"))


def _copy_skill(dest_root: Path, skill_text: str, *, force: bool, label: str) -> dict:
    """Copy SKILL.md to dest_root/skills/pg-experiment/ (skip when identical; --force required when different)."""
    dest = dest_root / "skills" / "pg-experiment" / "SKILL.md"
    if dest.is_file():
        if dest.read_text(encoding="utf-8") == skill_text:
            return _step(f"skill→{label}", False, "已一致,跳过")
        if not force:
            return _step(f"skill→{label}", False, f"已存在且内容不同(覆盖加 --force): {dest}")
        _atomic_write_text(dest, skill_text)
        return _step(f"skill→{label}", True, "已覆盖(--force)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(dest, skill_text)
    return _step(f"skill→{label}", True, str(dest))


def _apply_block(cur: str, block: str) -> tuple[str | None, str]:
    """Marker-block merge core: block present → replace (return (None,"same") when identical);
    no block → append. mode ∈ {"same","replace","append"}; replace needs the caller's force
    before writing to disk, append writes directly."""
    # The replacement goes through a lambda on purpose (R2-D finding): re.sub
    # treats a string replacement as a template — the shipped AGENTS.md contains
    # `\timing` and the \t was silently expanded into a TAB, corrupting the merged
    # file. count=1 keeps a stray second marker block from being expanded in the
    # same pass.
    if AGENTS_BEGIN in cur and AGENTS_END in cur:
        new = re.sub(re.escape(AGENTS_BEGIN) + r".*?" + re.escape(AGENTS_END),
                     lambda _m: block, cur, count=1, flags=re.S)
        return (None, "same") if new == cur else (new, "replace")
    return cur.rstrip() + "\n\n" + block + "\n", "append"


def _write_agents(agents_path: Path, agents_src: str, *, force: bool) -> dict:
    """Land AGENTS.md: absent → write the whole file; present → append/replace the marker block (idempotent, no duplicate appends)."""
    if not agents_path.exists():
        _atomic_write_text(agents_path, agents_src)
        return _step("AGENTS.md", True, "整份写入")
    cur = agents_path.read_text(encoding="utf-8")
    if cur == agents_src:
        return _step("AGENTS.md", False, "已一致,跳过")
    new, mode = _apply_block(cur, f"{AGENTS_BEGIN}\n{agents_src.rstrip()}\n{AGENTS_END}")
    if mode == "same":
        return _step("AGENTS.md", False, "标记块已是最新,跳过")
    if mode == "append":
        _atomic_write_text(agents_path, new)
        return _step("AGENTS.md", True, "已有文件,追加标记块(原有内容不动)")
    if not force:
        return _step("AGENTS.md", False, "标记块有更新(覆盖加 --force)")
    _atomic_write_text(agents_path, new)
    return _step("AGENTS.md", True, "标记块已更新(--force)")


def _merge_marked(path: Path, content: str, *, force: bool, label: str) -> dict:
    """User-level rule files (v0.11.0): absent → create a marker-block-only file; present →
    append/replace the block, original content untouched (this machine's ~/.claude/CLAUDE.md
    and ~/.codex/AGENTS.md are single-line @import shims that must be preserved)."""
    block = f"{AGENTS_BEGIN}\n{content.rstrip()}\n{AGENTS_END}"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, block + "\n")
        return _step(f"规则→{label}", True, "新建(仅标记块)")
    cur = path.read_text(encoding="utf-8")
    new, mode = _apply_block(cur, block)
    if mode == "same":
        return _step(f"规则→{label}", False, "标记块已是最新,跳过")
    if mode == "append":
        _atomic_write_text(path, new)
        return _step(f"规则→{label}", True, "追加标记块(原有内容不动)")
    if not force:
        return _step(f"规则→{label}", False, "标记块有更新(覆盖加 --force)")
    _atomic_write_text(path, new)
    return _step(f"规则→{label}", True, "标记块已更新(--force)")


def _write_claude(claude_path: Path) -> dict:
    if not claude_path.exists():
        _atomic_write_text(claude_path, CLAUDE_IMPORT + "\n")
        return _step("CLAUDE.md", True, "写入 @AGENTS.md 引导")
    cur = claude_path.read_text(encoding="utf-8")
    if CLAUDE_IMPORT in cur:
        return _step("CLAUDE.md", False, f"已含 {CLAUDE_IMPORT},跳过")
    _atomic_write_text(claude_path, cur.rstrip() + f"\n\n{CLAUDE_IMPORT}\n")
    return _step("CLAUDE.md", True, f"追加 {CLAUDE_IMPORT} 引导行")


def _write_gitignore(target: Path) -> dict:
    gi = target / ".gitignore"
    want = ".pgtuner/"
    if gi.exists():
        cur = gi.read_text(encoding="utf-8")
        if any(line.strip() == want for line in cur.splitlines()):
            return _step(".gitignore", False, "已忽略 .pgtuner/,跳过")
        _atomic_write_text(
            gi, cur.rstrip() + f"\n\n# pgtuner 运行时状态(账本/实例注册表——本地持久,不入库)\n{want}\n")
        return _step(".gitignore", True, "追加 .pgtuner/")
    _atomic_write_text(gi, f"# pgtuner 运行时状态(账本/实例注册表——本地持久,不入库)\n{want}\n")
    return _step(".gitignore", True, "新建并忽略 .pgtuner/")


def init(target_dir: str | Path, *, force: bool = False) -> dict:
    """Land the project-level five-piece set; returns {"steps": [...], "target": ...}.
    1. .pgtuner/{ledger,plans} state skeleton (the directory itself is the anchor marker)
    2. AGENTS.md 3. CLAUDE.md 4. skills×2 (.zcode/.claude) 5. .gitignore. Each step is
    idempotent; reruns only fill gaps."""
    target = Path(target_dir).resolve()
    if not target.is_dir():
        raise ValueError(f"目标目录不存在: {target}")
    agents_src, skill_src = _assets()
    steps: list[dict] = []

    # 1) state skeleton (the .pgtuner directory = the PROJECT_ROOT resolution marker;
    #    after init, the upward search from cwd is guaranteed to hit it)
    fresh = not (target / ".pgtuner").exists()
    for d in ("ledger", "plans"):
        (target / ".pgtuner" / d).mkdir(parents=True, exist_ok=True)
    steps.append(_step("状态骨架", fresh,
                       ".pgtuner/{ledger,plans}" + ("(新建,即状态锚点 marker)" if fresh else " 已存在,跳过")))

    steps.append(_write_agents(target / "AGENTS.md", agents_src, force=force))
    steps.append(_write_claude(target / "CLAUDE.md"))
    for d in SKILL_DIRS:
        steps.append(_copy_skill(target / d, skill_src, force=force, label=d))
    steps.append(_write_gitignore(target))
    return {"steps": steps, "target": str(target)}


RC_BEGIN = "# pgtuner:begin"
RC_END = "# pgtuner:end"
RC_PATH_LINE = 'export PATH="$HOME/.local/bin:$PATH"'


def _launcher_target() -> str | None:
    """B-MINOR-2 (round 4): resolve the executable the wiring should point at —
    the one that is ACTUALLY running, not whichever stale shim happens to sit
    first on PATH. Priority:
      1) the console script installed next to the running interpreter (pip
         install into a venv / user scheme — the running pgtuner itself);
      2) sys.argv[0] when the pgtuner launcher is what was invoked;
      3) the tool-repo .venv (the editable-checkout premise — never exists for
         wheel installs);
      4) shutil.which — last resort: first hit may be an older install."""
    if sys.executable:
        beside = Path(sys.executable).with_name("pgtuner")
        if beside.is_file():
            return str(beside)
    if sys.argv and sys.argv[0] and Path(sys.argv[0]).name == "pgtuner":
        resolved = Path(sys.argv[0]).resolve()
        if resolved.is_file():
            return str(resolved)
    venv_bin = paths.REPO_ROOT / ".venv" / "bin" / "pgtuner"
    if venv_bin.is_file():
        return str(venv_bin)
    return shutil.which("pgtuner")


def _wire_path(home: Path) -> dict:
    """PATH wiring: ~/.local/bin/pgtuner symlink to the running pgtuner
    (_launcher_target priority above). ~/.bashrc is touched only when
    ~/.local/bin is not on PATH (marker-block fallback line) — the only branch
    that ever writes to an rc file."""
    local_bin = home / ".local" / "bin"
    launcher = local_bin / "pgtuner"
    if launcher.exists() or launcher.is_symlink():
        return _step("PATH 接入", False, f"{launcher} 已存在,跳过")
    target = _launcher_target()
    if not target:
        return _step("PATH 接入", False,
                     '未找到 pgtuner 可执行——先 pip install "pgtuner[db]"'
                     "(或工具仓 bash scripts/setup.sh),再重跑 init --global")
    local_bin.mkdir(parents=True, exist_ok=True)
    launcher.symlink_to(target)
    notes = [f"{launcher} → {target}"]
    if str(local_bin) not in os.environ.get("PATH", "").split(os.pathsep):
        bashrc = home / ".bashrc"
        cur = bashrc.read_text(encoding="utf-8") if bashrc.exists() else ""
        if RC_PATH_LINE in cur or RC_BEGIN in cur:
            notes.append("~/.local/bin 不在 PATH(~/.bashrc 已有配置,重开终端生效)")
        else:
            _atomic_write_text(bashrc, cur.rstrip() + f"\n\n{RC_BEGIN}\n{RC_PATH_LINE}\n{RC_END}\n")
            notes.append("~/.local/bin 不在 PATH,已追加 ~/.bashrc 兜底行(重开终端生效)")
    return _step("PATH 接入", True, ";".join(notes))


def init_global(*, force: bool = False) -> dict:
    """User-level seven-piece set (v0.11.0): skills×3 + rule marker blocks×3 + PATH wiring.
    Fully idempotent. Rule landing spots all verified to be read: ~/.claude/CLAUDE.md,
    ~/.codex/AGENTS.md, ~/.zcode/AGENTS.md; skills land in
    ~/.zcode|~/.claude|~/.codex/skills. Project-level assets (AGENTS/CLAUDE/state skeleton)
    are still landed by `pgtuner init` inside the project — the user level only carries the
    universally applicable rules digest."""
    _, skill_src = _assets()
    home = Path.home()
    steps = [_copy_skill(home / d, skill_src, force=force, label=f"~/{d}")
             for d in GLOBAL_SKILL_DIRS]
    for d, name in GLOBAL_RULE_FILES:
        steps.append(_merge_marked(home / d / name, USER_RULES, force=force,
                                   label=f"~/{d}/{name}"))
    steps.append(_wire_path(home))
    steps.append(_step("说明", False,
                       "用户级=普适规则摘要+skills+PATH;项目内再 pgtuner init 落完整知识层+状态骨架"))
    return {"steps": steps}
