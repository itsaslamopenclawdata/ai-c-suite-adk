#!/usr/bin/env bash
#
# Deploy the ADK C-Suite to Google Cloud Run.
#
#   ./deploy/deploy.sh
#
# Creates ONE Cloud Run service: <PREFIX>-csuite. All five ADK agents, the
# orchestration engine and the dashboard run inside it.
#
# Everything is namespaced by SERVICE_PREFIX (default "adk") so this cannot
# collide with the Option 1 deployment in the same project.
#
# Written for bash 3.2, which is what macOS ships.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log() { printf '\033[1;34m>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mx\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
# 0. Configuration
# --------------------------------------------------------------------------
[[ -f .env ]] || die ".env not found. Run: cp .env.example .env  then fill it in."
set -a; source .env; set +a

command -v gcloud >/dev/null || die "gcloud CLI not found."

GCP_PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
[[ -n "$GCP_PROJECT_ID" && "$GCP_PROJECT_ID" != "your-project-id" ]] \
  || die "Set GCP_PROJECT_ID in .env, or run: gcloud config set project <id>"

GCP_REGION="${GCP_REGION:?Set GCP_REGION in .env}"
MODEL_NAME="${MODEL_NAME:?Set MODEL_NAME in .env}"
ADK_BACKEND="${ADK_BACKEND:-vertex}"
VERTEX_LOCATION="${VERTEX_LOCATION:-$GCP_REGION}"
ACTIVE_ROLES_CSV="${ACTIVE_ROLES_CSV:-cfo,cso,cmo,chro,cto}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"
DECISION_LOG_BACKEND="${DECISION_LOG_BACKEND:-memory}"
GEMINI_API_KEY_SECRET="${GEMINI_API_KEY_SECRET:-ADK_GEMINI_API_KEY}"

# --------------------------------------------------------------------------
# 0a. Naming. Cloud Run, Artifact Registry and IAM all require lowercase, so
# normalise rather than fail mid-deploy on a capitalised prefix.
# --------------------------------------------------------------------------
SERVICE_PREFIX="${SERVICE_PREFIX:?Set SERVICE_PREFIX in .env}"
SERVICE_PREFIX_RAW="$SERVICE_PREFIX"
SERVICE_PREFIX="$(printf '%s' "$SERVICE_PREFIX" | tr '[:upper:]' '[:lower:]')"
[[ "$SERVICE_PREFIX" == "$SERVICE_PREFIX_RAW" ]] \
  || log "Normalised SERVICE_PREFIX '${SERVICE_PREFIX_RAW}' -> '${SERVICE_PREFIX}'."

[[ "$SERVICE_PREFIX" =~ ^[a-z][a-z0-9-]{1,23}[a-z0-9]$ ]] || die \
  "SERVICE_PREFIX '${SERVICE_PREFIX_RAW}' is not a valid name component.
   Start with a letter, end alphanumeric, lowercase/digits/hyphens, 3-25 chars."

