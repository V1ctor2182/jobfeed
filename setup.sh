#!/usr/bin/env bash
# Install and open Jobfeed. The Applier has its own setup; see README.md.
set -euo pipefail
exec "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/jobfeed/setup.sh" "$@"
