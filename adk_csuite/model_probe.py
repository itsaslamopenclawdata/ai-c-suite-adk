"""Verify the configured model is actually reachable, at startup.

Constructing an ADK ``LlmAgent`` does not validate the model name -- the first
request does. That meant five agents could report healthy and then every one
of them 404 on stage. This module closes that gap: it makes one minimal
generation call at startup, and on failure enumerates the models the project
*can* reach, so the fix is a copy-paste rather than a guess.

Newer Gemini models are frequently served only from Vertex's ``global``
endpoint, so ``VERTEX_LOCATION=global`` is a supported value and is suggested
explicitly when a regional lookup fails.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ProbeResult:
    ok: bool
    model: str
    location: str
    backend: str
    error: str = ""
    guidance: str = ""
    available_models: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        out = {
            "ok": self.ok,
            "model": self.model,
            "location": self.location,
            "backend": self.backend,
        }
        if not self.ok:
            out["error"] = self.error
            out["guidance"] = self.guidance
            if self.available_models:
                out["models_available_to_this_project"] = self.available_models
        return out


def _build_client(settings):
    from google import genai

    if settings.adk_backend == "vertex":
        return genai.Client(
            vertexai=True,
            project=settings.gcp_project_id,
            location=settings.vertex_location,
        )
    return genai.Client(api_key=settings.gemini_api_key)


def list_available_models(settings, limit: int = 40) -> List[str]:
    """Best-effort list of model IDs this project can call."""
    try:
        client = _build_client(settings)
        names = []
        for model in client.models.list():
            name = getattr(model, "name", "") or ""
            # Vertex returns publishers/google/models/<id>; keep the id.
            names.append(name.split("/")[-1] if "/" in name else name)
            if len(names) >= limit:
                break
        return sorted({n for n in names if n})
    except Exception as exc:  # noqa: BLE001 - diagnostics must not raise
        logger.warning("Could not list available models: %s", exc)
        return []


def probe(settings) -> ProbeResult:
    """Make one tiny call to confirm the model answers."""
    location = (
        settings.vertex_location if settings.adk_backend == "vertex" else "n/a"
    )
    result = ProbeResult(
        ok=False,
        model=settings.model_name,
        location=location,
        backend=settings.adk_backend,
    )

    try:
        from google.genai import types

        client = _build_client(settings)
        client.models.generate_content(
            model=settings.model_name,
            contents="ping",
            config=types.GenerateContentConfig(max_output_tokens=8),
        )
        result.ok = True
        logger.info(
            "Model probe OK: %s via %s (%s)",
            settings.model_name,
            settings.adk_backend,
            location,
        )
        return result
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        result.error = message[:600]
        result.guidance = _guidance(settings, message)
        if "NOT_FOUND" in message or "404" in message:
            result.available_models = list_available_models(settings)
        logger.error("Model probe FAILED for %s: %s", settings.model_name, message[:300])
        return result


def _guidance(settings, message: str) -> str:
    lowered = message.lower()

    if "not_found" in lowered or "404" in lowered:
        base = (
            f"Model '{settings.model_name}' is not available to this project"
            f" in location '{settings.vertex_location}'."
        )
        if settings.adk_backend == "vertex" and settings.vertex_location != "global":
            return (
                base
                + " Newer Gemini models are often served only from Vertex's global"
                + " endpoint. Try VERTEX_LOCATION=global, or pick one of the model"
                + " ids listed below and set MODEL_NAME to it, then redeploy."
            )
        return base + " Set MODEL_NAME to one of the ids listed below and redeploy."

    if "permission" in lowered or "403" in lowered:
        return (
            "Vertex AI denied the call. The runtime service account needs"
            " roles/aiplatform.user, and the aiplatform.googleapis.com API must be"
            " enabled. A freshly granted role can take a minute to propagate."
        )
    if "api key" in lowered or "401" in lowered or "unauthenticated" in lowered:
        return (
            "Authentication failed. With ADK_BACKEND=vertex there should be no key"
            " at all -- check the service account. With api_key, check the Secret"
            " Manager value."
        )
    if "quota" in lowered or "429" in lowered or "resource_exhausted" in lowered:
        return (
            "Quota exhausted for this model in this location. Request quota, or"
            " switch MODEL_NAME to a model you have headroom on."
        )
    return "See the error above. Logs: gcloud run services logs read <service>."
