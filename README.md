# AI C-Suite — Google ADK edition (Option 2)

The same demo as Option 1, built a different way. Upload a document, give an
objective, and a committee of five executive AI agents — CFO, CSO, CMO, CHRO,
CTO — works the problem. An Orchestrator decides which of them the task needs,
a human approves that plan, the agents run, and anything they cannot prove is
held for a human before it reaches the CEO.

The difference is underneath: **every agent here is a
`google.adk.agents.LlmAgent`**, composed in-process by ADK Runners.

---

## The architectural contrast

| | Option 1 (`Ai-C-Suite-GDG-Sydney`) | Option 2 (this folder) |
|---|---|---|
| Agents | 5 independent Cloud Run services | 5 ADK `LlmAgent`s in one process |
| Transport | HTTPS + per-call OIDC identity tokens | ADK Runner, in-process |
| Cloud Run services | 6 | 1 |
| Model access | Gemini API key from Secret Manager | Vertex AI via the service account — **no key exists** |
| Scaling | Each executive scales and fails independently | One process; one agent's load is everyone's |
| Failure blast radius | One agent down = that domain unassessed | Process down = everything down |
| What can go wrong | Ingress, IAM, token audience, cold starts ×6 | ADK init, model access, one cold start |
| Deploy time | ~10 min first run | ~5 min first run |

Both give identical guarantees — line-level citations, confidence floors,
routing, the plan gate, the escalation queue, the decision log, the CEO brief.
That is deliberate: it makes the comparison about *architecture*, not features.

**The honest summary for the stage:** ADK removes an entire category of
distributed-systems failure. It also removes independent scaling and fault
isolation. For five agents that always run together on one request, ADK is the
better trade. For agents owned by different teams with different scaling
profiles, Option 1's shape is the better trade.

```
        markdown + objective
                 │
                 ▼
  ┌──────────────────────────────────────────────┐
  │  adk-csuite   (one Cloud Run service)        │
  │                                              │
  │  Dashboard · SSE · CEO brief                 │
  │  Orchestration engine                        │
  │                                              │
  │  ADK Runner ──► router      (LlmAgent)       │
  │  ADK Runner ──► CFO CSO CMO CHRO CTO         │
  │                 (LlmAgent + tools)           │
  │  ADK Runner ──► synthesiser (LlmAgent)       │
  └──────────────────────┬───────────────────────┘
                         ▼
                   Vertex AI (Gemini)
```

---

## What ADK actually buys here

- **`LlmAgent` with `output_schema` + `tools` together.** ADK 2.x runs tools
  during the reasoning loop and enforces the schema only on the final answer.
  So an executive can *look up* the source lines it intends to cite, and still
  be forced to return a structured finding carrying those citations and a
  confidence score.
- **Tool calling as evidence.** Each executive gets `document_outline` and
  `lookup_source_lines`. The dashboard shows a tool-call count per agent —
  visible proof it read the document rather than recalled it.
- **Runners and sessions.** One Runner per agent, an ephemeral session per
  invocation, deleted afterwards. No cross-run state.
- **No API key anywhere.** ADK talks to Vertex AI as the Cloud Run service
  account. There is no secret to leak, rotate, or forget.

---

## Deploy

```bash
cp .env.example .env          # set GCP_PROJECT_ID, GCP_REGION
./deploy/list_models.sh       # confirm MODEL_NAME is available to your project
./deploy/deploy.sh
```

**Check the model first.** Model availability varies by project *and* by
location, and a name that works in one is routinely absent from another.
`list_models.sh` prints what your project can actually call and tells you
whether your `MODEL_NAME` is among them. Newer Gemini models are frequently
served only from Vertex's global endpoint:

```bash
./deploy/list_models.sh global   # then set VERTEX_LOCATION="global" in .env
```

Building an ADK `LlmAgent` does **not** validate the model name — only the
first request does. So the service makes one tiny generation call at startup
and `/api/agents/health` reports every agent unhealthy if it fails, carrying
the reason and the list of models the project can use. `deploy.sh` keys off
that, so a bad model name stops the deploy instead of surfacing mid-demo.

No Secret Manager step: the default `ADK_BACKEND=vertex` authenticates as the
runtime service account. Set `ADK_BACKEND=api_key` only if your project cannot
use Vertex, and the key is read from Secret Manager at runtime.

`deploy.sh` enables the APIs, creates a least-privilege runtime service
account, grants `roles/aiplatform.user`, builds the image, deploys one public
Cloud Run service, then **verifies all five ADK agents initialised** before
reporting success. If they did not, it prints a diagnosis and exits non-zero.

### Nothing here can touch Option 1

Everything is namespaced by `SERVICE_PREFIX` (default `adk`):

| Resource | Name |
|---|---|
| Cloud Run | `adk-csuite` |
| Artifact Registry | `adk-csuite` |
| Service account | `adk-csuite-runtime` |
| Firestore collection | `adk_csuite_decision_log` |

Services are labelled `managed-by=ai-csuite-adk`. `deploy.sh` refuses to
overwrite a service without that label, and `teardown.sh` only deletes one
carrying it — so neither can reach an Option 1 `tech-*` service. The dry run
asserts this explicitly.

```bash
./tests/deploy_dryrun.sh      # rehearse the deploy, no cloud access
./deploy/teardown.sh          # remove just this service
```

## Run locally

```bash
cp .env.example .env
gcloud auth application-default login
pip install -r requirements.txt
./deploy/run_local.sh         # http://127.0.0.1:8081
```

Port 8081 by default, so it runs alongside Option 1 on 8080 — handy for
showing both dashboards side by side.

---

## Tests

```bash
python tests/e2e_smoke.py       # full run against a stubbed ADK registry
python tests/model_probe_test.py # health must fail when the model is unreachable
./tests/deploy_dryrun.sh        # deploy.sh against a mocked gcloud
```

Neither needs Vertex access, an API key, or credentials. 66 assertions cover
routing, the plan gate, ADK dispatch, all three escalation triggers, the human
gates, the decision log and the CEO brief.

## Demo runbook

Identical to Option 1 — see its README. Two things to say differently:

- At the pipeline: *"these five agents are ADK LlmAgents in one process, not
  five services. Watch the tool-call column — that's each executive reading the
  source document before it cites it."*
- At the close: *"same guarantees, one deployable. That's the trade."*

## Notes and limits

- Run state is in memory, so the service deploys with `--max-instances 1`.
  The SSE dashboard and the blocking human gates depend on it.
- Five agents in one process needs real memory; the default is 2Gi / 2 CPU.
- Sequential dispatch means latency is the sum of five model calls. ADK's
  `ParallelAgent` would cut that, but the demo's value is watching the baton
  move, so this stays sequential on purpose.
- There is no authentication on the service. Add IAP or Cloud Run IAM before
  this goes near real data.
