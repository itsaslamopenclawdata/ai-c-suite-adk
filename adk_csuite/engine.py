"""The Orchestrator engine (ADK edition).

Responsibilities:
  1. Accept a CEO objective plus an uploaded document.
  2. Decide which executives the objective actually needs, and let a human
     approve or amend that plan before anything is called.
  3. Run each engaged executive as an **ADK agent**, in sequence, passing each
     one the findings of those before it.
  4. Score every returned finding against the trust policy and escalate the
     ones that fail to a human.
  5. Block synthesis until every escalation is resolved.
  6. Block completion until a human signs the synthesis off.

The only architectural difference from Option 1 is step 3: there, each
executive is a separate Cloud Run service reached over authenticated HTTP;
here they are ADK LlmAgents composed in-process by ADK Runners. Every other
guarantee -- citations, confidence floors, the human gates, the decision log --
is identical, which is what makes the two directly comparable.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from pydantic import BaseModel, Field

from adk_csuite import tools as source_tools
from adk_csuite.agents import (
    AgentError,
    AgentRegistry,
    ExecutiveOut,
    PlanOut,
    SynthesisOut,
)
from adk_csuite.config import Settings
from adk_csuite.decision_log import DecisionLogBackend, make_entry
from adk_csuite.models import (
    AgentFinding,
    Citation,
    DecisionKind,
    EscalationReason,
    Finding,
    Handoff,
    HandoffStatus,
    PlanDecision,
    ReviewDecision,
    ReviewItem,
    ReviewStatus,
    RoleRouting,
    Run,
    RunPlan,
    RunStatus,
    SourceDocument,
    Synthesis,
)
from adk_csuite.roles import get_role

logger = logging.getLogger("adk_csuite.engine")


class RunNotFoundError(KeyError):
    pass


class ReviewNotFoundError(KeyError):
    pass


class PlanNotPendingError(RuntimeError):
    pass


# The routing and synthesis schemas live with the ADK agents that enforce
# them (adk_csuite/agents.py). Aliased here so the engine reads naturally.
_PlanOutput = PlanOut
_SynthesisOutput = SynthesisOut


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


class RunEngine:
    def __init__(
        self,
        *,
        settings: Settings,
        decision_log: DecisionLogBackend,
        registry: Optional[AgentRegistry],
    ) -> None:
        self._settings = settings
        self._log = decision_log
        self._registry = registry
        self._runs: Dict[str, Run] = {}
        self._sequences: Dict[str, int] = {}
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}
        self._review_signals: Dict[str, asyncio.Event] = {}
        self._plan_signals: Dict[str, asyncio.Event] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    # ---------------- public API ----------------

    def get_run(self, run_id: str) -> Run:
        run = self._runs.get(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        return run

    def list_runs(self) -> List[Run]:
        return sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)

    async def start_run(self, *, objective: str, document: Optional[SourceDocument]) -> Run:
        run = Run(objective=objective.strip(), document=document)
        self._runs[run.id] = run
        self._sequences[run.id] = 0
        self._subscribers[run.id] = set()
        self._review_signals[run.id] = asyncio.Event()
        self._plan_signals[run.id] = asyncio.Event()

        await self._record(
            run,
            DecisionKind.RUN_STARTED,
            actor="orchestrator",
            summary=f"Run started for objective: {objective.strip()[:120]}",
            detail={
                "objective": run.objective,
                "document": document.filename if document else None,
                "document_lines": document.line_count if document else 0,
                "document_sha256": document.sha256 if document else "",
                "available_roles": self._settings.active_roles,
                "routing_mode": self._settings.routing_mode,
                "hitl_enabled": self._settings.hitl_enabled,
                "hitl_plan_approval": self._settings.hitl_plan_approval,
                "confidence_floor": self._settings.hitl_confidence_floor,
            },
        )

        self._tasks[run.id] = asyncio.create_task(self._execute(run.id))
        return run

    def subscribe(self, run_id: str) -> asyncio.Queue:
        if run_id not in self._runs:
            raise RunNotFoundError(run_id)
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._subscribers.setdefault(run_id, set()).add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(run_id)
        if subs:
            subs.discard(queue)

    async def resolve_review(
        self, *, run_id: str, review_id: str, decision: ReviewDecision
    ) -> ReviewItem:
        run = self.get_run(run_id)
        item = next((r for r in run.reviews if r.id == review_id), None)
        if item is None:
            raise ReviewNotFoundError(review_id)
        if item.status != ReviewStatus.PENDING:
            return item

        action = decision.action.strip().lower()
        item.reviewer_note = decision.reviewer_note.strip()
        item.resolved_at = datetime.now(timezone.utc)

        if action == "approve":
            item.status = ReviewStatus.APPROVED
            kind, verb = DecisionKind.HUMAN_APPROVED, "approved"
        elif action == "edit":
            item.status = ReviewStatus.EDITED
            item.edited_detail = decision.edited_detail.strip()
            kind, verb = DecisionKind.HUMAN_EDITED, "edited"
            self._apply_edit(run, item)
        elif action == "reject":
            item.status = ReviewStatus.REJECTED
            kind, verb = DecisionKind.HUMAN_REJECTED, "rejected"
        else:
            raise ValueError(f"Unknown review action '{decision.action}'.")

        await self._record(
            run,
            kind,
            actor="human",
            summary=(
                f"Human {verb} {item.role_title} finding: {item.original_headline[:90]}"
                if item.reason is not EscalationReason.FINAL_SIGNOFF
                else f"Human {verb} the final synthesis"
            ),
            detail={
                "review_id": item.id,
                "role": item.role,
                "reason": item.reason.value,
                "confidence": item.confidence,
                "reviewer_note": item.reviewer_note,
                "edited_detail": item.edited_detail,
            },
        )
        await self._emit(run, "review_resolved", item.model_dump(mode="json"))

        if not run.pending_reviews:
            self._review_signals[run_id].set()
        return item

    # ---------------- execution ----------------

    async def resolve_plan(self, *, run_id: str, decision: PlanDecision) -> RunPlan:
        """Approve or amend the routing plan. This unblocks dispatch."""
        run = self.get_run(run_id)
        if run.plan is None or run.status != RunStatus.AWAITING_PLAN_APPROVAL:
            raise PlanNotPendingError(
                f"Run '{run_id}' is not waiting for plan approval (status: {run.status.value})."
            )

        plan = run.plan
        plan.reviewer_note = decision.reviewer_note.strip()
        action = decision.action.strip().lower()

        if action == "amend":
            requested = [r.strip().lower() for r in decision.roles if r.strip()]
            available = self._settings.active_roles
            unknown = [r for r in requested if r not in available]
            if unknown:
                raise ValueError(
                    f"Unknown role(s): {', '.join(unknown)}. "
                    f"Available: {', '.join(available)}."
                )
            if not requested:
                raise ValueError("An amended plan must engage at least one executive.")

            original = {r.role: r.selected for r in plan.routing}
            for entry in plan.routing:
                now_selected = entry.role in requested
                if now_selected != original[entry.role]:
                    entry.amended_by_human = True
                    entry.reason = (
                        f"Added by the CEO. (Orchestrator had said: {entry.reason})"
                        if now_selected
                        else f"Removed by the CEO. (Orchestrator had said: {entry.reason})"
                    )
                entry.selected = now_selected
                entry.sequence = requested.index(entry.role) + 1 if now_selected else 0

            plan.amended_by_human = True
            plan.approved_by_human = True
            kind, summary = (
                DecisionKind.PLAN_AMENDED,
                f"CEO amended the plan; engaging {', '.join(r.upper() for r in requested)}",
            )
        elif action == "approve":
            plan.approved_by_human = True
            kind, summary = (
                DecisionKind.PLAN_APPROVED,
                "CEO approved the Orchestrator's plan: "
                + ", ".join(r.short_title or r.role.upper() for r in plan.engaged),
            )
        else:
            raise ValueError(f"Unknown plan action '{decision.action}'. Use approve or amend.")

        await self._record(
            run,
            kind,
            actor="human",
            summary=summary,
            detail={
                "engaged": plan.engaged_roles,
                "skipped": [r.role for r in plan.skipped],
                "amended": plan.amended_by_human,
                "reviewer_note": plan.reviewer_note,
            },
        )
        await self._emit(run, "plan", plan.model_dump(mode="json"))
        self._plan_signals[run_id].set()
        return plan

    async def _execute(self, run_id: str) -> None:
        run = self._runs[run_id]
        try:
            run.plan = await self._build_plan(run)
            await self._record(
                run,
                DecisionKind.PLAN_PRODUCED,
                actor="orchestrator",
                summary=(
                    "Plan: engage "
                    + ", ".join(r.short_title or r.role.upper() for r in run.plan.engaged)
                    + (
                        "; not engaging "
                        + ", ".join(
                            r.short_title or r.role.upper() for r in run.plan.skipped
                        )
                        if run.plan.skipped
                        else ""
                    )
                ),
                detail={
                    "interpretation": run.plan.interpretation,
                    "strategy": run.plan.strategy,
                    "routing": [r.model_dump(mode="json") for r in run.plan.routing],
                    "model": run.plan.model_used,
                },
            )
            await self._emit(run, "plan", run.plan.model_dump(mode="json"))

            if self._settings.hitl_plan_approval:
                run.status = RunStatus.AWAITING_PLAN_APPROVAL
                await self._emit(run, "run_status", {"status": run.status.value})
                await self._await_plan(run)

            await self._dispatch_all(run)

            if self._settings.hitl_enabled and run.pending_reviews:
                run.status = RunStatus.AWAITING_REVIEW
                await self._emit(run, "run_status", {"status": run.status.value})
                await self._await_reviews(run)

            run.status = RunStatus.SYNTHESISING
            await self._emit(run, "run_status", {"status": run.status.value})
            synthesis = await self._synthesise(run)
            run.synthesis = synthesis

            await self._record(
                run,
                DecisionKind.SYNTHESIS_PRODUCED,
                actor="orchestrator",
                summary="Synthesis produced from human-approved findings",
                detail={
                    "contributing_roles": synthesis.contributing_roles,
                    "recommendation": synthesis.recommendation,
                    "risk_count": len(synthesis.key_risks),
                    "dissent_count": len(synthesis.dissent),
                },
            )
            await self._emit(run, "synthesis", synthesis.model_dump(mode="json"))

            if self._settings.hitl_enabled and self._settings.hitl_final_signoff:
                signoff = await self._request_final_signoff(run)
                if signoff.status == ReviewStatus.REJECTED:
                    run.status = RunStatus.REJECTED
                    run.completed_at = datetime.now(timezone.utc)
                    await self._record(
                        run,
                        DecisionKind.RUN_COMPLETED,
                        actor="human",
                        summary="Run closed: CEO rejected the recommendation",
                        detail={"reviewer_note": signoff.reviewer_note},
                    )
                    await self._emit(run, "run_status", {"status": run.status.value})
                    return
                if signoff.status == ReviewStatus.EDITED and signoff.edited_detail:
                    synthesis.recommendation = signoff.edited_detail

            run.status = RunStatus.COMPLETED
            run.completed_at = datetime.now(timezone.utc)
            await self._record(
                run,
                DecisionKind.RUN_COMPLETED,
                actor="orchestrator",
                summary="Run completed and signed off",
                detail={
                    "handoffs": len(run.handoffs),
                    "findings": sum(len(f.findings) for f in run.findings),
                    "escalations": len(run.reviews),
                    "duration_ms": int(
                        (run.completed_at - run.created_at).total_seconds() * 1000
                    ),
                },
            )
            await self._emit(run, "run_status", {"status": run.status.value})

        except Exception as exc:  # noqa: BLE001
            logger.exception("Run %s failed", run_id)
            run.status = RunStatus.FAILED
            run.error = f"{type(exc).__name__}: {exc}"
            run.completed_at = datetime.now(timezone.utc)
            await self._record(
                run,
                DecisionKind.RUN_FAILED,
                actor="orchestrator",
                summary="Run failed",
                detail={"error": run.error},
            )
            await self._emit(run, "run_status", {"status": run.status.value, "error": run.error})

    # ---------------- routing ----------------

    async def _build_plan(self, run: Run) -> RunPlan:
        """Decide which executives to engage for this objective."""
        run.status = RunStatus.PLANNING
        await self._emit(run, "run_status", {"status": run.status.value})

        available = self._settings.active_roles

        if self._settings.routing_mode == "all" or self._registry is None:
            reason = (
                "Routing is configured to engage the full committee."
                if self._settings.routing_mode == "all"
                else "No routing model available, so the full committee was engaged."
            )
            return self._plan_from_roles(
                available,
                interpretation=run.objective,
                strategy=reason,
                reason_for_selected=reason,
                model_used="none",
            )

        roster = "\n".join(
            f"- {key}: {get_role(key).title} — {get_role(key).lens}" for key in available
        )
        prompt_parts = [
            "## CEO OBJECTIVE",
            run.objective.strip(),
            "",
            "## AVAILABLE EXECUTIVES",
            roster,
        ]
        if run.document:
            prompt_parts += [
                "",
                f"## SOURCE DOCUMENT: {run.document.filename}",
                run.document.numbered_content,
            ]
        else:
            prompt_parts += ["", "## SOURCE DOCUMENT", "None supplied."]

        system = (
            "You are the Orchestrator of the GlobalTech Solutions executive committee. "
            "Before convening anyone, you decide which executives this objective "
            "actually needs.\n\n"
            "RULES:\n"
            "1. Engage an executive only when the material raises a question squarely "
            "in their domain. Convening the whole committee for everything wastes "
            "their time and dilutes the answer.\n"
            f"2. Engage between {self._settings.routing_min_roles} and "
            f"{self._settings.routing_max_roles} executives.\n"
            "3. Give a reason for every executive, including the ones you leave out. "
            "An unexplained omission is indistinguishable from an oversight.\n"
            "4. Order matters: each executive sees the findings of those before them, "
            "so start with whoever's domain is most central.\n"
            "5. Return exactly one entry per executive offered to you."
        )

        try:
            output = await self._registry.run(
                AgentRegistry.ROUTER,
                session_key=f"{run.id}_router",
                message="\n".join(prompt_parts) + "\n\n" + system,
                schema=_PlanOutput,
            )
        except AgentError as exc:
            logger.error("Routing failed, engaging the full committee: %s", exc)
            return self._plan_from_roles(
                available,
                interpretation=run.objective,
                strategy=f"Routing model unavailable ({exc}); engaged the full committee.",
                reason_for_selected="Engaged by fallback after the routing step failed.",
                model_used="none",
            )

        return self._validate_plan(output, available)

    def _validate_plan(self, output: _PlanOutput, available: List[str]) -> RunPlan:
        """Never trust the router's output shape -- normalise and bound it."""
        by_role = {}
        for choice in output.routing:
            key = (choice.role or "").strip().lower()
            if key in available and key not in by_role:
                by_role[key] = choice

        selected = [
            (by_role[k].order, k) for k in available
            if k in by_role and by_role[k].selected
        ]
        selected.sort(key=lambda pair: (pair[0] if pair[0] > 0 else 999))
        chosen = [k for _, k in selected][: self._settings.routing_max_roles]

        # Guard rail: a router that selects nobody has failed, not decided.
        if len(chosen) < self._settings.routing_min_roles:
            logger.warning(
                "Router selected %d role(s); falling back to the full committee.",
                len(chosen),
            )
            return self._plan_from_roles(
                available,
                interpretation=output.interpretation,
                strategy=(
                    "The routing step selected too few executives, so the full "
                    "committee was engaged instead."
                ),
                reason_for_selected="Engaged by fallback: routing returned an unusable plan.",
                model_used=self._settings.model_name if self._registry else "none",
            )

        routing: List[RoleRouting] = []
        for key in available:
            spec = get_role(key)
            choice = by_role.get(key)
            is_selected = key in chosen
            routing.append(
                RoleRouting(
                    role=key,
                    role_title=spec.title,
                    short_title=spec.short_title,
                    selected=is_selected,
                    reason=(choice.reason.strip() if choice else "No routing decision returned."),
                    sequence=chosen.index(key) + 1 if is_selected else 0,
                )
            )

        return RunPlan(
            interpretation=output.interpretation.strip(),
            strategy=output.strategy.strip(),
            routing=routing,
            model_used=self._settings.model_name if self._registry else "none",
        )

    def _plan_from_roles(
        self,
        roles: List[str],
        *,
        interpretation: str,
        strategy: str,
        reason_for_selected: str,
        model_used: str,
    ) -> RunPlan:
        return RunPlan(
            interpretation=interpretation,
            strategy=strategy,
            model_used=model_used,
            routing=[
                RoleRouting(
                    role=key,
                    role_title=get_role(key).title,
                    short_title=get_role(key).short_title,
                    selected=True,
                    reason=reason_for_selected,
                    sequence=index,
                )
                for index, key in enumerate(roles, start=1)
            ],
        )

    async def _await_plan(self, run: Run) -> None:
        signal = self._plan_signals[run.id]
        logger.info("Run %s blocked awaiting plan approval", run.id)
        try:
            await asyncio.wait_for(signal.wait(), timeout=self._settings.hitl_timeout_seconds)
        except asyncio.TimeoutError:
            if run.plan:
                run.plan.approved_by_human = False
            await self._record(
                run,
                DecisionKind.REVIEW_TIMED_OUT,
                actor="orchestrator",
                summary="Plan approval timed out; proceeding with the proposed plan",
                detail={"engaged": run.plan.engaged_roles if run.plan else []},
            )

    # ---------------- dispatch ----------------

    async def _dispatch_all(self, run: Run) -> None:
        """Run each engaged executive as an ADK agent, in the planned order."""
        run.status = RunStatus.DISPATCHING
        await self._emit(run, "run_status", {"status": run.status.value})

        roles = run.plan.engaged_roles if run.plan else self._settings.active_roles
        if self._registry is None:
            raise RuntimeError("The ADK agent registry is not initialised.")
        missing = [r for r in roles if not self._registry.has(r)]
        if missing:
            raise RuntimeError(
                "No ADK agent registered for role(s): "
                f"{', '.join(missing)}. Check ACTIVE_ROLES_CSV and agents/<role>/SKILL.md."
            )

        # Bind the source document so the agents' lookup tools can read it.
        # Set once for the run and cleared at the end, so an agent can never
        # reach a document from another run.
        source_tools.set_document(
            run.document.numbered_content.splitlines() if run.document else None,
            run.document.filename if run.document else "",
        )
        try:
            for index, role_key in enumerate(roles, start=1):
                spec = get_role(role_key)
                handoff = Handoff(
                    run_id=run.id,
                    role=role_key,
                    role_title=spec.title,
                    sequence=index,
                    method="ADK",
                    url=f"adk://{role_key}",
                    auth_mode="in-process",
                )
                run.handoffs.append(handoff)
                await self._emit(run, "handoff", handoff.model_dump(mode="json"))
                await self._call_agent(run, handoff, spec)
        finally:
            source_tools.clear_document()

    def _build_agent_message(self, run: Run, spec) -> str:
        """The brief handed to one executive, including what came before it."""
        blocks = ["## CEO OBJECTIVE", run.objective.strip()]

        if run.document:
            blocks += [
                "",
                f"## SOURCE DOCUMENT: {run.document.filename}",
                f"{run.document.line_count} numbered lines are available.",
                "Call `document_outline` to see its structure, then "
                "`lookup_source_lines` to read the parts that concern you. "
                "Cite the line numbers you actually read.",
            ]
        else:
            blocks += [
                "",
                "## SOURCE DOCUMENT",
                "None supplied. You have nothing to cite, so every finding must "
                "carry low confidence and be flagged for human review.",
            ]

        if run.findings:
            blocks += ["", "## WHAT OTHER EXECUTIVES HAVE ALREADY REPORTED"]
            for prior in run.findings:
                blocks.append(f"\n### {prior.role_title}")
                blocks.append(prior.summary)
                for finding in prior.findings:
                    blocks.append(
                        f"- {finding.headline} (confidence {finding.confidence:.2f})"
                    )
            blocks.append(
                "\nBuild on or challenge the above where it touches your domain. "
                "Do not simply restate it."
            )

        blocks += [
            "",
            "## YOUR TASK",
            f"Respond as {spec.short_title}. Produce 3-5 findings within your lens, "
            "each cited to source lines you have read, each with an honest "
            "confidence score.",
        ]
        return "\n".join(blocks)

    async def _call_agent(self, run: Run, handoff: Handoff, spec) -> None:
        message = self._build_agent_message(run, spec)
        handoff.request_bytes = len(message.encode("utf-8"))
        handoff.status = HandoffStatus.IN_FLIGHT
        handoff.dispatched_at = datetime.now(timezone.utc)

        await self._record(
            run,
            DecisionKind.HANDOFF_DISPATCHED,
            actor="orchestrator",
            summary=f"ADK invoke -> {handoff.role_title}",
            detail={
                "handoff_id": handoff.id,
                "agent": handoff.url,
                "transport": "adk-in-process",
                "prior_findings": len(run.findings),
                "request_bytes": handoff.request_bytes,
            },
        )
        await self._emit(run, "handoff", handoff.model_dump(mode="json"))

        started = time.perf_counter()
        last_error = ""

        for attempt in range(1, self._settings.agent_max_retries + 2):
            handoff.attempt = attempt
            try:
                output = await asyncio.wait_for(
                    self._registry.run(
                        handoff.role,
                        session_key=f"{run.id}_{handoff.role}_{attempt}",
                        message=message,
                        schema=ExecutiveOut,
                    ),
                    timeout=self._settings.agent_timeout_seconds,
                )
                handoff.http_status = 200  # in-process success, shown as OK
                handoff.tool_calls = int(getattr(output, "_tool_calls", 0) or 0)
                handoff.response_bytes = len(output.model_dump_json().encode("utf-8"))
                finding = self._to_agent_finding(run, handoff, spec, output, started)
                await self._accept_finding(run, handoff, finding, started)
                return
            except asyncio.TimeoutError:
                last_error = (
                    f"ADK agent '{handoff.role}' exceeded "
                    f"{self._settings.agent_timeout_seconds:.0f}s."
                )
            except AgentError as exc:
                last_error = str(exc)
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                break
            if attempt <= self._settings.agent_max_retries:
                await asyncio.sleep(min(2 ** (attempt - 1), 8))

        handoff.status = HandoffStatus.FAILED
        handoff.returned_at = datetime.now(timezone.utc)
        handoff.latency_ms = int((time.perf_counter() - started) * 1000)
        handoff.error = last_error

        await self._record(
            run,
            DecisionKind.HANDOFF_FAILED,
            actor="orchestrator",
            summary=f"{handoff.role_title} did not respond; continuing without it",
            detail={
                "handoff_id": handoff.id,
                "agent": handoff.url,
                "attempts": handoff.attempt,
                "error": last_error,
            },
        )
        await self._emit(run, "handoff", handoff.model_dump(mode="json"))

    def _to_agent_finding(
        self, run: Run, handoff: Handoff, spec, output: ExecutiveOut, started: float
    ) -> AgentFinding:
        """Convert ADK output to the wire model, validating every citation.

        A citation pointing outside the document is itself a hallucination, so
        it is discarded and the finding's confidence is capped below the floor,
        which routes it to a human.
        """
        max_line = run.document.line_count if run.document else 0
        floor = self._settings.hitl_confidence_floor
        findings: List[Finding] = []

        for raw in output.findings:
            citations: List[Citation] = []
            dropped = 0
            for c in raw.citations:
                lo, hi = min(c.line_start, c.line_end), max(c.line_start, c.line_end)
                if lo < 1 or (max_line and hi > max_line):
                    dropped += 1
                    continue
                citations.append(
                    Citation(line_start=lo, line_end=hi, quote=c.quote.strip())
                )

            confidence = max(0.0, min(1.0, float(raw.confidence)))
            rationale = raw.review_rationale.strip()
            requires_review = bool(raw.requires_human_review)

            if dropped:
                confidence = min(confidence, floor - 0.01)
                requires_review = True
                rationale = (
                    f"{dropped} citation(s) referenced lines outside the source "
                    f"document and were discarded. {rationale}"
                ).strip()
            if not citations:
                confidence = min(confidence, floor - 0.01)
                requires_review = True
                rationale = (rationale + " No source citation was provided.").strip()

            findings.append(
                Finding(
                    headline=raw.headline.strip(),
                    detail=raw.detail.strip(),
                    confidence=confidence,
                    citations=citations,
                    requires_human_review=requires_review,
                    review_rationale=rationale,
                )
            )

        return AgentFinding(
            run_id=run.id,
            handoff_id=handoff.id,
            role=spec.key,
            role_title=spec.title,
            summary=output.summary.strip(),
            findings=findings,
            recommendation=output.recommendation.strip(),
            open_questions=[q.strip() for q in output.open_questions if q.strip()],
            model_used=self._settings.model_name,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def _accept_finding(
        self, run: Run, handoff: Handoff, finding: AgentFinding, started: float
    ) -> None:
        handoff.status = HandoffStatus.SUCCEEDED
        handoff.returned_at = datetime.now(timezone.utc)
        handoff.latency_ms = int((time.perf_counter() - started) * 1000)
        handoff.finding_count = len(finding.findings)
        run.findings.append(finding)

        await self._record(
            run,
            DecisionKind.HANDOFF_RETURNED,
            actor=finding.role,
            summary=f"{finding.role_title} returned {len(finding.findings)} finding(s)",
            detail={
                "handoff_id": handoff.id,
                "http_status": handoff.http_status,
                "latency_ms": handoff.latency_ms,
                "agent_latency_ms": finding.latency_ms,
                "model": finding.model_used,
                "recommendation": finding.recommendation,
                "open_questions": finding.open_questions,
            },
        )

        escalated = 0
        for item in finding.findings:
            reason, detail = self._triage(item)
            if reason is None:
                await self._record(
                    run,
                    DecisionKind.FINDING_ACCEPTED,
                    actor="orchestrator",
                    summary=f"Accepted: {item.headline[:90]}",
                    detail={
                        "role": finding.role,
                        "finding_id": item.id,
                        "confidence": item.confidence,
                        "citations": len(item.citations),
                    },
                )
                continue

            escalated += 1
            review = ReviewItem(
                run_id=run.id,
                role=finding.role,
                role_title=finding.role_title,
                finding_id=item.id,
                reason=reason,
                reason_detail=detail,
                original_headline=item.headline,
                original_detail=item.detail,
                confidence=item.confidence,
                citations=item.citations,
            )
            run.reviews.append(review)
            self._review_signals[run.id].clear()

            await self._record(
                run,
                DecisionKind.FINDING_ESCALATED,
                actor="orchestrator",
                summary=f"Escalated to human ({reason.value}): {item.headline[:80]}",
                detail={
                    "role": finding.role,
                    "finding_id": item.id,
                    "review_id": review.id,
                    "confidence": item.confidence,
                    "citations": len(item.citations),
                    "reason_detail": detail,
                },
            )
            await self._emit(run, "review", review.model_dump(mode="json"))

        handoff.escalated_count = escalated
        await self._emit(run, "handoff", handoff.model_dump(mode="json"))
        await self._emit(run, "finding", finding.model_dump(mode="json"))

    def _triage(self, item) -> tuple[Optional[EscalationReason], str]:
        """Decide whether a finding may pass without a human.

        Checked most-specific first, so the reviewer is shown the sharpest
        diagnosis available: an uncited claim is reported as uncited rather
        than as a generic agent request.
        """
        if not self._settings.hitl_enabled:
            return None, ""
        if self._settings.hitl_require_citations and not item.citations:
            return (
                EscalationReason.MISSING_CITATION,
                "No citation into the source document, so the claim cannot be verified. "
                + item.review_rationale,
            )
        if item.confidence < self._settings.hitl_confidence_floor:
            return (
                EscalationReason.LOW_CONFIDENCE,
                f"Confidence {item.confidence:.2f} is below the "
                f"{self._settings.hitl_confidence_floor:.2f} floor. "
                + item.review_rationale,
            )
        if item.requires_human_review:
            return (
                EscalationReason.AGENT_FLAGGED,
                item.review_rationale or "The agent asked for human verification.",
            )
        return None, ""

    def _apply_edit(self, run: Run, item: ReviewItem) -> None:
        """Write a human's edit back onto the underlying finding."""
        for agent_finding in run.findings:
            for finding in agent_finding.findings:
                if finding.id == item.finding_id:
                    finding.detail = item.edited_detail
                    finding.confidence = 1.0
                    return

    async def _await_reviews(self, run: Run) -> None:
        signal = self._review_signals[run.id]
        if not run.pending_reviews:
            return
        logger.info(
            "Run %s blocked on %d human review(s)", run.id, len(run.pending_reviews)
        )
        try:
            await asyncio.wait_for(signal.wait(), timeout=self._settings.hitl_timeout_seconds)
        except asyncio.TimeoutError:
            for item in run.pending_reviews:
                item.status = ReviewStatus.TIMED_OUT
                item.resolved_at = datetime.now(timezone.utc)
                await self._record(
                    run,
                    DecisionKind.REVIEW_TIMED_OUT,
                    actor="orchestrator",
                    summary=f"Review timed out and was dropped: {item.original_headline[:80]}",
                    detail={"review_id": item.id, "role": item.role},
                )
                await self._emit(run, "review_resolved", item.model_dump(mode="json"))

    async def _request_final_signoff(self, run: Run) -> ReviewItem:
        synthesis = run.synthesis
        item = ReviewItem(
            run_id=run.id,
            role="orchestrator",
            role_title="Orchestrator",
            reason=EscalationReason.FINAL_SIGNOFF,
            reason_detail="The CEO must sign off before this recommendation is acted on.",
            original_headline="Final recommendation",
            original_detail=synthesis.recommendation if synthesis else "",
            confidence=1.0,
        )
        run.reviews.append(item)
        self._review_signals[run.id].clear()
        run.status = RunStatus.AWAITING_SIGNOFF

        await self._record(
            run,
            DecisionKind.FINDING_ESCALATED,
            actor="orchestrator",
            summary="Final synthesis held for human sign-off",
            detail={"review_id": item.id, "reason": item.reason.value},
        )
        await self._emit(run, "review", item.model_dump(mode="json"))
        await self._emit(run, "run_status", {"status": run.status.value})

        await self._await_reviews(run)
        return item

    # ---------------- synthesis ----------------

    async def _synthesise(self, run: Run) -> Synthesis:
        approved = self._approved_material(run)
        contributing = sorted({role for role, _ in approved})

        if self._registry is None:
            return Synthesis(
                executive_summary=(
                    "Synthesis model unavailable; showing approved findings verbatim."
                ),
                recommendation="\n".join(
                    f.recommendation for f in run.findings if f.recommendation
                ),
                contributing_roles=contributing,
                model_used="none",
            )

        lines: List[str] = [
            "## CEO OBJECTIVE",
            run.objective,
            "",
            "## HUMAN-APPROVED EXECUTIVE FINDINGS",
        ]
        for agent_finding in run.findings:
            approved_for_role = [
                text for role, text in approved if role == agent_finding.role
            ]
            if not approved_for_role:
                continue
            lines.append(f"\n### {agent_finding.role_title}")
            lines.append(agent_finding.summary)
            lines.extend(f"- {t}" for t in approved_for_role)
            if agent_finding.recommendation:
                lines.append(f"Recommendation: {agent_finding.recommendation}")
            for question in agent_finding.open_questions:
                lines.append(f"Open question: {question}")

        rejected = [r for r in run.reviews if r.status == ReviewStatus.REJECTED]
        if rejected:
            lines.append("\n## FINDINGS A HUMAN REJECTED -- DO NOT USE THESE")
            lines.extend(f"- ({r.role_title}) {r.original_headline}" for r in rejected)

        failed = [h for h in run.handoffs if h.status == HandoffStatus.FAILED]
        if failed:
            lines.append("\n## EXECUTIVES WHO DID NOT REPORT")
            lines.extend(
                f"- {h.role_title}: no input available, treat their domain as unassessed"
                for h in failed
            )

        not_assessed = self._not_assessed(run)
        if not_assessed:
            lines.append("\n## DOMAINS DELIBERATELY NOT ASSESSED")
            lines.extend(f"- {item}" for item in not_assessed)
            lines.append(
                "\nDo not speculate about these domains. Name them as out of scope."
            )

        system = (
            "You are the Orchestrator of the GlobalTech Solutions executive committee. "
            "Consolidate the executives' human-approved findings into one recommendation "
            "for the CEO.\n\n"
            "RULES:\n"
            "1. Use ONLY the findings supplied below. Introduce no new facts, figures or "
            "names of your own.\n"
            "2. Findings a human rejected must not influence the outcome in any way.\n"
            "3. Do not average away disagreement -- surface it in `dissent`, naming who "
            "held which position.\n"
            "4. Anything the committee could not answer belongs in `unresolved`.\n"
            "5. Lead with the logic, close with the recommendation. No filler."
        )

        try:
            output = await self._registry.run(
                AgentRegistry.SYNTHESISER,
                session_key=f"{run.id}_synth",
                message="\n".join(lines),
                schema=_SynthesisOutput,
            )
        except AgentError as exc:
            logger.error("Synthesis failed: %s", exc)
            return Synthesis(
                executive_summary=f"Synthesis could not be generated: {exc}",
                recommendation="",
                contributing_roles=contributing,
                model_used=self._settings.model_name,
            )

        return Synthesis(
            executive_summary=output.executive_summary.strip(),
            recommendation=output.recommendation.strip(),
            key_risks=[r.strip() for r in output.key_risks if r.strip()],
            next_actions=[a.strip() for a in output.next_actions if a.strip()],
            dissent=[d.strip() for d in output.dissent if d.strip()],
            unresolved=[u.strip() for u in output.unresolved if u.strip()],
            contributing_roles=contributing,
            not_assessed=self._not_assessed(run),
            model_used=self._settings.model_name,
        )

    def _not_assessed(self, run: Run) -> List[str]:
        """Domains the committee never covered, and why. Stated, not hidden."""
        items: List[str] = []
        if run.plan:
            items.extend(
                f"{entry.role_title}: {entry.reason}" for entry in run.plan.skipped
            )
        items.extend(
            f"{h.role_title}: engaged but did not respond ({h.error or 'no response'})"
            for h in run.handoffs
            if h.status == HandoffStatus.FAILED
        )
        return items

    def _approved_material(self, run: Run) -> List[tuple[str, str]]:
        """Findings that survived the human gate, with edits applied."""
        blocked = {
            r.finding_id
            for r in run.reviews
            if r.status in {ReviewStatus.REJECTED, ReviewStatus.TIMED_OUT, ReviewStatus.PENDING}
        }
        result: List[tuple[str, str]] = []
        for agent_finding in run.findings:
            for finding in agent_finding.findings:
                if finding.id in blocked:
                    continue
                cites = ", ".join(
                    f"L{c.line_start}-L{c.line_end}" for c in finding.citations
                )
                suffix = f" [source: {cites}]" if cites else ""
                result.append(
                    (agent_finding.role, f"{finding.headline} — {finding.detail}{suffix}")
                )
        return result

    # ---------------- plumbing ----------------

    async def _record(
        self,
        run: Run,
        kind: DecisionKind,
        *,
        actor: str,
        summary: str,
        detail: Dict[str, Any] | None = None,
    ) -> None:
        async with self._lock:
            self._sequences[run.id] = self._sequences.get(run.id, 0) + 1
            sequence = self._sequences[run.id]
        entry = make_entry(
            run_id=run.id,
            sequence=sequence,
            kind=kind,
            actor=actor,
            summary=summary,
            detail=detail,
        )
        run.decision_log.append(entry)
        await self._log.append(entry)
        await self._emit(run, "decision", entry.model_dump(mode="json"))

    async def _emit(self, run: Run, event: str, data: Dict[str, Any]) -> None:
        message = {"event": event, "data": data}
        for queue in list(self._subscribers.get(run.id, set())):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("Dropping event for a slow dashboard subscriber")
