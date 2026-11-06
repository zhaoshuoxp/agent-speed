#!/usr/bin/env bash
# Compatibility entry point: use the unified native Codex and Claude updater.
set -euo pipefail
MAINTENANCE_BIN="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/agent-maintenance"
arguments=(upgrade --tool all --apply)
if (( $# > 0 )); then
    for alias in "$@"; do arguments+=(--host "$alias"); done
else
    arguments+=(--host all)
fi
exec "$MAINTENANCE_BIN" "${arguments[@]}"
