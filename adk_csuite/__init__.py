"""AI C-Suite — Google ADK edition.

Same demo as Option 1, different architecture: every agent here is a
``google.adk.agents.LlmAgent`` composed in-process by ADK Runners, rather than
five independent Cloud Run services called over HTTP.
"""

__all__ = ["config", "models", "roles", "agents", "tools", "engine", "decision_log"]
