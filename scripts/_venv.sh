# Shared dev-venv bootstrap — sourced by scripts/lint.sh and scripts/check.sh.
# Creates a project-local .venv/ on first run and installs the pinned dev extras
# (ruff + pytest, see pyproject.toml [project.optional-dependencies].dev).
# Idempotent: pip skips already-satisfied requirements, so repeat runs are a
# fast no-op and the venv is never recreated once it exists.
#
# Exports: REPO_ROOT, VENV_DIR  (for the caller to run .venv/bin/<tool>).

# Guard against direct execution: this file only makes sense sourced (it exports
# vars for the caller), and running it standalone would silently do nothing useful.
(return 0 2>/dev/null) || { echo "scripts/_venv.sh must be sourced, not executed" >&2; exit 1; }

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_ROOT/.venv"

if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "==> creating dev venv at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

echo "==> installing dev dependencies (pip no-op if already satisfied)"
# `python -m pip` (not the bin/pip shim) so this still works if the venv's
# console scripts are stale/broken after an interpreter move.
"$VENV_DIR/bin/python" -m pip install -q -e "$REPO_ROOT[dev]"
