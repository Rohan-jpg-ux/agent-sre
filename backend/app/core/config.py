"""Application settings.

Integration credentials deliberately do not live here. Provider secrets are
written to a secret backend (env, AWS Secrets Manager, Vault, Azure Key Vault,
GCP Secret Manager) and an integration row stores only the reference. Nothing in
this object is ever serialised to the browser.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SecretBackend(str, Enum):
    ENV = "env"
    AWS_SECRETS_MANAGER = "aws_secrets_manager"
    AZURE_KEY_VAULT = "azure_key_vault"
    HASHICORP_VAULT = "hashicorp_vault"
    GCP_SECRET_MANAGER = "gcp_secret_manager"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="OBS_", extra="ignore", case_sensitive=False
    )

    environment: str = "development"
    debug: bool = False

    database_url: str = "postgresql+asyncpg://obs:obs@localhost:5432/obsagent"
    redis_url: str = "redis://localhost:6379/0"

    secret_backend: SecretBackend = SecretBackend.ENV
    secret_backend_prefix: str = "obsagent/integrations"

    llm_model: str = "claude-sonnet-4-6"
    llm_max_output_tokens: int = 4096

    # Token budget guardrails. Raw telemetry is reduced deterministically before
    # any LLM call, so these caps bound cost rather than truncate evidence.
    max_evidence_in_prompt: int = 120
    max_log_rows_per_provider: int = 5000

    # Reliability envelope for connector calls.
    provider_timeout_seconds: float = 20.0
    provider_max_retries: int = 3
    provider_backoff_base_seconds: float = 0.5
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_reset_seconds: float = 60.0

    # Correlation defaults.
    neighbour_window_seconds: int = Field(
        default=120, description="Symmetric expansion around a matched trace timestamp"
    )

    demo_mode_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
