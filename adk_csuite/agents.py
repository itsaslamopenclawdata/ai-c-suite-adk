"""ADK agent definitions and the runner that drives them.

Every model call in this service goes through ADK: the router, the five
executives, and the synthesiser are all ``LlmAgent`` instances executed by an
ADK ``Runner`` with an in-memory session service.

This is the architectural contrast with Option 1. There, five executives are
five Cloud Run services and the Orchestrator calls them over authenticated
HTTP. Here they are five ADK agents composed inside one process. Same
guarantees, different deployment shape.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional, Type

from pydantic import BaseModel, Field, ValidationError

from adk_csuite.config import Settings
from adk_csuite.roles import RoleSpec, get_role, load_context, load_skill
from adk_csuite.tools import EXECUTIVE_TOOLS

logger = logging.getLogger(__name__)


class AgentError(RuntimeError):
    """Raised when an ADK agent fails or returns unusable output."""


# --------------------------------------------------------------------------
# Output schemas -- ADK enforces these on the final response
# --------------------------------------------------------------------------


class CitationOut(BaseModel):
    line_start: int = Field(..., description="First source line number relied on.")
    line_end: int = Field(..., description="Last source line number relied on.")
    quote: str = Field(..., description="Verbatim excerpt from those lines.")


class FindingOut(BaseModel):
    headline: str = Field(..., description="One sentence stating the finding.")
    detail: str = Field(..., description="2-4 sentences of reasoning.")
    confidence: float = Field(
        ...,
        description=(
            "0.0-1.0. Your honest confidence that this is fully supported by the "
            "source document. Below 0.75 whenever you infer beyond what it states."
        ),
    )
    citations: List[CitationOut] = Field(
        ..., description="Source lines supporting this finding, verified with the tool."
    )
    requires_human_review: bool = Field(
        ..., description="True if a human must verify this before it is acted on."
    )
    review_rationale: str = Field(..., description="Why. Empty string if not needed.")


class ExecutiveOut(BaseModel):
    summary: str = Field(..., description="Your read of the situation, 2-3 sentences.")
    findings: List[FindingOut] = Field(..., description="3-5 findings from your lens.")
    recommendation: str = Field(..., description="Your single clearest recommendation.")
    open_questions: List[str] = Field(
        ..., description="What you could NOT determine from the source document."
    )


class RoleChoiceOut(BaseModel):
    role: str = Field(..., description="One of the role keys offered to you.")
    selected: bool = Field(..., description="True if this executive is needed.")
    reason: str = Field(
        ...,
        description=(
            "One sentence. If selected, what you need from them. If not, why "
            "their domain is not implicated."
        ),
    )
    order: int = Field(..., description="Consultation order from 1, or 0 if not selected.")


class PlanOut(BaseModel):
    interpretation: str = Field(..., description="2-3 sentences: what is being asked.")
    strategy: str = Field(..., description="1-2 sentences: why this set, in this order.")
    routing: List[RoleChoiceOut] = Field(..., description="One entry per role offered.")


class SynthesisOut(BaseModel):
    executive_summary: str = Field(..., description="3-4 sentences for the CEO.")
    recommendation: str = Field(..., description="The single decision you recommend.")
    key_risks: List[str] = Field(..., description="Material risks raised by the C-suite.")
    next_actions: List[str] = Field(..., description="Concrete next steps with an owner role.")
    dissent: List[str] = Field(..., description="Where executives disagreed, and who held what.")
    unresolved: List[str] = Field(..., description="What the committee could not answer.")


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------


def executive_instruction(role: RoleSpec, skill: str, context: str) -> str:
    return f"""You are the {role.title} ({role.short_title}) of GlobalTech Solutions,
sitting on a live executive committee. Your lens is: {role.lens}.

You contribute ONLY from your own functional perspective. If a point belongs to
another executive, leave it to them.

## GROUNDING RULES -- these override everything else
1. Before you cite a passage, read it with the `lookup_source_lines` tool.
   Start with `document_outline` to find the sections that concern you.
   Citing a line you have not read is how errors reach the CEO.
2. Never state a figure, name, date or metric that is not in the source
   document. Do not reconstruct one from memory or plausibility.
3. If the document does not contain what you need, say so in `open_questions`.
   An acknowledged gap is a correct answer; an invented number is a failure.
4. Set `confidence` honestly. Below 0.75 means "a human must check this".
5. Set `requires_human_review` for anything that would commit money, headcount,
   customer communication or a production change.

## YOUR ROLE DEFINITION
{skill}

## COMPANY CONTEXT
{context}

Lead with the logic, close with the recommendation, no filler."""


ROUTER_INSTRUCTION = """You are the Orchestrator of the GlobalTech Solutions executive
committee. Before convening anyone, you decide which executives an objective
actually needs.

