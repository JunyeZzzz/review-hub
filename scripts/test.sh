#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; exit(0 if sys.version_info >= (3,10) else 1)'; then
    PY="$cand"
    break
  fi
done
[ -n "$PY" ] || { echo "需要 Python >= 3.10" >&2; exit 1; }

TEST_ENV="$(mktemp -d "${TMPDIR:-/tmp}/review-hub-test.XXXXXX")"
trap 'rm -rf "$TEST_ENV"' EXIT
"$PY" -m venv "$TEST_ENV/venv"
"$TEST_ENV/venv/bin/python" -m pip install -q -r requirements.txt
"$TEST_ENV/venv/bin/python" -m unittest discover -s tests
