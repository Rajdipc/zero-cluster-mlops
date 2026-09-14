#!/usr/bin/env bash
# ==============================================================================
# Helper Script: Execute Pipeline Orchestrator Locally
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_DIR}"

if [[ -f .env ]]; then
  echo "Loading environment from .env..."
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

echo "Running BQML batch pipeline locally..."
python -m src.orchestrator "$@"
