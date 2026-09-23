"""Shared synchronous audit-event contract for shopper tools and plugins.

This module deliberately does not register a watsonx Orchestrate tool. It is ordinary Python used
by the pre-invoke and retrieval paths.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditReceipt:
    """Acknowledgement returned only after the structured log call completes."""

    logged: bool
    escalation: dict[str, Any]


def normalize_audit_payload(escalation: str | dict[str, Any]) -> dict[str, Any]:
    """Parse and normalize the three fields accepted by the established audit tool."""

    try:
        payload = json.loads(escalation) if isinstance(escalation, str) else escalation
    except (ValueError, TypeError):
        payload = {
            "type": False,
            "identification": "parse_error",
            "description": str(escalation),
        }
    return {
        "type": bool(payload.get("type", False)),
        "identification": str(payload.get("identification", "")),
        "description": str(payload.get("description", "")),
    }


def rag_audit_payload(
    outcome: Literal["candidates", "insufficient", "error"] | str,
    *,
    retrieval_scope: Literal["plan", "general"] = "plan",
) -> dict[str, Any]:
    """Map a retrieval outcome to the established SOP audit taxonomy."""

    if outcome == "candidates":
        return {
            "type": False,
            "identification": "rag_response",
            "description": "Normal RAG response logging",
        }
    if outcome == "insufficient":
        description = (
            "Plan search returned ambiguous or insufficient evidence"
            if retrieval_scope == "plan"
            else "General search returned ambiguous or insufficient evidence"
        )
        return {
            "type": True,
            "identification": "rag_insufficient_context",
            "description": description,
        }
    description = (
        "RAG tool returned an error or empty result"
        if retrieval_scope == "plan"
        else "General document search returned an error or empty result"
    )
    return {
        "type": True,
        "identification": "Triggered by Guardrails - RAG Error",
        "description": description,
    }


def emit_audit_event(
    escalation: str | dict[str, Any],
    context: Any,
    *,
    request_id: str = "",
    event_logger: logging.Logger | Any = logger,
) -> AuditReceipt:
    """Synchronously emit the established ``escalation_event`` structured log.

    Logging errors are intentionally not caught: callers that require an audit must not expose a
    response that claims completion when the configured log sink rejected the event.
    """

    record = normalize_audit_payload(escalation)
    runtime_context = getattr(context, "request_context", None)
    ctx = runtime_context or {}
    session_id = str(ctx.get("session_id", "unknown"))
    # Prefer the API-owned request correlation ID. Plugin-local IDs are only a fallback when the
    # runtime context does not carry one.
    effective_request_id = str(ctx.get("request_id", "") or request_id)
    escalation_value = json.dumps(record)
    try:
        if runtime_context is not None:
            runtime_context["escalation"] = escalation_value
    except TypeError:
        event_logger.debug("WXO request context is immutable; escalation context was not updated")

    log_fn = event_logger.warning if record["type"] else event_logger.info
    log_fn(
        "escalation_event",
        extra={
            "session_id": session_id,
            "request_id": effective_request_id,
            "type": record["type"],
            "identification": record["identification"],
            "description": record["description"],
        },
    )
    return AuditReceipt(
        logged=True,
        escalation=record,
    )
