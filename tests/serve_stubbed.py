"""Serve the ADK C-Suite with the stub registry patched in.

    python tests/serve_stubbed.py --port 8300

Patching happens before adk_csuite.main is imported, so no test-only code
lives in the service itself.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    import os

    import adk_csuite.model_probe as model_probe  # noqa: E402
    import adk_csuite.main as service_main  # noqa: E402
    from stub_registry import StubAgentRegistry  # noqa: E402

    # Replace the registry, the ADK backend setup and the model probe, so
    # nothing reaches Vertex AI or Secret Manager.
    service_main.AgentRegistry = StubAgentRegistry
    service_main._configure_adk_backend = lambda: None

    # PROBE_FIXTURE=fail exercises the path where the agents build but their
    # model is unreachable -- the failure that reached the stage.
    fail = os.environ.get("PROBE_FIXTURE", "ok") == "fail"

    def _stub_probe(settings):
        if fail:
            return model_probe.ProbeResult(
                ok=False,
                model=settings.model_name,
                location=settings.vertex_location,
                backend=settings.adk_backend,
                error="ClientError: 404 NOT_FOUND. Publisher model not found.",
                guidance="Try VERTEX_LOCATION=global, or pick a listed model id.",
                available_models=["gemini-3.6-flash", "gemini-3.5-flash-lite"],
            )
        return model_probe.ProbeResult(
            ok=True,
            model=settings.model_name,
            location=settings.vertex_location,
            backend=settings.adk_backend,
        )

    model_probe.probe = _stub_probe

    import uvicorn

    uvicorn.run(service_main.app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
