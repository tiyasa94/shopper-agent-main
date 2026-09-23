"""Direct WXO gateway client for strictly validated JSON generation."""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import requests
from pydantic import BaseModel, ValidationError

from shared.wxo_http import iam_token as _iam_token
from shared.wxo_http import post as _post

REQUIRED_GENERATION_CONFIG = (
    "WXO_API_URL",
    "WXO_API_KEY",
    "WXO_MODEL_ID",
    "WXO_IAM_URL",
)

GenerationFailureCategory = Literal[
    "configuration_error",
    "iam_error",
    "http_4xx",
    "http_429",
    "http_5xx",
    "timeout",
    "connection_error",
    "invalid_response_envelope",
    "non_json_model_output",
    "schema_validation_failed",
    "truncated_output",
]
ParseFailureCategory = Literal["non_json_model_output", "schema_validation_failed"]


@dataclass(frozen=True)
class GenerationTelemetry:
    """Content-free facts about one structured-generation operation."""

    attempts: int
    last_http_status: int | None = None
    retry_after_honored: bool = False
    finish_reason: str | None = None
    missing_configuration: tuple[str, ...] = ()
    failure_category: GenerationFailureCategory | None = None


@dataclass(frozen=True)
class GenerationResult[OutputT: BaseModel]:
    """A strictly validated model output and its operational telemetry."""

    value: OutputT
    telemetry: GenerationTelemetry


class StructuredGenerationError(RuntimeError):
    """Content-free failure after the configured generation policy is exhausted."""

    def __init__(self, telemetry: GenerationTelemetry):
        self.telemetry = telemetry
        super().__init__(f"structured generation failed: {telemetry.failure_category or 'unknown'}")


class _InvalidResponseEnvelope(ValueError):
    pass


class _OutputParseError(ValueError):
    def __init__(self, category: ParseFailureCategory):
        self.category: ParseFailureCategory = category
        super().__init__(category)


@dataclass(frozen=True)
class _Completion:
    text: str
    finish_reason: str | None


def _completion(body: Any) -> _Completion:
    """Validate the gateway envelope and extract its first completion."""
    if not isinstance(body, dict):
        raise _InvalidResponseEnvelope("generation response is not an object")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise _InvalidResponseEnvelope("generation response omitted choices")
    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    normalized_finish_reason = finish_reason if isinstance(finish_reason, str) else None
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise _InvalidResponseEnvelope("generation response omitted message content")
    return _Completion(text=content.strip(), finish_reason=normalized_finish_reason)