SERVICE_NAME="${SERVICE_PREFIX}-csuite"
ARTIFACT_REPO="${ARTIFACT_REPO:-${SERVICE_PREFIX}-csuite}"
RUNTIME_SA_NAME="${RUNTIME_SA_NAME:-${SERVICE_PREFIX}-csuite-runtime}"
if [[ ${#RUNTIME_SA_NAME} -gt 30 ]]; then
  RUNTIME_SA_NAME="${SERVICE_PREFIX}-runtime"
  [[ ${#RUNTIME_SA_NAME} -le 30 ]] || die "Set RUNTIME_SA_NAME explicitly in .env."
fi
RUNTIME_SA="${RUNTIME_SA_NAME}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
REGISTRY="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${ARTIFACT_REPO}"

log "Project ......... $GCP_PROJECT_ID"
log "Region .......... $GCP_REGION"
log "Cloud Run ....... $SERVICE_NAME  (public)"
log "Artifact repo ... $ARTIFACT_REPO"
log "Runtime SA ...... $RUNTIME_SA_NAME"
log "ADK backend ..... $ADK_BACKEND"
log "Model ........... $MODEL_NAME"
log "Roles ........... $ACTIVE_ROLES_CSV"
log "Image tag ....... $IMAGE_TAG"
echo

# Refuse to clobber a service this deployment does not own -- notably any
# Option 1 service that happens to share the name.
if gcloud run services describe "$SERVICE_NAME" --project "$GCP_PROJECT_ID" \
     --region "$GCP_REGION" --format='value(metadata.name)' &>/dev/null; then
  OWNER="$(gcloud run services describe "$SERVICE_NAME" --project "$GCP_PROJECT_ID" \
    --region "$GCP_REGION" --format='value(metadata.labels.managed-by)' 2>/dev/null || true)"
  [[ "$OWNER" == "ai-csuite-adk" ]] || die \
    "Cloud Run service '${SERVICE_NAME}' already exists in ${GCP_REGION} and was not
   created by this ADK deployment (managed-by='${OWNER}'). Choose a different
   SERVICE_PREFIX so you do not overwrite it."
  log "Will update existing service ${SERVICE_NAME}."
fi

# --------------------------------------------------------------------------
# 1. APIs
# --------------------------------------------------------------------------
log "Enabling required APIs (idempotent)..."
APIS=(run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com)
[[ "$ADK_BACKEND" == "vertex" ]] && APIS+=(aiplatform.googleapis.com)
[[ "$ADK_BACKEND" == "api_key" ]] && APIS+=(secretmanager.googleapis.com generativelanguage.googleapis.com)
[[ "$DECISION_LOG_BACKEND" == "firestore" ]] && APIS+=(firestore.googleapis.com)
gcloud services enable "${APIS[@]}" --project "$GCP_PROJECT_ID" --quiet

# --------------------------------------------------------------------------
# 2. Artifact Registry
# --------------------------------------------------------------------------
if ! gcloud artifacts repositories describe "$ARTIFACT_REPO" \
      --location "$GCP_REGION" --project "$GCP_PROJECT_ID" &>/dev/null; then
  log "Creating Artifact Registry repo '$ARTIFACT_REPO'..."
  gcloud artifacts repositories create "$ARTIFACT_REPO" \
    --repository-format=docker --location "$GCP_REGION" \
    --description="AI C-Suite ADK images" --project "$GCP_PROJECT_ID" --quiet
fi

# --------------------------------------------------------------------------
# 3. Runtime service account (least privilege)
# --------------------------------------------------------------------------
if ! gcloud iam service-accounts describe "$RUNTIME_SA" --project "$GCP_PROJECT_ID" &>/dev/null; then
  log "Creating runtime service account $RUNTIME_SA_NAME..."
  gcloud iam service-accounts create "$RUNTIME_SA_NAME" \
    --display-name="AI C-Suite ADK runtime" --project "$GCP_PROJECT_ID" --quiet
fi

if [[ "$ADK_BACKEND" == "vertex" ]]; then
  log "Granting Vertex AI access..."
  # --condition=None is required, not optional: a project whose IAM policy
  # already contains any conditional binding rejects an unconditional add in
  # non-interactive mode unless the absence of a condition is explicit.
  gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/aiplatform.user" \
    --condition=None \
    --quiet >/dev/null
else
  gcloud secrets describe "$GEMINI_API_KEY_SECRET" --project "$GCP_PROJECT_ID" &>/dev/null || die \
    "ADK_BACKEND=api_key but secret '$GEMINI_API_KEY_SECRET' does not exist. Create it:

  gcloud secrets create $GEMINI_API_KEY_SECRET --replication-policy=automatic --project=$GCP_PROJECT_ID
  printf '%s' 'YOUR_KEY' | gcloud secrets versions add $GEMINI_API_KEY_SECRET --data-file=- --project=$GCP_PROJECT_ID"
  log "Granting Secret Manager access..."
  gcloud secrets add-iam-policy-binding "$GEMINI_API_KEY_SECRET" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/secretmanager.secretAccessor" \
    --condition=None \
    --project "$GCP_PROJECT_ID" --quiet >/dev/null
fi

if [[ "$DECISION_LOG_BACKEND" == "firestore" ]]; then
  gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/datastore.user" \
    --condition=None \
    --quiet >/dev/null
fi

# --------------------------------------------------------------------------
# 4. Build
# --------------------------------------------------------------------------
IMAGE="${REGISTRY}/csuite:${IMAGE_TAG}"
IMAGE_LATEST="${REGISTRY}/csuite:latest"

log "Building image..."
gcloud builds submit --config deploy/cloudbuild.yaml \
  --substitutions "_IMAGE=${IMAGE},_IMAGE_LATEST=${IMAGE_LATEST}" \
  --project "$GCP_PROJECT_ID" --quiet

# --------------------------------------------------------------------------
# 5. Deploy
# --------------------------------------------------------------------------
log "Deploying $SERVICE_NAME..."

ENV_FILE="$(mktemp)"
trap 'rm -f "$ENV_FILE"' EXIT
cat > "$ENV_FILE" <<YAML
GCP_PROJECT_ID: "${GCP_PROJECT_ID}"
GCP_REGION: "${GCP_REGION}"
MODEL_NAME: "${MODEL_NAME}"
MODEL_TEMPERATURE: "${MODEL_TEMPERATURE:-0.2}"
ADK_BACKEND: "${ADK_BACKEND}"
VERTEX_LOCATION: "${VERTEX_LOCATION}"
GEMINI_API_KEY_SECRET: "${GEMINI_API_KEY_SECRET}"
LOG_LEVEL: "${LOG_LEVEL:-INFO}"
ACTIVE_ROLES_CSV: "${ACTIVE_ROLES_CSV}"
AGENT_TIMEOUT_SECONDS: "${AGENT_TIMEOUT_SECONDS:-180}"
AGENT_MAX_RETRIES: "${AGENT_MAX_RETRIES:-1}"
ROUTING_MODE: "${ROUTING_MODE:-llm}"
ROUTING_MIN_ROLES: "${ROUTING_MIN_ROLES:-1}"
ROUTING_MAX_ROLES: "${ROUTING_MAX_ROLES:-5}"
HITL_ENABLED: "${HITL_ENABLED:-true}"
HITL_PLAN_APPROVAL: "${HITL_PLAN_APPROVAL:-true}"
HITL_CONFIDENCE_FLOOR: "${HITL_CONFIDENCE_FLOOR:-0.75}"
HITL_REQUIRE_CITATIONS: "${HITL_REQUIRE_CITATIONS:-true}"
HITL_FINAL_SIGNOFF: "${HITL_FINAL_SIGNOFF:-true}"
HITL_TIMEOUT_SECONDS: "${HITL_TIMEOUT_SECONDS:-900}"
DECISION_LOG_BACKEND: "${DECISION_LOG_BACKEND}"
FIRESTORE_COLLECTION: "${FIRESTORE_COLLECTION:-adk_csuite_decision_log}"
MAX_UPLOAD_BYTES: "${MAX_UPLOAD_BYTES:-1048576}"
ALLOWED_UPLOAD_EXTENSIONS_CSV: "${ALLOWED_UPLOAD_EXTENSIONS_CSV:-.md,.markdown,.txt,.csv}"
YAML

# Ingress must be "all": there is no separate caller service here, but keeping
# it explicit prevents a project default from silently making the dashboard
# unreachable. max-instances 1 keeps a run's live state on one instance, which
# the SSE dashboard and the blocking human gates both rely on.
SECRET_ARGS=""
if [[ "$ADK_BACKEND" == "api_key" ]]; then
  SECRET_ARGS="--set-secrets=GEMINI_API_KEY=${GEMINI_API_KEY_SECRET}:latest"
fi

gcloud run deploy "$SERVICE_NAME" \
  --image "$IMAGE" \
  --project "$GCP_PROJECT_ID" \
  --region "$GCP_REGION" \
  --service-account "$RUNTIME_SA" \
  --allow-unauthenticated \
  --ingress "${SERVICE_INGRESS:-all}" \
  --memory "${SERVICE_MEMORY:-2Gi}" \
  --cpu "${SERVICE_CPU:-2}" \
  --timeout "${SERVICE_TIMEOUT:-3600}" \
  --min-instances "${SERVICE_MIN_INSTANCES:-1}" \
  --max-instances 1 \
  --labels "managed-by=ai-csuite-adk,csuite-prefix=${SERVICE_PREFIX},csuite-variant=adk" \
  --env-vars-file "$ENV_FILE" \
  ${SECRET_ARGS} \
  --quiet

SERVICE_URL="$(gcloud run services describe "$SERVICE_NAME" \
  --project "$GCP_PROJECT_ID" --region "$GCP_REGION" --format='value(status.url)')"

# --------------------------------------------------------------------------
# 6. Verify -- never report success on a system that cannot run its agents
# --------------------------------------------------------------------------
log "Verifying all ADK agents initialised..."
VERIFY_ATTEMPTS="${VERIFY_ATTEMPTS:-6}"
VERIFY_DELAY_SECONDS="${VERIFY_DELAY_SECONDS:-10}"
HEALTH_JSON=""
ATTEMPT=1
while [[ $ATTEMPT -le $VERIFY_ATTEMPTS ]]; do
  HEALTH_JSON="$(curl -fsS --max-time 60 "${SERVICE_URL}/api/agents/health" 2>/dev/null || true)"
  case "$HEALTH_JSON" in
    *'"all_ok": true'*|*'"all_ok":true'*) break ;;
  esac
  if [[ $ATTEMPT -lt $VERIFY_ATTEMPTS ]]; then
    log "  not ready yet (attempt ${ATTEMPT}/${VERIFY_ATTEMPTS}); waiting ${VERIFY_DELAY_SECONDS}s..."
    sleep "$VERIFY_DELAY_SECONDS"
  fi
  ATTEMPT=$((ATTEMPT + 1))
done

case "$HEALTH_JSON" in
  *'"all_ok": true'*|*'"all_ok":true'*)
    printf '\n\033[1;32mDeployment complete and verified\033[0m\n\n'
    printf '  Dashboard ....... %s\n' "$SERVICE_URL"
    printf '  Agent health .... %s/api/agents/health\n' "$SERVICE_URL"
    printf '  Variant ......... Google ADK, %s agents in one Cloud Run service\n\n' \
      "$(printf '%s' "$ACTIVE_ROLES_CSV" | awk -F, '{print NF}')"
    ;;
  *)
    printf '\n\033[1;33m! Deployed, but the ADK agents are not all usable.\033[0m\n\n'
    printf '%s\n\n' "${HEALTH_JSON:-  (no response from ${SERVICE_URL}/api/agents/health)}"
    printf '  Read model_probe above first -- it carries the exact reason and, when\n'
    printf '  the model is not found, the list of models this project CAN use.\n\n'
    printf '    NOT_FOUND / 404  -> MODEL_NAME "%s" is not served to this project in\n' "$MODEL_NAME"
    printf '        VERTEX_LOCATION "%s". Newer Gemini models are often global-only:\n' "$VERTEX_LOCATION"
    printf '            ./deploy/list_models.sh global\n'
    printf '        then set MODEL_NAME (and VERTEX_LOCATION=global) in .env and redeploy.\n'
    printf '    403 / PermissionDenied -> the runtime service account is missing\n'
    printf '        roles/aiplatform.user, or aiplatform.googleapis.com is not enabled.\n'
    printf '    SKILL.md in the message -> a role in ACTIVE_ROLES_CSV has no\n'
    printf '        agents/<role>/SKILL.md in the image.\n\n'
    printf '  Logs: gcloud run services logs read %s --region %s --limit 50\n\n' \
      "$SERVICE_NAME" "$GCP_REGION"
    exit 1
    ;;
esac
