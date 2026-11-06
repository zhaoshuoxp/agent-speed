#!/usr/bin/env bash
# Print the first compatible interpreter; usable from Finder and Terminal.
set -euo pipefail
agent_find_python() {
    local candidate
    local -a candidates
    if (( $# > 0 )); then
        candidates=("$@")
    else
        candidates=("${AGENT_MAINTENANCE_PYTHON:-}")
        candidates+=(/Library/Frameworks/Python.framework/Versions/Current/bin/python3)
        candidates+=(/Library/Frameworks/Python.framework/Versions/*/bin/python3)
        candidates+=("$HOME/.local/bin/python3" /opt/homebrew/bin/python3 /usr/local/bin/python3)
        candidate="$(command -v python3 2>/dev/null || true)"
        [[ -z "$candidate" ]] || candidates+=("$candidate")
        candidates+=(/usr/bin/python3)
    fi
    for candidate in "${candidates[@]}"; do
        [[ -n "$candidate" && -x "$candidate" ]] || continue
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    echo 'Python 3.9+ was not found. Install Python 3, then retry.' >&2
    return 1
}
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then agent_find_python "$@"; fi
