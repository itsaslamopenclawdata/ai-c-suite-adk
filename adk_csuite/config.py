"""Environment-driven configuration.

Nothing is hard-coded: project, region, model, thresholds, timeouts, active
roles and the model backend are all injected. Secrets are never read from the
repository; when the API-key backend is used the value comes from Secret
Manager at runtime.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration for the single ADK C-Suite service."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Platform ---
    gcp_project_id: str = Field(default="")
    gcp_region: str = Field(default="us-central1")
    log_level: str = Field(default="INFO")
    port: int = Field(default=8080)

    # --- Model ---
    model_name: str = Field(default="gemini-2.5-flash")
    model_temperature: float = Field(default=0.2)
    model_max_output_tokens: int = Field(default=4096)

    # How ADK reaches Gemini.
    #   vertex  -> Vertex AI via the service account. No API key anywhere.
    #   api_key -> Gemini API key, resolved from Secret Manager at runtime.
    adk_backend: Literal["vertex", "api_key"] = Field(default="vertex")
    # Vertex region, kept separate because Cloud Run may run somewhere Vertex
    # does not serve this model.
    vertex_location: str = Field(default="us-central1")

    gemini_api_key_secret: str = Field(default="ADK_GEMINI_API_KEY")
    gemini_api_key_secret_version: str = Field(default="latest")
    gemini_api_key: str = Field(default="")

    # --- Agents ---
    agents_dir: str = Field(default="agents")
    context_dir: str = Field(default="context")
    active_roles_csv: str = Field(default="cfo,cso,cmo,chro,cto")
    agent_timeout_seconds: float = Field(default=180.0)
    agent_max_retries: int = Field(default=1)

    # --- Routing ---
    routing_mode: Literal["llm", "all"] = Field(default="llm")
    routing_min_roles: int = Field(default=1)
    routing_max_roles: int = Field(default=5)

    # --- Human in the loop ---
    hitl_enabled: bool = Field(default=True)
    hitl_plan_approval: bool = Field(default=True)
    hitl_confidence_floor: float = Field(default=0.75)
    hitl_require_citations: bool = Field(default=True)
    hitl_final_signoff: bool = Field(default=True)
    hitl_timeout_seconds: float = Field(default=900.0)

    # --- Decision log ---
    decision_log_backend: Literal["memory", "firestore"] = Field(default="memory")
    firestore_collection: str = Field(default="adk_csuite_decision_log")
    firestore_database: str = Field(default="(default)")

    # --- Uploads ---
    max_upload_bytes: int = Field(default=1_048_576)
    allowed_upload_extensions_csv: str = Field(default=".md,.markdown,.txt,.csv")

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @property
    def active_roles(self) -> list[str]:
        return [r.strip().lower() for r in self.active_roles_csv.split(",") if r.strip()]

    @property
    def allowed_upload_extensions(self) -> set[str]:
        return {
            e.strip().lower()
            for e in self.allowed_upload_extensions_csv.split(",")
            if e.strip()
        }


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()