def _json_object(text: str) -> dict[str, Any]:
    """Decode a JSON object, allowing an enclosing Markdown code fence."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise _OutputParseError("non_json_model_output") from exc
    if not isinstance(value, dict):
        raise _OutputParseError("schema_validation_failed")
    return value


def _parse_output[OutputT: BaseModel](text: str, output_model: type[OutputT]) -> OutputT:
    """Validate model output while preserving distinct JSON and schema failures."""
    try:
        return output_model.model_validate(_json_object(text))
    except ValidationError as exc:
        raise _OutputParseError("schema_validation_failed") from exc


def _failure_for_finish_reason(
    category: ParseFailureCategory,
    finish_reason: str | None,
) -> GenerationFailureCategory:
    """Distinguish truncated output from other parsing failures."""
    if (finish_reason or "").strip().casefold() in {
        "length",
        "max_tokens",
        "max_output_tokens",
    }:
        return "truncated_output"
    return category


def generate_structured[OutputT: BaseModel](
    *,
    system_prompt: str,
    input_payload: dict[str, Any],
    config: dict[str, Any],
    output_model: type[OutputT],
    max_tokens: int,
    max_attempts: int,
    timeout_seconds: float,
    max_retry_delay_seconds: float = 5.0,
    token_provider: Callable[[str, str], str] | None = None,
    post: Callable[..., requests.Response] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> GenerationResult[OutputT]:
    """Validate a JSON completion with bounded retries and one 401 token refresh.

    Telemetry counts every inference send, including the authentication retry.
    Configuration, authentication, and exhausted generation failures raise
    StructuredGenerationError without exposing request content or credentials.
    """

    if max_tokens < 1 or max_attempts < 1 or timeout_seconds <= 0:
        raise ValueError("generation limits must be positive")
    if max_retry_delay_seconds < 0:
        raise ValueError("generation retry delay cannot be negative")

    post = post or _post
    sleeper = sleeper or time.sleep

    configured_values = {
        key: value.strip() if isinstance((value := config.get(key)), str) else ""
        for key in REQUIRED_GENERATION_CONFIG
    }
    missing_configuration = tuple(
        key for key in REQUIRED_GENERATION_CONFIG if not configured_values[key]
    )
    if missing_configuration:
        raise StructuredGenerationError(
            GenerationTelemetry(
                attempts=0,
                missing_configuration=missing_configuration,
                failure_category="configuration_error",
            )
        )

    base_url = configured_values["WXO_API_URL"].rstrip("/")
    api_key = configured_values["WXO_API_KEY"]
    model_id = configured_values["WXO_MODEL_ID"]
    iam_url = configured_values["WXO_IAM_URL"]

    request_body: dict[str, Any] = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(input_payload, ensure_ascii=False, separators=(",", ":")),
            },
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_completion_tokens": max_tokens,
    }
    if "gpt-oss" in model_id.casefold():
        request_body["reasoning_effort"] = "low"

    try:
        token = (
            token_provider(api_key, iam_url)
            if token_provider
            else _iam_token(api_key, iam_url, instance_url=base_url)
        )
    except Exception as exc:
        raise StructuredGenerationError(
            GenerationTelemetry(attempts=0, failure_category="iam_error")
        ) from exc

    url = f"{base_url}/v1/orchestrate/gateway/model/chat/completions"
    last_category: GenerationFailureCategory = "connection_error"
    last_http_status: int | None = None
    last_finish_reason: str | None = None
    retry_after_honored = False

    auth_refreshed = False
    attempts_sent = 0
    for attempt in range(1, max_attempts + 1):
        retry_delay = 0.25 * (2 ** (attempt - 1))
        try:
            while True:
                attempts_sent += 1
                response = post(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    json=request_body,
                    timeout=timeout_seconds,
                )
                if response.status_code != 401 or auth_refreshed:
                    break
                auth_refreshed = True
                response.close()
                try:
                    token = (
                        token_provider(api_key, iam_url)
                        if token_provider
                        else _iam_token(
                            api_key, iam_url, instance_url=base_url, rejected_token=token
                        )
                    )
                except Exception as exc:
                    raise StructuredGenerationError(
                        GenerationTelemetry(
                            attempts=attempts_sent,
                            last_http_status=401,
                            failure_category="iam_error",
                        )
                    ) from exc
            last_http_status = response.status_code
            if last_http_status >= 400:
                response.raise_for_status()
            try:
                body = response.json()
            except (json.JSONDecodeError, requests.exceptions.JSONDecodeError) as exc:
                raise _InvalidResponseEnvelope("generation response body is not JSON") from exc
            completion = _completion(body)
            last_finish_reason = completion.finish_reason
            try:
                value = _parse_output(completion.text, output_model)
            except _OutputParseError as exc:
                last_category = _failure_for_finish_reason(exc.category, completion.finish_reason)
            else:
                return GenerationResult(
                    value=value,
                    telemetry=GenerationTelemetry(
                        attempts=attempts_sent,
                        last_http_status=last_http_status,
                        retry_after_honored=retry_after_honored,
                        finish_reason=completion.finish_reason,
                    ),
                )
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else last_http_status or 0
            last_http_status = int(status) if status else None
            if status == 429:
                last_category = "http_429"
                retry_after = (
                    exc.response.headers.get("Retry-After", "") if exc.response is not None else ""
                )
                try:
                    retry_delay = max(float(retry_after), 1.0 * (2 ** (attempt - 1)))
                    retry_after_honored = bool(str(retry_after).strip())
                except (TypeError, ValueError):
                    retry_delay = 1.0 * (2 ** (attempt - 1))
            elif status >= 500:
                last_category = "http_5xx"
            else:
                raise StructuredGenerationError(
                    GenerationTelemetry(
                        attempts=attempts_sent,
                        last_http_status=last_http_status,
                        failure_category="http_4xx",
                    )
                ) from exc
        except requests.Timeout:
            last_category = "timeout"
        except requests.RequestException:
            last_category = "connection_error"
        except _InvalidResponseEnvelope:
            last_category = "invalid_response_envelope"

        if attempt < max_attempts:
            sleeper(min(retry_delay, max_retry_delay_seconds))

    raise StructuredGenerationError(
        GenerationTelemetry(
            attempts=attempts_sent,
            last_http_status=last_http_status,
            retry_after_honored=retry_after_honored,
            finish_reason=last_finish_reason,
            failure_category=last_category,
        )
    )
