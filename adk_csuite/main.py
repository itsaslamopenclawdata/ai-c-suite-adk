"""AI C-Suite — Google ADK edition.

One Cloud Run service. The dashboard, the orchestration engine and all five
executive agents live in this process; the executives are ADK LlmAgents driven
by ADK Runners rather than separate services called over HTTP.

That is the whole point of this variant. Option 1 shows distributed agents with
a real network boundary and per-call identity tokens. This shows ADK's
in-process composition: fewer moving parts, no service-to-service auth to get
wrong, one deploy — at the cost of the agents no longer scaling or failing
independently.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from adk_csuite.agents import AgentRegistry
from adk_csuite.config import settings as get_settings
from adk_csuite.decision_log import build_decision_log
from adk_csuite.engine import (
    PlanNotPendingError,
    ReviewNotFoundError,
    RunEngine,
    RunNotFoundError,
)
from adk_csuite.models import (
    DecisionLogEntry,
    HandoffStatus,
    HealthResponse,
    PlanDecision,
    ReviewDecision,
    ReviewItem,
    ReviewStatus,
    Run,
    RunPlan,
    SourceDocument,
)
from adk_csuite.roles import ROLE_REGISTRY, number_lines, sha256_of

settings = get_settings()
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("adk_csuite")

STATIC_DIR = Path(__file__).parent / "static"


def _configure_adk_backend() -> None:
    """Point ADK at Vertex AI or at the Gemini API, per configuration.

    ADK reads these from the environment, so they are set before any agent is
    constructed. The Vertex path uses the Cloud Run service account and needs
    no key at all, which is why it is the default.
    """
    import os

    if settings.adk_backend == "vertex":
        if not settings.gcp_project_id:
            raise RuntimeError(
                "ADK_BACKEND=vertex requires GCP_PROJECT_ID so ADK can reach Vertex AI."
            )
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
        os.environ["GOOGLE_CLOUD_PROJECT"] = settings.gcp_project_id
        os.environ["GOOGLE_CLOUD_LOCATION"] = settings.vertex_location
        logger.info(
            "ADK backend: Vertex AI (project=%s location=%s)",
            settings.gcp_project_id,
            settings.vertex_location,
        )
        return

    from adk_csuite.secrets import SecretResolutionError, resolve_secret

    try:
        key = resolve_secret(
            inline_value=settings.gemini_api_key,
            project_id=settings.gcp_project_id,
            secret_name=settings.gemini_api_key_secret,
            version=settings.gemini_api_key_secret_version,
            label="Gemini API key",
        )
    except SecretResolutionError as exc:
        raise RuntimeError(str(exc)) from exc

    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "FALSE"
    os.environ["GOOGLE_API_KEY"] = key
    logger.info("ADK backend: Gemini API key from Secret Manager")


class _State:
    engine: Optional[RunEngine] = None
    registry: Optional[AgentRegistry] = None
    init_error: str = ""
    # Result of the startup model probe. Building an LlmAgent does not
    # validate the model name, so without this the agents look healthy and
    # then every one of them fails on the first real request.
    probe: Optional[dict] = None


state = _State()


@asynccontextmanager
async def lifespan(_: FastAPI):
    decision_log = build_decision_log(
        backend=settings.decision_log_backend,
        project_id=settings.gcp_project_id,
        collection=settings.firestore_collection,
        database=settings.firestore_database,
    )

    try:
        _configure_adk_backend()
        registry = AgentRegistry(settings)
        registry.build()
        state.registry = registry
    except Exception as exc:  # noqa: BLE001
        # Start anyway so /healthz can report *why* the service is unhealthy.
        # A crash-looping revision is far harder to debug on stage.
        state.init_error = f"{type(exc).__name__}: {exc}"
        logger.error("ADK agents unavailable: %s", exc)

    if state.registry is not None:
        # One tiny generation call. Confirms the model name, the location and
        # the credentials in one go, at deploy time rather than on stage.
        from adk_csuite.model_probe import probe

        result = await asyncio.to_thread(probe, settings)
        state.probe = result.as_dict()
        if not result.ok:
            state.init_error = (
                f"Model '{result.model}' is not reachable: {result.error}"
            )

    state.engine = RunEngine(
        settings=settings, decision_log=decision_log, registry=state.registry
    )
    logger.info(
        "ADK C-Suite ready: roles=%s backend=%s model=%s hitl=%s",
        settings.active_roles,
        settings.adk_backend,
        settings.model_name,
        settings.hitl_enabled,
    )
    yield


app = FastAPI(
    title="AI C-Suite Orchestrator",
    description="Multi-agent executive committee on Google Cloud Run.",
    version="1.0.0",
    lifespan=lifespan,
)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _engine() -> RunEngine:
    if state.engine is None:
        raise HTTPException(status_code=503, detail="Orchestrator is still starting.")
    return state.engine


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="Dashboard asset not found.")
    return FileResponse(index)


@app.get("/summary/{run_id}", include_in_schema=False)
async def summary_page(run_id: str) -> FileResponse:
    """The CEO-facing brief. The dashboard is the how; this is the what."""
    page = STATIC_DIR / "summary.html"
    if not page.is_file():
        raise HTTPException(status_code=404, detail="Summary asset not found.")
    return FileResponse(page)


# --------------------------------------------------------------------------
# Health and configuration
# --------------------------------------------------------------------------


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok", role="orchestrator", model=settings.model_name)


@app.get("/api/config")
async def config() -> dict:
    """Everything the dashboard needs to render itself. No secrets."""
    return {
        "model": settings.model_name,
        "region": settings.gcp_region,
        "variant": "adk",
        "adk_backend": settings.adk_backend,
        "service_auth_mode": "in-process",
        "decision_log_backend": settings.decision_log_backend,
        "synthesis_available": not state.init_error,
        "init_error": state.init_error,
        "routing": {
            "mode": settings.routing_mode,
            "min_roles": settings.routing_min_roles,
            "max_roles": settings.routing_max_roles,
        },
        "hitl": {
            "enabled": settings.hitl_enabled,
            "confidence_floor": settings.hitl_confidence_floor,
            "require_citations": settings.hitl_require_citations,
            "final_signoff": settings.hitl_final_signoff,
            "plan_approval": settings.hitl_plan_approval,
            "timeout_seconds": settings.hitl_timeout_seconds,
        },
        "upload": {
            "max_bytes": settings.max_upload_bytes,
            "allowed_extensions": sorted(settings.allowed_upload_extensions),
        },
        "roles": [
            {
                "key": key,
                "title": spec.title,
                "short_title": spec.short_title,
                "accent": spec.accent,
                "lens": spec.lens,
                "endpoint": f"adk://{key}",
                "configured": state.registry is not None and state.registry.has(key),
            }
            for key, spec in ROLE_REGISTRY.items()
            if key in settings.active_roles
        ],
    }


@app.get("/api/agents/health")
async def agents_health() -> dict:
    """Report whether every configured ADK agent was constructed.

    There is no network hop to probe here -- the agents are in this process.
    What can fail is ADK initialisation: a missing Vertex configuration, an
    unreadable SKILL.md, or a model the project cannot access.
    """
    model_ok = bool(state.probe and state.probe.get("ok"))
    results = []
    for role_key in settings.active_roles:
        spec = ROLE_REGISTRY.get(role_key)
        # An agent is only healthy if it was built AND its model answers.
        # Reporting "ok" on a model that 404s is how this fails on stage.
        ready = (
            state.registry is not None and state.registry.has(role_key) and model_ok
        )
        entry = {
            "role": role_key,
            "ok": ready,
            "url": f"adk://{role_key}",
            "transport": "adk-in-process",
        }
        if state.registry is not None and state.registry.has(role_key) and not model_ok:
            entry["detail"] = (
                "The ADK agent was built, but its model is unreachable. "
                "See model_probe below."
            )
            results.append(entry)
            continue
        if ready:
            entry["detail"] = {
                "status": "ok",
                "role_title": spec.title if spec else role_key,
                "model": settings.model_name,
                "backend": settings.adk_backend,
            }
        else:
            entry["detail"] = state.init_error or (
                f"No ADK agent was built for '{role_key}'. Check ACTIVE_ROLES_CSV "
                f"and that {settings.agents_dir}/{role_key}/SKILL.md exists."
            )
        results.append(entry)

    return {
        "all_ok": bool(results) and all(r["ok"] for r in results),
        "service_auth_mode": "in-process",
        "adk_backend": settings.adk_backend,
        "model_probe": state.probe or {"ok": False, "error": "probe did not run"},
        "agents": results,
    }


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


@app.post("/api/runs", response_model=Run)
async def start_run(
    objective: str = Form(...),
    file: Optional[UploadFile] = File(default=None),
) -> Run:
    """Kick off a C-suite engagement from an objective and an uploaded file."""
    objective = objective.strip()
    if not objective:
        raise HTTPException(status_code=422, detail="An objective is required.")

    document: Optional[SourceDocument] = None
    if file is not None and file.filename:
        document = await _read_upload(file)

    run = await _engine().start_run(objective=objective, document=document)
    return run


async def _read_upload(file: UploadFile) -> SourceDocument:
    suffix = Path(file.filename).suffix.lower()
    allowed = settings.allowed_upload_extensions
    if suffix not in allowed:
        raise HTTPException(
            status_code=415,
            detail=f"'{suffix}' is not an accepted file type. Allowed: {', '.join(sorted(allowed))}.",
        )

    raw = await file.read()
    if len(raw) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {settings.max_upload_bytes} byte limit.",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=415, detail="File must be UTF-8 text.") from exc
    if not text.strip():
        raise HTTPException(status_code=422, detail="The uploaded file is empty.")

    return SourceDocument(
        filename=file.filename,
        content_type=file.content_type or "text/markdown",
        numbered_content=number_lines(text),
        line_count=len(text.splitlines()),
        sha256=sha256_of(text),
    )


@app.get("/api/runs", response_model=List[Run])
async def list_runs() -> List[Run]:
    return _engine().list_runs()


@app.get("/api/runs/{run_id}", response_model=Run)
async def get_run(run_id: str) -> Run:
    try:
        return _engine().get_run(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No run '{run_id}'.") from exc


@app.get("/api/runs/{run_id}/decisions", response_model=List[DecisionLogEntry])
async def get_decisions(run_id: str) -> List[DecisionLogEntry]:
    try:
        return _engine().get_run(run_id).decision_log
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No run '{run_id}'.") from exc


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str, request: Request) -> StreamingResponse:
    """Server-sent events: the live feed the dashboard renders."""
    try:
        engine = _engine()
        run = engine.get_run(run_id)
        queue = engine.subscribe(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No run '{run_id}'.") from exc

    async def event_stream():
        try:
            # Replay current state so a late-joining dashboard is never behind.
            yield _sse("snapshot", run.model_dump(mode="json"))
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(message["event"], message["data"])
        finally:
            engine.unsubscribe(run_id, queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


# --------------------------------------------------------------------------
# Human in the loop
# --------------------------------------------------------------------------


@app.post("/api/runs/{run_id}/plan", response_model=RunPlan)
async def resolve_plan(run_id: str, decision: PlanDecision) -> RunPlan:
    """Approve or amend the routing plan. This unblocks dispatch."""
    try:
        return await _engine().resolve_plan(run_id=run_id, decision=decision)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No run '{run_id}'.") from exc
    except PlanNotPendingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/runs/{run_id}/reviews", response_model=List[ReviewItem])
async def list_reviews(run_id: str) -> List[ReviewItem]:
    try:
        return _engine().get_run(run_id).reviews
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No run '{run_id}'.") from exc


@app.post("/api/runs/{run_id}/reviews/{review_id}", response_model=ReviewItem)
async def resolve_review(
    run_id: str, review_id: str, decision: ReviewDecision
) -> ReviewItem:
    """Approve, edit or reject an escalated finding. This unblocks the run."""
    try:
        return await _engine().resolve_review(
            run_id=run_id, review_id=review_id, decision=decision
        )
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No run '{run_id}'.") from exc
    except ReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No review '{review_id}'.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/runs/{run_id}/summary")
async def get_summary(run_id: str) -> dict:
    """The CEO-facing view of a run.

    Deliberately the Orchestrator's consolidated position -- not five
    independent executive summaries. What the CEO needs is one answer, plus
    honest provenance: who was consulted, what was held back for a human, and
    what the human changed.
    """
    try:
        run = _engine().get_run(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No run '{run_id}'.") from exc

    total_findings = sum(len(f.findings) for f in run.findings)
    escalated = [r for r in run.reviews if r.reason.value != "final_signoff"]
    by_status = {
        status.value: len([r for r in escalated if r.status == status])
        for status in ReviewStatus
    }
    signoff = next(
        (r for r in run.reviews if r.reason.value == "final_signoff"), None
    )

    duration_seconds = None
    if run.completed_at:
        duration_seconds = int((run.completed_at - run.created_at).total_seconds())

    return {
        "run_id": run.id,
        "status": run.status.value,
        "objective": run.objective,
        "document": (
            {
                "filename": run.document.filename,
                "line_count": run.document.line_count,
                "sha256": run.document.sha256,
            }
            if run.document
            else None
        ),
        "created_at": run.created_at,
        "completed_at": run.completed_at,
        "duration_seconds": duration_seconds,
        "plan": (
            {
                "interpretation": run.plan.interpretation,
                "strategy": run.plan.strategy,
                "approved_by_human": run.plan.approved_by_human,
                "amended_by_human": run.plan.amended_by_human,
                "reviewer_note": run.plan.reviewer_note,
                "engaged": [r.model_dump(mode="json") for r in run.plan.engaged],
                "skipped": [r.model_dump(mode="json") for r in run.plan.skipped],
            }
            if run.plan
            else None
        ),
        "synthesis": run.synthesis.model_dump(mode="json") if run.synthesis else None,
        "provenance": {
            "executives_engaged": len(
                [h for h in run.handoffs if h.status == HandoffStatus.SUCCEEDED]
            ),
            "executives_available": len(settings.active_roles),
            "findings_produced": total_findings,
            "findings_auto_accepted": total_findings - len(escalated),
            "findings_escalated": len(escalated),
            "human_approved": by_status.get("approved", 0),
            "human_edited": by_status.get("edited", 0),
            "human_rejected": by_status.get("rejected", 0),
            "timed_out": by_status.get("timed_out", 0),
            "final_signoff": signoff.status.value if signoff else "not_required",
            "signoff_note": signoff.reviewer_note if signoff else "",
        },
        # Every point at which a human changed the outcome. This is the
        # anti-hallucination story told as evidence rather than assertion.
        "human_interventions": [
            {
                "role_title": r.role_title,
                "action": r.status.value,
                "reason": r.reason.value,
                "headline": r.original_headline,
                "note": r.reviewer_note,
                "at": r.resolved_at,
            }
            for r in run.reviews
            if r.status
            in {ReviewStatus.EDITED, ReviewStatus.REJECTED, ReviewStatus.APPROVED}
            and r.resolved_at is not None
        ],
        "model": settings.model_name,
    }


@app.get("/api/runs/{run_id}/source")
async def get_source(run_id: str) -> dict:
    """The numbered source document, so a reviewer can verify a citation."""
    try:
        run = _engine().get_run(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No run '{run_id}'.") from exc
    if run.document is None:
        return {"filename": None, "lines": []}
    return {
        "filename": run.document.filename,
        "sha256": run.document.sha256,
        "line_count": run.document.line_count,
        "lines": run.document.numbered_content.splitlines(),
    }
