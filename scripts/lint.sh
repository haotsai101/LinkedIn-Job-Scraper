#!/usr/bin/env bash
# One command for lint: bootstrap .venv/ (first run only), then run ruff.
# Extra args are forwarded to ruff, e.g. ./scripts/lint.sh --fix
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_venv.sh
source "$DIR/_venv.sh"

exec "$VENV_DIR/bin/ruff" check "$REPO_ROOT" "$@"
