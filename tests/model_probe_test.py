"""Tests for the startup model probe and its effect on health.

Written after a deploy reported five healthy agents and then every one of them
returned 404 on the first run: constructing an ADK LlmAgent does not validate
the model name. The probe closes that gap, and health must fail when it fails
-- otherwise deploy.sh reports success on a system that cannot answer.

    python tests/model_probe_test.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
PORT = 8322

PASS, FAIL = "\033[1;32m✓\033[0m", "\033[1;31m✗\033[0m"
failures: list[str] = []

PROXY_VARS = [
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "GRPC_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy", "grpc_proxy",
]


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  {PASS} {label}")
    else:
        print(f"  {FAIL} {label}  {detail}")
        failures.append(label)


def main() -> int:
    from adk_csuite.config import Settings
    from adk_csuite.model_probe import _guidance

    print("\n-- Guidance for each failure mode --")
    vertex = Settings(
        gcp_project_id="p", vertex_location="us-central1",
        model_name="gemini-3.7-flash", adk_backend="vertex",
    )
    not_found = (
        "ClientError: 404 NOT_FOUND. Publisher model "
        "projects/p/locations/us-central1/publishers/google/models/"
        "gemini-3.7-flash was not found"
    )

    g = _guidance(vertex, not_found)
    check("404 names the model and location",
          "gemini-3.7-flash" in g and "us-central1" in g, g[:90])
    check("404 suggests the global endpoint", "global" in g, g[:90])

    glob = Settings(gcp_project_id="p", vertex_location="global",
                    model_name="x", adk_backend="vertex")
    g = _guidance(glob, not_found)
    check("does not suggest global when already global",
          "Try VERTEX_LOCATION=global" not in g, g[:90])

    check("403 points at aiplatform.user",
          "aiplatform.user" in _guidance(vertex, "PermissionDenied: 403 denied"))
    check("429 points at quota",
          "Quota" in _guidance(vertex, "ResourceExhausted: 429 quota exceeded"))
    check("401 points at credentials",
          "Authentication failed" in _guidance(vertex, "401 unauthenticated"))

    print("\n-- Health must fail when the model is unreachable --")
    env = {k: v for k, v in os.environ.items() if k not in PROXY_VARS}
    env.update(
        PROBE_FIXTURE="fail",
        GCP_PROJECT_ID="stub-project",
        ADK_BACKEND="api_key",
        GEMINI_API_KEY="stub-key",
        PYTHONPATH=str(REPO_ROOT),
    )
    log = (REPO_ROOT / ".local_logs")
    log.mkdir(exist_ok=True)
    handle = (log / "test_probe.log").open("wb")
    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "tests" / "serve_stubbed.py"),
         "--port", str(PORT)],
        env=env, cwd=REPO_ROOT, stdout=handle, stderr=subprocess.STDOUT,
    )
    try:
        client = httpx.Client(base_url=f"http://127.0.0.1:{PORT}",
                              timeout=20.0, trust_env=False)
        ready = False
        for _ in range(60):
            try:
                if client.get("/healthz").status_code == 200:
                    ready = True
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)
        check("service started", ready)
        if not ready:
            print((log / "test_probe.log").read_text(errors="replace")[:1500])
            return 1

        health = client.get("/api/agents/health").json()
        check("all_ok is false", health["all_ok"] is False, json.dumps(health)[:200])
        check("no agent reports ok",
              not any(a["ok"] for a in health["agents"]),
              json.dumps([a["ok"] for a in health["agents"]]))
        check("model_probe is included", "model_probe" in health)
        probe = health.get("model_probe", {})
        check("probe reports the failure", probe.get("ok") is False)
        check("probe carries actionable guidance", bool(probe.get("guidance")))
        check("probe lists usable models",
              "gemini-3.6-flash" in probe.get("models_available_to_this_project", []),
              json.dumps(probe)[:200])
        check("agent detail points at the probe",
              all("model_probe" in str(a.get("detail", "")) for a in health["agents"]),
              json.dumps(health["agents"])[:200])

        # This is the property that matters: deploy.sh keys off all_ok, so a
        # bad model must stop the deploy rather than surface on stage.
        blob = json.dumps(health)
        check("deploy.sh would treat this as a failed deploy",
              '"all_ok": false' in blob or '"all_ok":false' in blob)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        handle.close()

    print()
    if failures:
        print(f"\033[1;31m{len(failures)} check(s) failed:\033[0m")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\033[1;32mAll checks passed.\033[0m\n")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(REPO_ROOT))
    sys.exit(main())
