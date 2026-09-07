#!/usr/bin/env bash
# One command for the full local check: bootstrap .venv/ (first run only),
# then run ruff + pytest. Extra args are forwarded to pytest, e.g.
#   ./scripts/check.sh -k offsite -x
#
# Exit status: 0 when pytest passed AND ruff either passed or only reported lint
# findings (rc 1) — the repo carries a deliberate ~509-finding baseline, so a
# clean ruff exit is not expected. A ruff crash (rc >= 2) or any pytest failure
# exits 1.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_venv.sh
source "$DIR/_venv.sh"

# Run from the repo root: some modules read data files by relative path at import
# time (e.g. scripts/helpers.py -> json_paths/data_variables.csv), so pytest
# collection fails if invoked from elsewhere.
cd "$REPO_ROOT"

echo "==> ruff"
ruff_rc=0
"$VENV_DIR/bin/python" -m ruff check "$REPO_ROOT" || ruff_rc=$?

echo "==> pytest"
pytest_rc=0
"$VENV_DIR/bin/python" -m pytest "$REPO_ROOT/tests" "$@" || pytest_rc=$?

echo "==> summary: ruff exit ${ruff_rc} (baseline ~509 findings expected), pytest exit ${pytest_rc}"

# succeed when pytest passed AND ruff either passed or only reported lint
# findings (rc 1), not a crash (rc 2+)
if [ "$pytest_rc" -eq 0 ] && { [ "$ruff_rc" -eq 0 ] || [ "$ruff_rc" -eq 1 ]; }; then
    exit 0
fi
exit 1
