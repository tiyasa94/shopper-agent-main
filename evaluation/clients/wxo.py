"""Reusable local-only watsonx Orchestrate agent runner and trace parser."""

import json
import re
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

import requests
from ibm_watsonx_orchestrate.client.client import Client
from ibm_watsonx_orchestrate.client.credentials import Credentials

SEARCH_TOOL = "search_plans"
GENERAL_SEARCH_TOOL = "search_general_documents"
PLAN_DETAILS_TOOL = "get_plan_details"
TURN_RESULT_CONTEXT_KEY = "_turn_result"
REDACTED = "[REDACTED]"
_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"\b\d{9}\b"),
    re.compile(r"\b(?:4\d{12}(?:\d{3})?|5[1-5]\d{14}|3[47]\d{13})\b"),
    re.compile(r"\bMRN[\s#:]*\d+\b", re.IGNORECASE),
)


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class ToolResponse:
    tool_call_id: str
    name: str
    content: Any


@dataclass
class AgentRunResult:
    submitted_run_id: str
    run_id: str
    duration_seconds: float
    response_text: str
    request_context: dict[str, Any]
    context: dict[str, Any]
    tool_calls: list[ToolCall]
    tool_responses: list[ToolResponse]

    def calls_for(self, name: str) -> list[ToolCall]:
        return [call for call in self.tool_calls if call.name == name]

    def responses_for(self, name: str) -> list[ToolResponse]:
        return [response for response in self.tool_responses if response.name == name]

    def turn_result(self) -> dict[str, Any]:
        """Return the trusted pre-invoke result exposed in final context variables."""

        content: Any = self.context.get(TURN_RESULT_CONTEXT_KEY, {})
        for _ in range(2):
            if not isinstance(content, str):
                break
            with suppress(ValueError):
                content = json.loads(content)
                continue
            break
        return content if isinstance(content, dict) else {}

    def search_envelope(self) -> dict[str, Any] | None:
        """Return the single scoped-search response from the trace."""

        responses = self.responses_for(SEARCH_TOOL) + self.responses_for(GENERAL_SEARCH_TOOL)
        if len(responses) != 1:
            return None
        content = responses[0].content
        if isinstance(content, str):
            with suppress(ValueError):
                content = json.loads(content)
        return content if isinstance(content, dict) else None

    def plan_details_envelope(self) -> dict[str, Any] | None:
        """Return the single structured plan-details response from the trace."""

        responses = self.responses_for(PLAN_DETAILS_TOOL)
        if len(responses) != 1:
            return None
        content = responses[0].content
        if isinstance(content, str):
            with suppress(ValueError):
                content = json.loads(content)
        return content if isinstance(content, dict) else None

    def trace_summary(self) -> str:
        return " -> ".join(call.name for call in self.tool_calls) or "<no tool calls>"


def assert_loopback_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError(f"Local E2E refuses non-loopback Orchestrate URL {url!r}")


def assert_remote_wxo_url(url: str) -> None:
    """Allow only IBM Cloud WXO HTTPS endpoints for explicitly opted-in remote tests."""
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not hostname.endswith(".watson-orchestrate.cloud.ibm.com"):
        raise ValueError(f"Remote E2E refuses non-WXO Orchestrate URL {url!r}")


