#!/usr/bin/env bash
# Install the test app and CLI for the current user; preserve existing versions.
set -euo pipefail
PACKAGE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
APP_PARENT="$HOME/Applications"
CLI_PREFIX="$HOME/Library/Application Support/AgentMaintenance/CLI"
BIN_DIR="$HOME/.local/bin"
DRY_RUN=false

usage() {
    cat <<'HELP'
Usage: bash Install.command [--dry-run] [--app-dir DIR] [--cli-prefix DIR] [--bin-dir DIR]
Installs Agent Maintenance.app and the CLI for the current macOS user.
Requires Python 3.9+ in a location supported by the desktop app.
Existing installations are backed up. SSH credentials and conversations are unchanged.
HELP
}
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        --app-dir|--cli-prefix|--bin-dir)
            [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || { usage >&2; exit 2; }
            case "$1" in
                --app-dir) APP_PARENT="$2" ;;
                --cli-prefix) CLI_PREFIX="$2" ;;
                --bin-dir) BIN_DIR="$2" ;;
            esac
            shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
[[ "$(uname -s)" == Darwin ]] || { echo 'Use python3 cli/install.py on Linux.' >&2; exit 1; }
for tool in /usr/bin/shasum /usr/bin/ditto /usr/bin/codesign /usr/bin/xattr /usr/bin/ssh; do
    [[ -x "$tool" ]] || { echo "Required system tool is unavailable: $tool" >&2; exit 1; }
done

# Use the same version-aware discovery as the desktop app.
source "$PACKAGE_DIR/cli/find-python.sh"
PYTHON_BIN="$(agent_find_python)"
(cd "$PACKAGE_DIR" && /usr/bin/shasum --check SHA256SUMS.txt)
"$PYTHON_BIN" "$PACKAGE_DIR/cli/agent_maintenance.py" --version
printf 'Application destination: %s\nCLI destination: %s\nLauncher: %s/agent-maintenance\n' \
    "$APP_PARENT/Agent Maintenance.app" "$CLI_PREFIX" "$BIN_DIR"
if [[ "$DRY_RUN" == true ]]; then
    echo 'Preflight passed. No installation was performed.'
    exit 0
fi

STAGING_DIR="$(mktemp -d "${TMPDIR:-/tmp/}agent-maintenance-install.XXXXXX")"
trap 'rm -rf "$STAGING_DIR"' EXIT
/usr/bin/ditto -x -k "$PACKAGE_DIR/AgentMaintenance-macOS.zip" "$STAGING_DIR"
STAGED_APP="$STAGING_DIR/Agent Maintenance.app"
# Strip transfer metadata only from this verified development app.
/usr/bin/xattr -cr "$STAGED_APP"
/usr/bin/codesign --verify --deep --strict "$STAGED_APP"

"$PYTHON_BIN" "$PACKAGE_DIR/cli/install.py" --prefix "$CLI_PREFIX" --bin-dir "$BIN_DIR"
"$BIN_DIR/agent-maintenance" --version
"$BIN_DIR/agent-maintenance" hosts --json
mkdir -p "$APP_PARENT"
APP_PATH="$APP_PARENT/Agent Maintenance.app"
if [[ -e "$APP_PATH" ]]; then
    STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
    APP_BACKUP="$APP_PARENT/Agent Maintenance.backup-$STAMP.app"
    [[ ! -e "$APP_BACKUP" ]] || { echo 'Backup destination already exists.' >&2; exit 1; }
    mv "$APP_PATH" "$APP_BACKUP"
    printf 'Previous app retained at: %s\n' "$APP_BACKUP"
fi
/usr/bin/ditto "$STAGED_APP" "$APP_PATH"
/usr/bin/codesign --verify --deep --strict "$APP_PATH"
printf '\nInstalled: %s\nCLI: %s/agent-maintenance\n' "$APP_PATH" "$BIN_DIR"
echo 'Open the app from your user Applications folder. Read README.txt to configure SSH hosts.'
echo 'No conversation maintenance or CLI upgrades were performed.'
