#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/ccf-paper-scout}"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env.local}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

cd "$PROJECT_DIR"
exec /usr/bin/python3 paper_scout.py --config config.json --output recommendations.md
