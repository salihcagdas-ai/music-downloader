#!/usr/bin/env bash
# Run from any directory. Package installation is an explicit setup step.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  printf '%s\n' 'Set up Python 3.11+ first:' '  python3 -m venv .venv' '  .venv/bin/python -m pip install -r requirements.txt'
  exit 1
fi
exec .venv/bin/python app.py "$@"
