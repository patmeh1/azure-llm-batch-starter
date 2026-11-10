from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .prompt_assets import read_prompt

CATEGORIES = ["shipping", "billing", "access", "product", "other"]
WORKFLOW_VERSION = "4.0"
DEFAULT_PROMPT = read_prompt("simple-v1.md")
DEFAULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "priority": {"type": "string", "enum": ["low", "normal", "high"]},
        "summary": {"type": "string"},
    },
    "required": ["category", "priority", "summary"],
    "additionalProperties": False,
}


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: str = ""
    online_deployment: str = "online"
    batch_deployment: str = "batch"
    model_name: str = "gpt-5.4-mini"
    model_version: str = "2026-03-17"
    region: str = "southcentralus"
    auth_mode: Literal["cli", "managed_identity", "default"] = "cli"
    managed_identity_client_id: str | None = None
    prompt: str = DEFAULT_PROMPT
    output_schema: dict[str, Any] = Field(default_factory=lambda: DEFAULT_SCHEMA.copy())
    max_output_tokens: int = Field(default=1024, ge=64, le=32768)
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = "low"
    context_limit: int = Field(default=128000, ge=1024)
    rpm: int = Field(default=60, ge=1)
    tpm: int = Field(default=150000, ge=1000)
    concurrency: int = Field(default=2, ge=1, le=16)
    max_attempts: int = Field(default=3, ge=1, le=6)
    request_timeout_seconds: float = Field(default=90, gt=0, le=300)
    run_timeout_seconds: float = Field(default=3600, gt=0)
    cloud_timeout_seconds: int = Field(default=86400, ge=300, le=172800)
    poll_seconds: float = Field(default=60, ge=1)
    max_poll_seconds: float = Field(default=300, ge=1)
    records_per_batch: int = Field(default=10000, ge=1, le=10000)
    max_batch_bytes: int = Field(default=100_000_000, ge=1024, le=200_000_000)
    max_enqueued_tokens: int = Field(default=2_000_000, ge=1000)
    max_active_batches: int = Field(default=2, ge=1, le=8)
    max_run_cost_usd: float = Field(default=5, gt=0)
    input_price_per_million: float = Field(default=0.825, ge=0)
    cached_input_price_per_million: float = Field(default=0.0825, ge=0)
    output_price_per_million: float = Field(default=4.95, ge=0)
    batch_discount: float = Field(default=0.5, gt=0, le=1)
    observe_service_throttling: bool = False
    observation_profile: Literal["bounded-burst", "full-reproduction"] = "bounded-burst"
    max_http_attempts: int = Field(default=10000, ge=1)
    storage_account: str = ""
    scheduler_endpoint: str = ""
    task_hub: str = ""
    container: str = "batch-artifacts"
    queue_name: str = "batch-requests"
    table_name: str = "BatchRuns"
    coordination_dir: str = str(Path.home() / ".llm-batch-starter" / "locks")

    @model_validator(mode="after")
    def validate_settings(self) -> Settings:
        if self.endpoint:
            parsed = urlparse(self.endpoint)
            host = parsed.hostname or ""
            if (
                parsed.scheme != "https"
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or not host.endswith(
                    (".openai.azure.com", ".cognitiveservices.azure.com", ".services.ai.azure.com")
                )
            ):
                raise ValueError(
                    "endpoint must be an HTTPS Azure OpenAI endpoint without credentials"
                )
        if self.max_output_tokens >= self.context_limit:
            raise ValueError("output budget must be smaller than the context limit")
        if self.max_poll_seconds < self.poll_seconds:
            raise ValueError("max_poll_seconds must be at least poll_seconds")
        if self.observation_profile == "full-reproduction":
            if (
                not self.observe_service_throttling
                or self.model_name != "gpt-5-mini"
                or self.model_version != "2025-08-07"
                or self.max_http_attempts > 5100
                or self.run_timeout_seconds > 36000
                or self.max_attempts > 3
                or self.concurrency > 5
                or self.max_run_cost_usd > 75
            ):
                raise ValueError(
                    "Full reproduction requires the pinned model and bounded attempts/time/cost"
                )
        elif self.observe_service_throttling and (
            self.max_http_attempts > 100 or self.run_timeout_seconds > 600
        ):
            raise ValueError("unpaced observation requires at most 100 attempts and 600 seconds")
        return self

    @property
    def base_url(self) -> str:
        if not self.endpoint:
            raise ValueError("Set AZURE_OPENAI_ENDPOINT or endpoint in the private configuration")
        return self.endpoint.rstrip("/").removesuffix("/openai/v1") + "/openai/v1/"

    def fingerprint(self) -> str:
        excluded = {"scheduler_endpoint", "task_hub"}
        if self.observation_profile == "bounded-burst":
            excluded.add("observation_profile")
        return hashlib.sha256(self.model_dump_json(exclude=excluded).encode()).hexdigest()

    def public_metadata(self) -> dict[str, Any]:
        fields = (
            "model_name",
            "model_version",
            "region",
            "rpm",
            "tpm",
            "concurrency",
            "max_output_tokens",
            "reasoning_effort",
            "max_run_cost_usd",
            "observe_service_throttling",
            "observation_profile",
            "input_price_per_million",
            "cached_input_price_per_million",
            "output_price_per_million",
            "batch_discount",
        )
        return {key: getattr(self, key) for key in fields}


def load_settings(path: Path | None = None, **overrides: Any) -> Settings:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path else {}
    mapping = {
        "AZURE_OPENAI_ENDPOINT": "endpoint",
        "AZURE_OPENAI_ONLINE_DEPLOYMENT": "online_deployment",
        "AZURE_OPENAI_BATCH_DEPLOYMENT": "batch_deployment",
        "AZURE_STORAGE_ACCOUNT": "storage_account",
        "DTS_ENDPOINT": "scheduler_endpoint",
        "TASKHUB_NAME": "task_hub",
        "AZURE_OPENAI_MODEL_NAME": "model_name",
        "AZURE_OPENAI_MODEL_VERSION": "model_version",
        "AZURE_LOCATION": "region",
        "LLM_AUTH_MODE": "auth_mode",
        "AZURE_CLIENT_ID": "managed_identity_client_id",
    }
    for env, key in mapping.items():
        if os.environ.get(env):
            data[key] = os.environ[env]
    data.update({key: value for key, value in overrides.items() if value is not None})
    return Settings.model_validate(data)


def credential(settings: Settings) -> Any:
    from azure.identity import AzureCliCredential, DefaultAzureCredential, ManagedIdentityCredential

    if settings.auth_mode == "cli":
        return AzureCliCredential()
    if settings.auth_mode == "managed_identity":
        if settings.managed_identity_client_id:
            return ManagedIdentityCredential(client_id=settings.managed_identity_client_id)
        return ManagedIdentityCredential()
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)
