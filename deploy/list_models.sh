#!/usr/bin/env bash
#
# List the Gemini models this project can actually call, and check whether the
# MODEL_NAME in .env is one of them.
#
#   ./deploy/list_models.sh            # checks VERTEX_LOCATION from .env
#   ./deploy/list_models.sh global     # checks a specific location
#
# Run this before deploying. Model availability varies by project and by
# location, and a name that works in one is routinely absent from another.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log() { printf '\033[1;34m>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mx\033[0m %s\n' "$*" >&2; exit 1; }

[[ -f .env ]] || die ".env not found. Run: cp .env.example .env"
set -a; source .env; set +a

command -v gcloud >/dev/null || die "gcloud CLI not found."
GCP_PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
[[ -n "$GCP_PROJECT_ID" ]] || die "Set GCP_PROJECT_ID in .env."

LOCATION="${1:-${VERTEX_LOCATION:-us-central1}}"
WANTED="${MODEL_NAME:-}"

if [[ "$LOCATION" == "global" ]]; then
  HOST="aiplatform.googleapis.com"
else
  HOST="${LOCATION}-aiplatform.googleapis.com"
fi

log "Project ..... $GCP_PROJECT_ID"
log "Location .... $LOCATION"
log "Looking for . ${WANTED:-(MODEL_NAME not set)}"
echo

TOKEN="$(gcloud auth print-access-token 2>/dev/null)" \
  || die "Could not get an access token. Run: gcloud auth login"

RESPONSE="$(curl -sS \
  -H "Authorization: Bearer ${TOKEN}" \
  "https://${HOST}/v1/publishers/google/models?pageSize=200" || true)"

if [[ -z "$RESPONSE" ]] || printf '%s' "$RESPONSE" | grep -q '"error"'; then
  printf '%s\n\n' "$RESPONSE"
  die "Could not list models in '${LOCATION}'. Check that aiplatform.googleapis.com
   is enabled and that you have access to this project."
fi

MODELS="$(printf '%s' "$RESPONSE" \
  | tr ',' '\n' | grep -o '"name": *"[^"]*"' \
  | sed 's/.*models\///; s/"//g' | sort -u)"

if [[ -z "$MODELS" ]]; then
  die "No publisher models returned for '${LOCATION}'."
fi

printf 'Gemini models available in %s:\n\n' "$LOCATION"
printf '%s\n' "$MODELS" | grep -i gemini | sed 's/^/  /' || printf '  (none matched "gemini")\n'
echo

if [[ -n "$WANTED" ]]; then
  if printf '%s\n' "$MODELS" | grep -qx "$WANTED"; then
    printf '\033[1;32mMODEL_NAME "%s" is available in %s.\033[0m\n\n' "$WANTED" "$LOCATION"
  else
    printf '\033[1;31mMODEL_NAME "%s" is NOT available in %s.\033[0m\n\n' "$WANTED" "$LOCATION"
    printf '  Pick one of the ids above and set MODEL_NAME in .env, or try the\n'
    printf '  global endpoint, which newer models are often restricted to:\n\n'
    printf '    ./deploy/list_models.sh global\n'
    printf '    # then in .env:  VERTEX_LOCATION="global"\n\n'
    exit 1
  fi
fi
