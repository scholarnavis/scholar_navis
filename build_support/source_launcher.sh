#!/usr/bin/env bash
# Scholar Navis —— Linux 源码包的启动器模板。
#
# 这份文件是仓库里**唯一**一份启动器：打包时由 build_app.py 原样复制为包内的
# run.sh（见 build_app.py::build_source_bundle），因此不要在别处再写一份。
#
# 职责边界：只做"准备运行环境 + 启动应用"，不参与打包/发布（那是 build_app.py 的事）。
# 依赖解析统一交给 uv：uv.lock 是唯一被锁定的依赖清单，--locked 保证不重新解析。
# 首次运行会在**包目录内**创建 .venv（torch / onnxruntime 等，数 GB），所以请把包
# 解压到空间充足且可写的目录；--no-dev 保证不把 PyInstaller / boto3 之类的构建期
# 依赖装到用户机器上。
#
# 退出码约定（与 main.py 对齐）：
#   0  正常退出（含 --help）
#   3  运行环境不满足（未安装 uv）
#   其它  应用自身的退出码
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

log() { printf '[run.sh] %s\n' "$*" >&2; }
die() { log "$*"; exit 3; }

usage() {
    cat <<'USAGE'
Usage: ./run.sh [options passed to main.py]

  (no arguments)   start the GUI
  --api-server     run only the API server (no GUI, for headless hosts)
  -h, --help       show this help

Environment: the first run downloads the runtime dependencies into ./.venv
(several GB). Logs are written to ./logs/.
USAGE
}

case "${1:-}" in
    -h|--help)
        usage
        exit 0
        ;;
esac

# 只支持 uv 引导：requirements.txt 里的 torch 带 +cpu 本地版本段且指向 PyTorch 的
# CPU 专用索引（见 pyproject.toml 的 [tool.uv.sources]），用 pip 直接安装会因索引
# 不同而哈希校验失败；uv 会按 pyproject.toml 选对索引。
if ! command -v uv >/dev/null 2>&1; then
    die "uv was not found. Install it and re-run this script:
       curl -LsSf https://astral.sh/uv/install.sh | sh
     (or: pipx install uv; see https://docs.astral.sh/uv/ )"
fi

log "uv $(uv --version 2>&1 | awk '{print $2}')"
log "Syncing runtime dependencies from uv.lock (first run downloads several GB)..."
uv sync --locked --no-dev

log "Launching Scholar Navis (application logs go to ./logs/)..."
exec uv run --locked --no-dev main.py "$@"