#!/usr/bin/env bash
# One command for the full local check: bootstrap .venv/ (first run only),
# then run ruff + pytest. Both always run; the script exits non-zero if either
# fails. Extra args are forwarded to pytest, e.g. ./scripts/check.sh -k offsite -x
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_venv.sh
source "$DIR/_venv.sh"

rc=0

echo "==> ruff"
"$VENV_DIR/bin/ruff" check "$REPO_ROOT" || rc=1

# Install Playwright's Chromium once, only if it's missing. The current test
# suite import-guards Playwright, but browser-driven tests need the binary.
if ! "$VENV_DIR/bin/python" - <<'PY' >/dev/null 2>&1
import os
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    assert os.path.exists(p.chromium.executable_path)
PY
then
    echo "==> installing Playwright Chromium (one-time)"
    "$VENV_DIR/bin/playwright" install chromium
fi

echo "==> pytest"
"$VENV_DIR/bin/pytest" "$REPO_ROOT" "$@" || rc=1

exit "$rc"
