"""A deterministic stand-in for the ADK agent registry.

Lets the whole service -- routing, the plan gate, dispatch, the human gates,
the decision log and the CEO brief -- be exercised without Vertex AI, an API
key, or a model call.

It deliberately emits one of each escalation trigger so the human-in-the-loop
path is always covered.
"""

from __future__ import annotations

from typing import List, Type

from pydantic import BaseModel

from adk_csuite.agents import AgentRegistry, ExecutiveOut, PlanOut, SynthesisOut

# The stub engages three of five, so tests prove the Orchestrator routes
# rather than fanning out to everyone.
ENGAGED_ORDER = {"cto": 1, "cfo": 2, "cmo": 3}


class StubAgentRegistry(AgentRegistry):
    """Drop-in replacement that never touches ADK or a model."""

    def __init__(self, settings) -> None:  # noqa: D107
        super().__init__(settings)
        self._names: List[str] = []

    def build(self) -> None:
        self._names = list(self._settings.active_roles) + [
            AgentRegistry.ROUTER,
            AgentRegistry.SYNTHESISER,
        ]

    @property
    def agent_names(self) -> List[str]:
        return sorted(self._names)

    def has(self, name: str) -> bool:
        return name in self._names

    async def run(
        self, name: str, *, session_key: str, message: str, schema: Type[BaseModel]
    ) -> BaseModel:
        if name == AgentRegistry.ROUTER:
            result = schema.model_validate(self._plan(message))
        elif name == AgentRegistry.SYNTHESISER:
            result = schema.model_validate(self._synthesis())
        else:
            result = schema.model_validate(self._executive(name))
        # Mirrors the real registry, which reports how many times the agent
        # used a tool to read the source document.
        setattr(result, "_tool_calls", 2 if name not in
                (AgentRegistry.ROUTER, AgentRegistry.SYNTHESISER) else 0)
        return result

    # ---------- fixtures ----------

    def _plan(self, message: str) -> dict:
        offered = [k for k in self._settings.active_roles if f"- {k}:" in message] \
            or list(self._settings.active_roles)
        return {
            "interpretation": "Stub interpretation of the objective.",
            "strategy": "Stub strategy: engage only the implicated domains.",
            "routing": [
                {
                    "role": key,
                    "selected": key in ENGAGED_ORDER,
                    "reason": (
                        f"{key.upper()} is implicated by the source material."
                        if key in ENGAGED_ORDER
                        else f"{key.upper()}'s domain is not raised by this objective."
                    ),
                    "order": ENGAGED_ORDER.get(key, 0),
                }
                for key in offered
            ],
        }

    @staticmethod
    def _executive(role: str) -> dict:
        upper = role.upper()
        return {
            "summary": f"{upper} stub read of the situation.",
            "recommendation": f"{upper} recommends the stub course of action.",
            "open_questions": [f"{upper}: what is the budget envelope?"],
            "findings": [
                {
                    "headline": f"{upper} finding A - fully grounded",
                    "detail": "Supported directly by the source document.",
                    "confidence": 0.93,
                    "citations": [{"line_start": 1, "line_end": 2, "quote": "stub quote"}],
                    "requires_human_review": False,
                    "review_rationale": "",
                },
                {
                    "headline": f"{upper} finding B - no citation",
                    "detail": "Asserted without pointing at the source.",
                    "confidence": 0.9,
                    "citations": [],
                    "requires_human_review": False,
                    "review_rationale": "",
                },
                {
                    "headline": f"{upper} finding C - low confidence",
                    "detail": "An inference beyond what the document states.",
                    "confidence": 0.41,
                    "citations": [{"line_start": 2, "line_end": 3, "quote": "stub quote"}],
                    "requires_human_review": False,
                    "review_rationale": "",
                },
                {
                    "headline": f"{upper} finding D - citation out of range",
                    "detail": "Cites a line the document does not contain.",
                    "confidence": 0.95,
                    "citations": [
                        {"line_start": 99000, "line_end": 99001, "quote": "does not exist"}
                    ],
                    "requires_human_review": False,
                    "review_rationale": "",
                },
                {
                    "headline": f"{upper} finding E - agent asked for a human",
                    "detail": "Well grounded, but it would commit budget.",
                    "confidence": 0.96,
                    "citations": [{"line_start": 1, "line_end": 1, "quote": "stub quote"}],
                    "requires_human_review": True,
                    "review_rationale": "Commits spend, so a human must approve it.",
                },
            ],
        }

    @staticmethod
    def _synthesis() -> dict:
        return {
            "executive_summary": "Stub synthesis of the human-approved findings.",
            "recommendation": "Proceed with the stub recommendation.",
            "key_risks": ["Stub risk"],
            "next_actions": ["Stub action (owner: CTO)"],
            "dissent": ["CFO and CSO disagreed on sequencing"],
            "unresolved": ["Budget envelope still unknown"],
        }
