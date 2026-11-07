from __future__ import annotations

from typing import Any

from azure.identity import get_bearer_token_provider
from openai import OpenAI

from .config import Settings, credential
from .prepare import request_body
from .synthetic import Record


class AzureProvider:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.identity = credential(settings)
        self.client = OpenAI(
            base_url=settings.base_url,
            api_key=get_bearer_token_provider(
                self.identity, "https://cognitiveservices.azure.com/.default"
            ),
            max_retries=0,
            timeout=settings.request_timeout_seconds,
        )

    def close(self) -> None:
        self.client.close()
        self.identity.close()

    def complete(self, record: Record) -> tuple[dict[str, Any], dict[str, str]]:
        body = request_body(record, self.settings, batch=False)
        raw = self.client.chat.completions.with_raw_response.create(**body)
        return raw.parse().model_dump(mode="json"), dict(raw.headers)

    def upload(self, payload: bytes, filename: str) -> str:
        return self.client.files.create(
            file=(filename, payload, "application/jsonl"), purpose="batch"
        ).id

    def submit(self, file_id: str, submission_key: str) -> dict[str, Any]:
        return self.client.batches.create(
            input_file_id=file_id,
            endpoint="/v1/chat/completions",
            completion_window="24h",
            metadata={"submission_key": submission_key},
        ).model_dump(mode="json")

    def poll(self, job_id: str) -> dict[str, Any]:
        return self.client.batches.retrieve(job_id).model_dump(mode="json")

    def find_submission(self, submission_key: str) -> dict[str, Any] | None:
        matches = []
        for index, batch in enumerate(self.client.batches.list(limit=100)):
            if index >= 1000:
                raise RuntimeError(
                    "Submission reconciliation exceeded 1000 jobs; operator review required"
                )
            if (batch.metadata or {}).get("submission_key") == submission_key:
                matches.append(batch.model_dump(mode="json"))
        if len(matches) > 1:
            raise RuntimeError("Multiple matching remote jobs; operator review required")
        return matches[0] if matches else None

    def download(self, file_id: str) -> str:
        return self.client.files.content(file_id).text

    def cancel(self, job_id: str) -> dict[str, Any]:
        return self.client.batches.cancel(job_id).model_dump(mode="json")


def rate_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key.lower(): value
        for key, value in headers.items()
        if key.lower().startswith("x-ratelimit-")
        or key.lower() in {"retry-after", "retry-after-ms", "apim-request-id", "x-request-id"}
    }
