#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python_bin="$repo_dir/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  python_bin=python3
fi
exec "$python_bin" "$repo_dir/pipeline/word_conversion.py" convert "$@"