class WxoClient:
    def __init__(
        self,
        base_url: str,
        agent_name: str,
        timeout: int = 120,
        *,
        allow_remote: bool = False,
        api_key: str | None = None,
        agent_id: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        if allow_remote:
            assert_remote_wxo_url(self.base_url)
            if not api_key:
                raise ValueError("Remote E2E requires a WXO API key")
        else:
            assert_loopback_url(self.base_url)
        self.agent_name = agent_name
        self.allow_remote = allow_remote
        self.timeout = timeout
        credentials = Credentials(
            url=self.base_url,
            api_key=api_key,
            username=None,
            password=None,
            iam_url=None,
            auth_type=None,
        )
        token = Client(credentials).token
        if not token:
            raise RuntimeError("Orchestrate did not issue an access token")
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        self.agent_id = agent_id or self._resolve_agent_id()
        self._thread_ids: dict[str, str] = {}

    def _resolve_agent_id(self) -> str:
        response = self.session.get(
            f"{self.base_url}/v1/orchestrate/agents",
            params={"names": self.agent_name},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        agents = (
            payload if isinstance(payload, list) else payload.get("data", payload.get("agents", []))
        )
        for agent in agents:
            if agent.get("name") == self.agent_name:
                return agent["id"]
        raise RuntimeError(f"Agent {self.agent_name!r} is not imported into Orchestrate")

    def _wait_for_run(self, run_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                response = self.session.get(
                    f"{self.base_url}/v1/orchestrate/runs/{run_id}", timeout=20
                )
            except (requests.ConnectionError, requests.Timeout):
                time.sleep(0.5)
                continue
            if response.status_code in {502, 503, 504}:
                response.close()
                time.sleep(0.5)
                continue
            response.raise_for_status()
            run = response.json()
            if run.get("status") == "completed":
                return run
            if run.get("status") in {"failed", "cancelled"}:
                raise RuntimeError(
                    f"Local agent run {run_id} ended as {run.get('status')}: "
                    f"{run.get('last_error')}"
                )
            time.sleep(1)
        raise TimeoutError(f"Local agent run {run_id} did not complete within {self.timeout}s")

    def run(
        self,
        case_id: str,
        query: str,
        context: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> AgentRunResult:
        isolated_context = dict(context)
        suffix = uuid4().hex[:10]
        session_key = session_id or f"{case_id}-{suffix}"
        isolated_context["session_id"] = (
            str(uuid5(NAMESPACE_URL, session_key))
            if isolated_context.get("application_plan_quote_inputs")
            else session_key
        )
        isolated_context["request_id"] = f"{case_id}-{suffix}"

        payload: dict[str, Any] = {
            "agent_id": self.agent_id,
            "message": {"role": "user", "content": query},
            "context_variables": isolated_context,
        }
        prior_thread_id = self._thread_ids.get(session_key)
        if prior_thread_id:
            payload["thread_id"] = prior_thread_id

        started = time.monotonic()
        response = self.session.post(
            f"{self.base_url}/v1/orchestrate/runs",
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        submission = response.json()
        submitted_run_id = str(submission["run_id"])
        initial = self._wait_for_run(submitted_run_id)
        target_run_id = initial.get("result", {}).get("data", {}).get("target_run_id")
        final = self._wait_for_run(target_run_id) if target_run_id else initial
        returned_thread_id = _thread_id(final) or _thread_id(initial) or _thread_id(submission)
        if returned_thread_id:
            if prior_thread_id and returned_thread_id != prior_thread_id:
                raise RuntimeError(
                    "Orchestrate changed thread IDs within logical session "
                    f"{session_key!r}: {prior_thread_id!r} -> {returned_thread_id!r}"
                )
            self._thread_ids[session_key] = returned_thread_id
        elif session_id is not None and not prior_thread_id:
            raise RuntimeError(
                "Orchestrate did not return a thread_id for logical session "
                f"{session_key!r}; multi-turn continuity cannot be guaranteed"
            )
        result = _parse_result(
            final,
            time.monotonic() - started,
            request_context=isolated_context,
            submitted_run_id=submitted_run_id,
        )
        return result


def _message(run: dict[str, Any]) -> dict[str, Any]:
    return run.get("result", {}).get("data", {}).get("message", {})


def _thread_id(run: dict[str, Any]) -> str:
    """Return the actual WXO thread identifier from a run or submission payload."""
    data = run.get("result", {}).get("data", {})
    message = data.get("message", {}) if isinstance(data, dict) else {}
    candidates = (
        message.get("thread_id") if isinstance(message, dict) else None,
        data.get("thread_id") if isinstance(data, dict) else None,
        run.get("thread_id"),
    )
    return next((str(value) for value in candidates if value), "")


def _step_details(run: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        detail
        for step in _message(run).get("step_history", [])
        for detail in step.get("step_details", [])
    ]


def _parse_tool_calls(run: dict[str, Any]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    seen: set[str] = set()
    for detail in _step_details(run):
        raw_calls = detail.get("tool_calls", []) if detail.get("type") == "tool_calls" else []
        if detail.get("type") == "tool_call":
            raw_calls.append(detail)
        for raw in raw_calls:
            call_id = str(raw.get("id") or raw.get("tool_call_id") or "")
            if not call_id or call_id in seen:
                continue
            seen.add(call_id)
            calls.append(
                ToolCall(id=call_id, name=str(raw.get("name", "")), args=raw.get("args", {}))
            )
    return calls


def _parse_tool_responses(run: dict[str, Any]) -> list[ToolResponse]:
    responses: list[ToolResponse] = []
    seen: set[str] = set()
    for detail in _step_details(run):
        if detail.get("type") != "tool_response":
            continue
        call_id = str(detail.get("tool_call_id") or "")
        if not call_id or call_id in seen:
            continue
        seen.add(call_id)
        content = detail.get("content")
        if isinstance(content, str):
            with suppress(ValueError):
                content = json.loads(content)
        responses.append(
            ToolResponse(
                tool_call_id=call_id,
                name=str(detail.get("name", "")),
                content=content,
            )
        )
    return responses


def _parse_result(
    run: dict[str, Any],
    duration: float,
    request_context: dict[str, Any] | None = None,
    submitted_run_id: str | None = None,
) -> AgentRunResult:
    message = _message(run)
    text = "\n".join(
        item.get("text", "")
        for item in message.get("content", [])
        if isinstance(item, dict) and item.get("text")
    ).strip()
    return AgentRunResult(
        submitted_run_id=submitted_run_id or str(run.get("id", "")),
        run_id=str(run.get("id", "")),
        duration_seconds=round(duration, 3),
        response_text=text,
        request_context=request_context or {},
        context=message.get("context", {}) or {},
        tool_calls=_parse_tool_calls(run),
        tool_responses=_parse_tool_responses(run),
    )


def _sanitized(value: Any, key: str = "") -> Any:
    normalized_key = key.casefold().replace("-", "_")
    if any(
        sensitive in normalized_key
        for sensitive in ("password", "secret", "token", "api_key", "apikey")
    ):
        return REDACTED
    if isinstance(value, dict):
        return {str(item_key): _sanitized(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_sanitized(item) for item in value]
    if isinstance(value, str):
        sanitized = value
        for pattern in _SENSITIVE_VALUE_PATTERNS:
            sanitized = pattern.sub(REDACTED, sanitized)
        return sanitized
    return value


def write_artifact(
    case_id: str,
    query: str,
    expectation: dict[str, Any],
    result: AgentRunResult,
    root: Path,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{case_id}.json"
    artifact = {
        "case_id": case_id,
        "query": query,
        "expectation": expectation,
        "result": asdict(result),
    }
    path.write_text(json.dumps(_sanitized(artifact), indent=2, sort_keys=True, default=str) + "\n")
    return path
