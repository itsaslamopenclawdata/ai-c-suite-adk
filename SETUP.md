# AI C-Suite (ADK edition) — Setup & Runbook

Study copy of [`anupamphoghat/AI-C-Suite-adk`](https://github.com/anupamphoghat/AI-C-Suite-adk).
One Cloud Run service runs a committee of five executive agents — **CFO, CSO,
CMO, CHRO, CTO** — built as Google ADK `LlmAgent`s, plus a router and a
synthesiser, all composed in-process. Upload a document + objective → the
router picks which executives are needed → a human approves the plan → the
agents work with line-level citations → anything unproven is escalated to a
human → the synthesiser writes the CEO brief. Full architecture notes in
[README.md](README.md).

---

## 1. What you need (prerequisites)

| Requirement | Why | Check |
|---|---|---|
| Python 3.10–3.12 | ADK 2.x runtime | `python --version` |
| bash | all deploy/run scripts are bash | git-bash works on Windows |
| Google Cloud project | model access + Cloud Run | `gcloud config get-value project` |
| `gcloud` CLI | deploy + local ADC auth | `gcloud --version` |
| Billing enabled on the project | Vertex AI / Cloud Run are billable | console → Billing |

For a **cloud deploy** you also need `roles/run.admin`, `roles/iam.serviceAccountAdmin`
(or equivalent) on the project.

---

## 2. Establish (one-time setup)

```bash
git clone https://github.com/itsaslamopenclawdata/ai-c-suite-adk.git
cd ai-c-suite-adk

cp .env.example .env          # then edit: set GCP_PROJECT_ID, GCP_REGION, MODEL_NAME
pip install -r requirements.txt   # or: uv pip install -r requirements.txt
```

**Pick a model your project can actually call** (availability varies by project
*and* location):

```bash
./deploy/list_models.sh            # regional endpoint
./deploy/list_models.sh global     # global endpoint (newer Gemini models)
```

If your model only appears under `global`, set `VERTEX_LOCATION=global` in `.env`.

---

## 3. Execute — three levels

### Level 0: tests only (no cloud, no credentials — do this first)

```bash
python tests/e2e_smoke.py         # 66 assertions: routing, plan gate, escalations, CEO brief
python tests/model_probe_test.py  # health must fail loudly when the model is unreachable
./tests/deploy_dryrun.sh          # deploy.sh rehearsed against a mocked gcloud
```

### Level 1: run locally (needs model access)

```bash
# Vertex path — authenticate once:
gcloud auth application-default login

# OR api_key path — put a key in .env (ADK_BACKEND=api_key, GEMINI_API_KEY=...)

./deploy/run_local.sh             # dashboard at http://127.0.0.1:8081
```

Health check: `curl http://127.0.0.1:8081/api/agents/health` — every agent
makes one tiny generation call at startup, so this reports real model access.

### Level 2: deploy to Cloud Run

```bash
./deploy/deploy.sh      # APIs → Artifact Registry → least-privilege SA →
                        # build → deploy → verify all 5 agents initialised
./deploy/teardown.sh    # remove only this deployment (label-guarded)
```

`deploy.sh` exits non-zero unless `/api/agents/health` reports all agents OK,
and prints a diagnosis (wrong model name / missing IAM / missing SKILL.md).

---

## 4. Demo flow (what to click)

1. Open the dashboard URL (local `127.0.0.1:8081` or the Cloud Run URL).
2. Upload a file from `demo_files/` (e.g. `q3_enterprise_pipeline.csv`) and
   enter an objective (e.g. *"Should we double down on enterprise?"*).
3. The **router** proposes which executives are needed — **you approve or edit
   the plan** (human gate #1).
4. Watch each executive run: the tool-call count proves it read the source
   lines it cites.
5. Findings below the confidence floor (`0.75`) or missing citations are
   **escalated to you** (human gate #2).
6. Final **sign-off** (human gate #3) → synthesiser → CEO brief, and the run is
   written to the decision log.

---

## 5. Way forward (suggested path)

1. **Verify locally first** — Level 0 tests, then Level 1 against Vertex or an
   API key. Do not deploy before `/api/agents/health` is green locally.
2. **Deploy to a dev project** with `SERVICE_PREFIX=adkdev`, confirm the
   verified deploy, and run the full demo with one `demo_files/` document.
3. **Harden before any real data**: the service is publicly reachable and has
   **no auth** — put IAP or Cloud Run IAM in front; switch
   `DECISION_LOG_BACKEND=firestore` for durable audit; consider
   `ROUTING_MODE=all` to skip the LLM router for deterministic demos.
4. **Learn the ADK internals** (`adk_csuite/agents.py`, `engine.py`): this repo
   is a compact reference for ADK Runner/session lifecycle, tool-calling as
   evidence, `output_schema` enforcement, and HITL gating — the patterns map
   directly onto Aslam's agentic-AI study track.
5. **Extend**: add an executive by dropping `agents/<role>/SKILL.md` and adding
   the role to `ACTIVE_ROLES_CSV`; add a tool in `adk_csuite/tools.py`; the
   deploy scripts pick both up with no other changes.
6. **Compare architectures**: run this next to the Option 1 variant
   (five independent Cloud Run services, `Ai-C-Suite-GDG-Sydney`) to see the
   in-process-ADK vs distributed-services trade live.

---

## 6. Cost & ops notes

- Cloud Run: `--max-instances 1`, 2 CPU / 2 GiB, min-instances 1 — small but
  not free; teardown when done.
- Vertex Gemini usage is per-token; the demo's five sequential agent calls per
  run are modest but not zero.
- Run state is **in-memory** — a redeploy or restart loses in-flight runs and
  (with `memory` backend) the decision log. Use Firestore for anything that
  must survive.
