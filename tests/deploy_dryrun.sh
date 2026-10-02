#!/usr/bin/env bash
#
# Dry-run deploy.sh against a mocked gcloud.
#
#   ./tests/deploy_dryrun.sh
#
# Executes every line of the real deploy script -- no Google Cloud access, no
# cost, no side effects -- and asserts the resource names and the agent URL
# map it produces. Catches unbound variables, array bugs and quoting mistakes
# that only surface partway through a real deploy.
#
# Run this on the machine you will deploy from: it exercises deploy.sh under
# *your* bash, which is the version that matters (macOS ships bash 3.2).
#
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

PASS="\033[1;32m✓\033[0m"; FAILM="\033[1;31m✗\033[0m"
FAILURES=0
check() {
  if [[ "$2" == "$3" ]]; then
    printf "  ${PASS} %s\n" "$1"
  else
    printf "  ${FAILM} %s\n      expected: %s\n      actual:   %s\n" "$1" "$3" "$2"
    FAILURES=$((FAILURES + 1))
  fi
}
contains() {
  if grep -qF "$2" "$3"; then printf "  ${PASS} %s\n" "$1"
  else printf "  ${FAILM} %s (missing: %s)\n" "$1" "$2"; FAILURES=$((FAILURES + 1)); fi
}

printf '\nbash %s\n' "${BASH_VERSION}"

# --------------------------------------------------------------------------
# Preflight: portability lint + Cloud Build config validation.
#
# Deliberately a Python file rather than an inline heredoc. bash 3.2 (macOS)
# cannot parse a heredoc inside $(...), and BSD grep has no -P -- the previous
# inline version silently reported success on macOS because of it.
# --------------------------------------------------------------------------
if command -v python3 >/dev/null; then
  if ! python3 "$REPO_ROOT/tests/preflight_lint.py" "$REPO_ROOT"; then
    FAILURES=$((FAILURES + 1))
  fi
else
  printf "  ${FAILM} python3 not found; skipped preflight lint\n"
  FAILURES=$((FAILURES + 1))
fi

# --------------------------------------------------------------------------
# Mock gcloud: records every invocation, returns plausible output.
# --------------------------------------------------------------------------
mkdir -p "$SANDBOX/bin"
cat > "$SANDBOX/bin/gcloud" <<'MOCK'
#!/usr/bin/env bash
echo "$*" >> "${MOCK_LOG}"

# deploy.sh removes its temp env file on exit, so snapshot it here instead.
PREV=""
for ARG in "$@"; do
  if [[ "$PREV" == "--env-vars-file" && -f "$ARG" ]]; then cp "$ARG" "${ENV_CAPTURE}"; fi
  PREV="$ARG"
done

case "$1 ${2:-} ${3:-}" in
  "config get-value project")   echo "mock-project" ;;
  "run services describe")
      # New service: no managed-by label, but a URL once "deployed".
      if [[ "$*" == *"metadata.labels.managed-by"* ]]; then
        exit 0
      elif [[ "$*" == *"status.url"* ]]; then
        echo "https://${4}-abc123-uc.a.run.app"
      else
        exit 1   # does not exist yet
      fi ;;
  "secrets describe "*)         exit 0 ;;
  "artifacts repositories describe") exit 1 ;;   # force the create path
  "iam service-accounts describe")   exit 1 ;;   # force the create path
  *) : ;;
esac
exit 0
MOCK
chmod +x "$SANDBOX/bin/gcloud"

# Mock curl so the post-deploy verification step can be exercised both ways.
# HEALTH_FIXTURE selects what the orchestrator "reports".
cat > "$SANDBOX/bin/curl" <<'MOCK'
#!/usr/bin/env bash
if [[ "${HEALTH_FIXTURE:-healthy}" == "healthy" ]]; then
  echo '{"all_ok": true, "agents": []}'
  exit 0
fi
echo '{"all_ok": false, "agents": [{"role":"cfo","ok":false,"http_status":404,"detail":"Ingress restriction, not IAM."}]}'
exit 0
MOCK
chmod +x "$SANDBOX/bin/curl"

