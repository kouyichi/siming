#!/usr/bin/env bash
set -euo pipefail
export TMPDIR="${TMPDIR:-$HOME/.cache/agent-tmp}"
if [[ "$TMPDIR" == "/tmp" || "$TMPDIR" == /tmp/* ]]; then
  export TMPDIR="$HOME/.cache/agent-tmp"
fi
mkdir -p "$TMPDIR"
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
SIMING_ROOT="${SIMING_REPO_ROOT:-$SCRIPT_DIR}"
if [[ -x "$SIMING_ROOT/.pixi/envs/default/bin/siming" ]]; then
  exec "$SIMING_ROOT/.pixi/envs/default/bin/siming" "$@"
elif [[ -x "$SIMING_ROOT/.venv/bin/siming" ]]; then
  exec "$SIMING_ROOT/.venv/bin/siming" "$@"
else
  exec python3 -m siming.cli "$@"
fi