RULES:
1. Engage an executive only when the material raises a question squarely in
   their domain. Convening everyone for everything wastes their time and
   dilutes the answer.
2. Give a reason for every executive, including those you leave out. An
   unexplained omission is indistinguishable from an oversight.
3. Order matters: each executive sees the findings of those before them, so
   start with whoever's domain is most central.
4. Return exactly one entry per executive offered to you."""


SYNTHESIS_INSTRUCTION = """You are the Orchestrator of the GlobalTech Solutions
executive committee. Consolidate the executives' human-approved findings into
one recommendation for the CEO.

RULES:
1. Use ONLY the findings supplied. Introduce no new facts, figures or names.
2. Findings a human rejected must not influence the outcome in any way.
3. Do not average away disagreement -- surface it in `dissent`, naming who held
   which position.
4. Anything the committee could not answer belongs in `unresolved`.
5. Lead with the logic, close with the recommendation. No filler."""


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


class AgentRegistry:
    """Builds and owns one ADK Runner per agent, created once at startup."""

    ROUTER = "__router__"
    SYNTHESISER = "__synthesiser__"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._runners: Dict[str, object] = {}
        self._context = ""

    # ---------- construction ----------

    def build(self) -> None:
        from google.adk.agents import LlmAgent
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types

        self._context = load_context(self._settings.context_dir)
        generate_config = types.GenerateContentConfig(
            temperature=self._settings.model_temperature,
            max_output_tokens=self._settings.model_max_output_tokens,
        )

        def make(name: str, instruction: str, schema: Type[BaseModel], tools: list):
            agent = LlmAgent(
                name=name,
                model=self._settings.model_name,
                instruction=instruction,
                output_schema=schema,
                tools=tools,
                generate_content_config=generate_config,
            )
            # A session service per agent keeps runs isolated from each other.
            self._runners[name] = Runner(
                agent=agent,
                app_name=f"adk_csuite_{name}",
                session_service=InMemorySessionService(),
            )

        for role_key in self._settings.active_roles:
            spec = get_role(role_key)
            make(
                role_key,
                executive_instruction(
                    spec, load_skill(role_key, self._settings.agents_dir), self._context
                ),
                ExecutiveOut,
                list(EXECUTIVE_TOOLS),
            )

        # The router and synthesiser reason over supplied text only, so they
        # get no tools.
        make(self.ROUTER, ROUTER_INSTRUCTION, PlanOut, [])
        make(self.SYNTHESISER, SYNTHESIS_INSTRUCTION, SynthesisOut, [])

        logger.info(
            "ADK registry ready: %d agents (%s)",
            len(self._runners),
            ", ".join(sorted(self._runners)),
        )

    @property
    def agent_names(self) -> List[str]:
        return sorted(self._runners)

    def has(self, name: str) -> bool:
        return name in self._runners

    # ---------- execution ----------

    async def run(
        self, name: str, *, session_key: str, message: str, schema: Type[BaseModel]
    ) -> BaseModel:
        """Run one ADK agent to completion and parse its structured output."""
        runner = self._runners.get(name)
        if runner is None:
            raise AgentError(f"No ADK agent registered under '{name}'.")

        from google.genai import types

        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id=session_key
        )

        text = ""
        tool_calls = 0
        try:
            async for event in runner.run_async(
                user_id=session_key,
                session_id=session.id,
                new_message=types.Content(
                    role="user", parts=[types.Part(text=message)]
                ),
            ):
                if getattr(event, "get_function_calls", None) and event.get_function_calls():
                    tool_calls += len(event.get_function_calls())
                if event.is_final_response() and event.content and event.content.parts:
                    text = "".join(
                        part.text for part in event.content.parts
                        if getattr(part, "text", None) and not getattr(part, "thought", False)
                    )
                    break
        except Exception as exc:  # noqa: BLE001
            logger.exception("ADK agent '%s' failed", name)
            raise AgentError(f"ADK agent '{name}' failed: {type(exc).__name__}: {exc}") from exc
        finally:
            await _delete_session(runner, session_key, session.id)

        if not text.strip():
            raise AgentError(f"ADK agent '{name}' returned an empty response.")

        try:
            parsed = schema.model_validate(json.loads(text))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise AgentError(
                f"ADK agent '{name}' returned output that did not match "
                f"{schema.__name__}."
            ) from exc

        logger.info("ADK agent '%s' completed (%d tool call(s))", name, tool_calls)
        setattr(parsed, "_tool_calls", tool_calls)
        return parsed


async def _delete_session(runner, user_id: str, session_id: str) -> None:
    """Sessions are per-request; drop them so memory does not grow unbounded."""
    try:
        await runner.session_service.delete_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
    except Exception:  # noqa: BLE001 - cleanup must never fail a run
        logger.debug("Session cleanup skipped for %s", session_id)
