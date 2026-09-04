#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
# Dependencies are installed at build time; boot must never fetch or replace code.
if [[ -x .venv/bin/python ]]; then
    exec .venv/bin/python -m Backend
fi
exec python -m Backend