# --------------------------------------------------------------------------
# Case 1: SERVICE_PREFIX="Tech" -- capitalised, as a user would naturally type
# --------------------------------------------------------------------------
run_deploy() {
  local prefix="$1"
  cp -R "$REPO_ROOT/" "$SANDBOX/repo" 2>/dev/null || {
    rm -rf "$SANDBOX/repo"; mkdir -p "$SANDBOX/repo"
    ( cd "$REPO_ROOT" && tar cf - --exclude=.git . ) | ( cd "$SANDBOX/repo" && tar xf - )
  }
  cat > "$SANDBOX/repo/.env" <<ENV
GCP_PROJECT_ID="mock-project"
GCP_REGION="us-central1"
SERVICE_PREFIX="${prefix}"
MODEL_NAME="gemini-2.5-flash"
ADK_BACKEND="vertex"
VERTEX_LOCATION="us-central1"
ACTIVE_ROLES_CSV="cfo,cso,cmo,chro,cto"
DECISION_LOG_BACKEND="memory"
ENV
  : > "$SANDBOX/gcloud.log"
  rm -f "$SANDBOX/orchestrator_env.yaml"
  MOCK_LOG="$SANDBOX/gcloud.log" \
  ENV_CAPTURE="$SANDBOX/orchestrator_env.yaml" \
  HEALTH_FIXTURE="${2:-healthy}" \
  VERIFY_ATTEMPTS=1 VERIFY_DELAY_SECONDS=0 \
  PATH="$SANDBOX/bin:$PATH" \
    bash "$SANDBOX/repo/deploy/deploy.sh" > "$SANDBOX/out.txt" 2>&1
  echo $?
}

printf '\n-- deploy.sh with SERVICE_PREFIX="ADK" --\n'
EXIT_CODE="$(run_deploy "ADK")"
check "script exits 0" "$EXIT_CODE" "0"
if [[ "$EXIT_CODE" != "0" ]]; then
  printf '\n--- output ---\n'; cat "$SANDBOX/out.txt"; exit 1
fi
contains "normalises ADK -> adk" "Normalised SERVICE_PREFIX 'ADK' -> 'adk'" "$SANDBOX/out.txt"

printf '\n-- Cloud Run service --\n'
contains "deploys adk-csuite"        "run deploy adk-csuite " "$SANDBOX/gcloud.log"
contains "artifact repo adk-csuite"  "repositories create adk-csuite " "$SANDBOX/gcloud.log"
contains "service account adk-csuite-runtime" \
         "service-accounts create adk-csuite-runtime " "$SANDBOX/gcloud.log"

# The whole point of the adk prefix: nothing here may touch Option 1.
printf '\n-- No collision with Option 1 --\n'
if grep -qE "run deploy tech-|repositories create tech-|service-accounts create tech-" "$SANDBOX/gcloud.log"; then
  printf "  ${FAILM} touches a tech-prefixed (Option 1) resource\n"; FAILURES=$((FAILURES+1))
else
  printf "  ${PASS} no tech-prefixed resource touched\n"
fi
if grep -qE "run (services )?delete" "$SANDBOX/gcloud.log"; then
  printf "  ${FAILM} deploy.sh deletes a Cloud Run service\n"; FAILURES=$((FAILURES+1))
else
  printf "  ${PASS} deploy.sh never deletes a service\n"
fi
SERVICES=$(grep -c "run deploy " "$SANDBOX/gcloud.log" || true)
check "deploys exactly one service" "$SERVICES" "1"

printf '\n-- Security posture --\n'
if grep -q "run deploy adk-csuite .*--ingress all" "$SANDBOX/gcloud.log"; then
  printf "  ${PASS} ingress set explicitly to all\n"
else
  printf "  ${FAILM} ingress not set explicitly\n"; FAILURES=$((FAILURES+1))
