#!/bin/bash
# The applier_helpers gate: registry consistency plus every helper against its fixture page.
cd "$(dirname "$0")/.." || exit 2
ROOT="$(cd .. && pwd)"
PY=""
# A worktree has no .venv of its own, so fall back to the main checkout next to it.
for candidate in "${APPLIER_PYTHON:-}" "$ROOT/.venv/bin/python" "$ROOT/../job-application-workspace/.venv/bin/python"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then PY="$candidate"; break; fi
done
[ -n "$PY" ] || { echo "no Python environment found" >&2; exit 2; }
APPLIER_HELPER_GATE=1 PYTHONPATH=.:tests exec "$PY" -m pytest tests/test_applier_helpers.py -q
