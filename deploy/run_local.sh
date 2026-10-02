#!/usr/bin/env bash
#
# Run the ADK C-Suite locally. One process, five ADK agents.
#
#   ./deploy/run_local.sh
#
# Uses ADC for Vertex (run `gcloud auth application-default login` first), or
# set ADK_BACKEND=api_key and GEMINI_API_KEY in .env.
#
# Defaults to port 8081 so it can run alongside Option 1 on 8080.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log() { printf '\033[1;34m>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mx\033[0m %s\n' "$*" >&2; exit 1; }

[[ -f .env ]] || die ".env not found. Run: cp .env.example .env"
set -a; source .env; set +a

ADK_BACKEND="${ADK_BACKEND:-vertex}"
if [[ "$ADK_BACKEND" == "vertex" ]]; then
  [[ -n "${GCP_PROJECT_ID:-}" && "$GCP_PROJECT_ID" != "your-project-id" ]] \
    || die "ADK_BACKEND=vertex needs a real GCP_PROJECT_ID in .env."
  log "Using Vertex AI via application default credentials."
  log "If this fails to authenticate, run: gcloud auth application-default login"
else
  [[ -n "${GEMINI_API_KEY:-}" ]] \
    || die "ADK_BACKEND=api_key needs GEMINI_API_KEY in .env for local runs."
fi

export PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}"
export AGENTS_DIR="${REPO_ROOT}/agents"
export CONTEXT_DIR="${REPO_ROOT}/context"

PORT="${LOCAL_PORT:-8081}"
printf '\n\033[1;32mDashboard: http://127.0.0.1:%s\033[0m\n\n' "$PORT"

exec python -m uvicorn adk_csuite.main:app --host 127.0.0.1 --port "$PORT"
