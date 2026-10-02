#!/usr/bin/env bash
#
# Remove the Cloud Run service this ADK deployment created.
#
#   ./deploy/teardown.sh          # prompts before deleting
#   ./deploy/teardown.sh --yes    # no prompt
#
# Only touches services labelled managed-by=ai-csuite-adk, so it can never
# delete an Option 1 service. Secrets and images are left in place.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log() { printf '\033[1;34m>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mx\033[0m %s\n' "$*" >&2; exit 1; }

[[ -f .env ]] || die ".env not found."
set -a; source .env; set +a

GCP_PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
[[ -n "$GCP_PROJECT_ID" ]] || die "Set GCP_PROJECT_ID in .env."
GCP_REGION="${GCP_REGION:?Set GCP_REGION in .env}"
SERVICE_PREFIX="$(printf '%s' "${SERVICE_PREFIX:?Set SERVICE_PREFIX in .env}" \
                  | tr '[:upper:]' '[:lower:]')"
SERVICE_NAME="${SERVICE_PREFIX}-csuite"

LABEL="$(gcloud run services describe "$SERVICE_NAME" --project "$GCP_PROJECT_ID" \
          --region "$GCP_REGION" --format='value(metadata.labels.managed-by)' 2>/dev/null || true)"

if [[ "$LABEL" != "ai-csuite-adk" ]]; then
  log "Nothing to remove: no service '${SERVICE_NAME}' in ${GCP_REGION} carries the ADK label."
  exit 0
fi

printf '\nThis will DELETE Cloud Run service "%s" from %s (%s).\n' \
  "$SERVICE_NAME" "$GCP_PROJECT_ID" "$GCP_REGION"
printf 'Artifact Registry images, the runtime service account and any secrets stay.\n\n'

if [[ "${1:-}" != "--yes" ]]; then
  read -r -p "Type the service name '${SERVICE_NAME}' to confirm: " CONFIRM
  [[ "$CONFIRM" == "$SERVICE_NAME" ]] || die "Cancelled."
fi

gcloud run services delete "$SERVICE_NAME" --project "$GCP_PROJECT_ID" \
  --region "$GCP_REGION" --quiet

printf '\n\033[1;32mRemoved %s.\033[0m\n\n' "$SERVICE_NAME"
