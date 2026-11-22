#!/usr/bin/env bash
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/../.." && pwd)"

"$here/setup.sh"

cd "$here"
export PYTHONPATH="$repo_root${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m herda run graph.py