fi
contains "labelled managed-by=ai-csuite-adk" "managed-by=ai-csuite-adk" "$SANDBOX/gcloud.log"
contains "grants Vertex AI to the runtime SA" "roles/aiplatform.user" "$SANDBOX/gcloud.log"

# Every IAM binding must declare --condition. Projects whose policy already
# contains a conditional binding reject an unconditional add in non-interactive
# mode unless the absence of a condition is explicit.
IAM_CALLS=$(grep -c "add-iam-policy-binding" "$SANDBOX/gcloud.log" || true)
IAM_WITH_COND=$(grep -c "add-iam-policy-binding.*--condition=None" "$SANDBOX/gcloud.log" || true)
check "every IAM binding passes --condition" "$IAM_WITH_COND" "$IAM_CALLS"
if grep -qE "AIza|GEMINI_API_KEY=[A-Za-z0-9_-]{20,}" "$SANDBOX/gcloud.log"; then
  printf "  ${FAILM} an API key value reached a gcloud command line\n"; FAILURES=$((FAILURES+1))
else
  printf "  ${PASS} no key value on any command line\n"
fi
if grep -q "set-secrets" "$SANDBOX/gcloud.log"; then
  printf "  ${FAILM} vertex backend should need no secret\n"; FAILURES=$((FAILURES+1))
else
  printf "  ${PASS} vertex backend uses no secret at all\n"
fi

printf '\n-- Runtime configuration --\n'
if [[ -f "$SANDBOX/orchestrator_env.yaml" ]]; then
  contains "ADK backend passed through" 'ADK_BACKEND: "vertex"' "$SANDBOX/orchestrator_env.yaml"
  contains "all five roles active" 'ACTIVE_ROLES_CSV: "cfo,cso,cmo,chro,cto"' "$SANDBOX/orchestrator_env.yaml"
  contains "plan gate enabled" 'HITL_PLAN_APPROVAL: "true"' "$SANDBOX/orchestrator_env.yaml"
  if grep -qE "^GEMINI_API_KEY: " "$SANDBOX/orchestrator_env.yaml"; then
    printf "  ${FAILM} secret written into the env file\n"; FAILURES=$((FAILURES+1))
  else
    printf "  ${PASS} no secret in the env file\n"
  fi
else
  printf "  ${FAILM} env file was never written\n"; FAILURES=$((FAILURES+1))
fi

printf '\n-- Post-deploy verification --\n'
contains "reports success when agents initialise" "Deployment complete and verified" "$SANDBOX/out.txt"
EXIT_CODE="$(run_deploy "ADK" "unhealthy")"
check "exits non-zero when agents fail to initialise" "$EXIT_CODE" "1"
contains "does not claim success"     "are not all usable"      "$SANDBOX/out.txt"
contains "points at the model probe"  "Read model_probe above"  "$SANDBOX/out.txt"
contains "explains the Vertex 403"    "roles/aiplatform.user"   "$SANDBOX/out.txt"
contains "explains a bad model name"  "is not served to this project" "$SANDBOX/out.txt"
contains "suggests the global endpoint" "list_models.sh global" "$SANDBOX/out.txt"

printf '\n-- Invalid prefixes rejected before any API call --\n'
for BAD in "9adk" "adk_x" "adk-" "ad"; do
  EXIT_CODE="$(run_deploy "$BAD")"
  CREATED=$(grep -cE "run deploy|repositories create|service-accounts create" "$SANDBOX/gcloud.log" || true)
  if [[ "$EXIT_CODE" != "0" && "$CREATED" == "0" ]]; then
    printf "  ${PASS} '%s' rejected, nothing created\n" "$BAD"
  else
    printf "  ${FAILM} '%s' exit=%s created=%s\n" "$BAD" "$EXIT_CODE" "$CREATED"
    FAILURES=$((FAILURES + 1))
  fi
done

printf '\n'
if [[ $FAILURES -gt 0 ]]; then
  printf '\033[1;31m%d check(s) failed.\033[0m\n\n' "$FAILURES"; exit 1
fi
printf '\033[1;32mAll checks passed.\033[0m\n\n'
