#!/usr/bin/env bash
# pgtuner one-command dependency provisioning (idempotent, safe to rerun; v0.11.0) — the automated version of README's install section.
#
# Usage:
#   bash scripts/setup.sh                     # default: Python side (repo .venv + pgtuner -e install), no sudo
#   bash scripts/setup.sh --system            # add: system build dependencies (apt + sudo, confirmed before install)
#   bash scripts/setup.sh --pg <prefix>       # add: build PG (+contrib pg_stat_statements) into <prefix>
#   bash scripts/setup.sh --full <prefix>     # all three stages (from bare machine to ready-to-run)
#
# Environment variables:
#   PGTUNER_PG_SRC              use a local PG source tree (skip the clone; e.g. ~/src/postgres)
#   PGTUNER_PG_BRANCH           branch to clone, default master (tracks PostgreSQL devel)
#   PGTUNER_PG_CONFIGURE_FLAGS  passed through to configure; debug build example:
#                               PGTUNER_PG_CONFIGURE_FLAGS="--enable-debug --enable-cassert" \
#                                 bash scripts/setup.sh --pg ~/pg-dbg
#
# Idempotency: each stage first probes whether already satisfied (venv has pgtuner /
# apt packages installed / prefix has pg_config) and skips; reruns only fill gaps, never rebuild.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_ROOT/.venv"
APT_PKGS=(build-essential bison flex libreadline-dev zlib1g-dev libicu-dev
          pkg-config python3-venv python3-pip git)

ok()   { printf '\033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '\033[33m⚠\033[0m %s\n' "$*"; }
log()  { printf '\033[36m…\033[0m %s\n' "$*"; }

usage() { sed -n '2,/^[^#]/p' "$0" | sed '/^[^#]/d; s/^# \{0,1\}//'; }   # header comment block, however long it grows

# Python floor (matches pyproject requires-python and the CI 3.9–3.12 matrix): fail fast, before any stage runs
python3 -c 'import sys; sys.exit(not (sys.version_info >= (3, 9)))' \
  || { warn "需要 python3 >= 3.9(当前: $(python3 -V 2>&1))——请升级后重跑本脚本"; exit 1; }

# ---------------- Stage 1: Python side (deps isolated, installed into the repo .venv, system pip untouched) ----------------
stage_python() {
  if [ ! -x "$VENV/bin/python" ]; then
    log "创建 venv: $VENV"
    python3 -m venv "$VENV"
  else
    log "venv 已存在: $VENV"
  fi
  if "$VENV/bin/python" -c "import pgtuner" >/dev/null 2>&1; then
    log "pgtuner 已在 venv 内(增量刷新依赖,pip install -e 幂等)"
  fi
  # -e editable install: the pgtuner inside the venv always tracks the repo source (not a copy)
  "$VENV/bin/pip" install --quiet -e "$REPO_ROOT[db,dev]"
  ok "Python 依赖就绪(独立于系统环境): $VENV"
  echo "  用法 A: source $VENV/bin/activate && pgtuner doctor"
  echo "  用法 B: $VENV/bin/pgtuner doctor"
}

# ---------------- Stage 2: system build dependencies (sudo, confirmed item by item before install) ----------------
stage_system() {
  if ! command -v apt-get >/dev/null 2>&1; then
    warn "未检测到 apt-get(非 Debian/WSL2?)——请手动安装后重跑:"
    echo "  ${APT_PKGS[*]}"
    return 0
  fi
  local need=() p
  for p in "${APT_PKGS[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || need+=("$p")
  done
  # Functional probing overrides dpkg bookkeeping drift (e.g. WSL's python installed outside apt): if venv/pip work, it doesn't count as missing
  local filtered=()
  for p in "${need[@]}"; do
    case "$p" in
      python3-venv) python3 -m venv --help >/dev/null 2>&1 || filtered+=("$p") ;;
      python3-pip)  python3 -m pip --version >/dev/null 2>&1 || filtered+=("$p") ;;
      *)            filtered+=("$p") ;;
    esac
  done
  need=("${filtered[@]}")
  if [ "${#need[@]}" -eq 0 ]; then
    ok "系统编译依赖已齐(apt 包全部就位)"
    return 0
  fi
  echo "将用 sudo 安装: ${need[*]}"
  local ans
  read -r -p "确认?[y/N] " ans || true   # piped/non-interactive EOF counts as No; don't abort the script
  case "$ans" in
    y|Y|yes|YES)
      sudo apt-get update && sudo apt-get install -y "${need[@]}"
      ok "系统编译依赖安装完成"
      ;;
    *)
      warn "已跳过——缺包时 PG 编译阶段会失败,可随时重跑本阶段"
      ;;
  esac
}

# ---------------- Stage 3: build PG + contrib (pg_stat_statements) ----------------
_next_steps() {
  echo "  下一步:"
  echo "    pgtuner instance create <实例名> $1"
  echo "    pgtuner lab bootstrap --instance <实例名>   # pgss 预载 + 标准模板 + 默认实例"
  echo "    pgtuner doctor                              # 全绿即可开工"
}

stage_pg() {
  local prefix="$1"
  if [ -x "$prefix/bin/pg_config" ] || [ -x "$prefix/pg_config" ]; then
    ok "PG 已装于 $prefix(pg_config 就位),跳过编译(要重编:换 prefix 或删掉后重跑)"
    _next_steps "$prefix"
    return 0
  fi
  local src="${PGTUNER_PG_SRC:-}"
  if [ -z "$src" ]; then
    src="${TMPDIR:-/tmp}/pgtuner-pg-src/postgres"
    if [ ! -d "$src" ]; then
      log "clone PG 源码(分支 ${PGTUNER_PG_BRANCH:-master})→ $src"
      mkdir -p "$(dirname "$src")"
      git clone --depth 1 --branch "${PGTUNER_PG_BRANCH:-master}" \
        https://github.com/postgres/postgres.git "$src"
    else
      log "复用源码缓存: $src"
    fi
  fi
  log "configure + make -j$(nproc) + install → $prefix(约几分钟)"
  # shellcheck disable=SC2086  # PGTUNER_PG_CONFIGURE_FLAGS needs word splitting
  ( cd "$src" \
    && ./configure --prefix="$prefix" ${PGTUNER_PG_CONFIGURE_FLAGS:-} \
    && make -j"$(nproc)" \
    && make install \
    && make -C contrib/pg_stat_statements install )
  ok "PG + contrib/pg_stat_statements 已装于 $prefix"
  _next_steps "$prefix"
}

MODE=python
PREFIX=""
case "${1:-}" in
  "")        MODE=python ;;
  --system)  MODE=system ;;
  --pg)      MODE=pg;      PREFIX="${2:?--pg 需要 <prefix> 参数}" ;;
  --full)    MODE=full;    PREFIX="${2:?--full 需要 <prefix> 参数}" ;;
  -h|--help) usage; exit 0 ;;
  *)         usage >&2; exit 2 ;;
esac

case "$MODE" in
  python) stage_python ;;
  system) stage_system ;;
  pg)     stage_pg "$PREFIX" ;;
  full)   stage_system; stage_python; stage_pg "$PREFIX" ;;
esac
