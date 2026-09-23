import json
from types import MappingProxyType
from unittest.mock import Mock

import pytest
from ibm_watsonx_orchestrate.run.context import AgentRun

from shared.audit import emit_audit_event, rag_audit_payload


def test_emit_audit_event_logs_complete_correlation_fields_synchronously():
    context = AgentRun(
        request_context={
            "session_id": "session-1",
            "request_id": "request-from-context",
        }
    )
    event_logger = Mock()
    payload = rag_audit_payload("candidates")

    receipt = emit_audit_event(payload, context, event_logger=event_logger)

    assert receipt.logged is True
    assert receipt.escalation == payload
    assert json.loads(context.request_context["escalation"]) == payload
    event_logger.info.assert_called_once_with(
        "escalation_event",
        extra={
            "session_id": "session-1",
            "request_id": "request-from-context",
            "type": False,
            "identification": "rag_response",
            "description": "Normal RAG response logging",
        },
    )


def test_emit_audit_event_supports_immutable_wxo_context_and_active_warning():
    context = AgentRun(request_context={})
    context.request_context = MappingProxyType(
        {"session_id": "session-2", "request_id": "request-2"}
    )
    event_logger = Mock()
    payload = rag_audit_payload("insufficient")

    receipt = emit_audit_event(payload, context, event_logger=event_logger)

    assert receipt.logged is True
    event_logger.debug.assert_called_once()
    event_logger.warning.assert_called_once()


def test_emit_audit_event_does_not_claim_completion_when_logger_fails():
    context = AgentRun(request_context={"session_id": "session-3"})
    event_logger = Mock()
    event_logger.info.side_effect = RuntimeError("sink unavailable")

    with pytest.raises(RuntimeError, match="sink unavailable"):
        emit_audit_event(rag_audit_payload("candidates"), context, event_logger=event_logger)


def test_normalize_audit_payload_handles_invalid_json():
    from shared.audit import normalize_audit_payload

    result = normalize_audit_payload("not valid json")
    assert result["type"] is False
    assert result["identification"] == "parse_error"
    assert result["description"] == "not valid json"


def test_emit_audit_event_without_runtime_context():
    context = Mock(spec=[])
    event_logger = Mock()
    payload = rag_audit_payload("candidates")

    receipt = emit_audit_event(
        payload, context, request_id="fallback-request-id", event_logger=event_logger
    )

    assert receipt.logged is True
    event_logger.info.assert_called_once_with(
        "escalation_event",
        extra={
            "session_id": "unknown",
            "request_id": "fallback-request-id",
            "type": False,
            "identification": "rag_response",
            "description": "Normal RAG response logging",
        },
    )
