"""Public Shopper Assistant API transport for behavioral evaluation."""

from __future__ import annotations

import time
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit

import requests

from evaluation.clients.wxo import AgentRunResult
from evaluation.contexts import context_to_shopper_api

PUBLIC_API_RESPONSE_KEY = "_shopper_api_response"
PUBLIC_API_TRANSPORT_ATTEMPTS_KEY = "_shopper_api_transport_attempts"
GENERIC_WXO_EXECUTION_FAILURE_RESPONSE = (
    "I’m unable to complete that request right now. Please try again."
)
TRANSIENT_QUERY_STATUSES = frozenset({502, 503, 504})
MAX_QUERY_ATTEMPTS = 3


def is_generic_wxo_execution_failure(response_text: str) -> bool:
    """Recognize the agent's exact fail-closed response without fuzzy text matching."""
    return response_text.strip() == GENERIC_WXO_EXECUTION_FAILURE_RESPONSE


def _assert_api_url(url: str, *, allow_remote: bool) -> None:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").casefold()
    if allow_remote:
        if (
            parsed.scheme != "https"
            or not hostname.endswith(".appdomain.cloud")
            or "shopper-assistant-api" not in hostname
        ):
            raise ValueError(f"Remote evaluation refuses non-Shopper-API URL {url!r}")
        return
    if parsed.scheme != "http" or hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"Local evaluation refuses non-loopback Shopper API URL {url!r}")


class ShopperApiClient:
    """Reuse one authenticated public-API session per evaluation conversation."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str,
        timeout: int,
        allow_remote: bool,
    ) -> None:
        _assert_api_url(base_url, allow_remote=allow_remote)
        if not api_key:
            raise ValueError("Shopper API evaluation requires a client API key")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": api_key})
        self._tokens: dict[tuple[str, str], str] = {}

    def _token(self, session_id: str, prospect_type: str) -> str:
        subject_type = "authenticated_member" if prospect_type == "member" else "anonymous"
        token_key = (session_id, subject_type)
        if token_key in self._tokens:
            return self._tokens[token_key]
        response = self.session.post(
            f"{self.base_url}/api/auth/login",
            json={"subject_type": subject_type},
            timeout=min(self.timeout, 30),
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise RuntimeError("Shopper API login omitted its access token")
        self._tokens[token_key] = token
        return token

    def run(
        self,
        case_id: str,
        query: str,
        context: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> AgentRunResult:
        session_key = session_id or case_id
        public_context = context_to_shopper_api(context)
        token = self._token(session_key, str(context.get("prospect_type", "prospect")))
        started = time.monotonic()
        response = None
        attempts = 0
        for attempts in range(1, MAX_QUERY_ATTEMPTS + 1):
            try:
                response = self.session.post(
                    f"{self.base_url}/api/query",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"query": query, "context": public_context},
                    timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout):
                if attempts == MAX_QUERY_ATTEMPTS:
                    raise
                time.sleep(0.5 * attempts)
                continue
            if response.status_code not in TRANSIENT_QUERY_STATUSES:
                break
            if attempts == MAX_QUERY_ATTEMPTS:
                break
            response.close()
            time.sleep(0.5 * attempts)

        if response is None:  # pragma: no cover - the bounded loop always returns or raises
            raise RuntimeError("Shopper API query produced no HTTP response")
        response.raise_for_status()
        duration = time.monotonic() - started
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise RuntimeError("Shopper API returned an invalid query response")
        answer = payload.get("response")
        text = answer.get("text") if isinstance(answer, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("Shopper API returned no generated response")
        call_info = payload.get("call_info")
        run_id = call_info.get("run_id") if isinstance(call_info, dict) else ""
        run_id = str(run_id or "")
        return AgentRunResult(
            submitted_run_id=run_id,
            run_id=run_id,
            duration_seconds=duration,
            response_text=text,
            request_context=public_context,
            context={
                PUBLIC_API_RESPONSE_KEY: payload,
                PUBLIC_API_TRANSPORT_ATTEMPTS_KEY: attempts,
            },
            tool_calls=[],
            tool_responses=[],
        )

    def close(self) -> None:
        for token in self._tokens.values():
            with suppress(requests.RequestException):
                self.session.post(
                    f"{self.base_url}/api/auth/logout",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=min(self.timeout, 20),
                )
        self.session.close()
