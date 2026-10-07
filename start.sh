#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo 'Сначала выполните установку по README.md.' >&2
  exit 1
fi
exec .venv/bin/python main.py "$@"
