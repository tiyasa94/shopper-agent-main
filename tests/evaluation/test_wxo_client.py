"""Fast contracts for WXO endpoint safety and trace normalization."""

import json

import pytest
import requests
from evaluation.clients.urls import assert_draft_rag_url
from evaluation.clients.wxo import (
    WxoClient,
    _parse_result,
    _thread_id,
    assert_loopback_url,
    assert_remote_wxo_url,
    write_artifact,
)


@pytest.mark.parametrize(
    "url",
    ["http://localhost:4321", "http://127.0.0.1:4321", "http://[::1]:4321"],
)
def test_loopback_urls_are_allowed(url):
    assert_loopback_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.us-south.watson-orchestrate.cloud.ibm.com",
        "http://192.168.1.10:4321",
        "https://localhost:4321",
    ],
)
def test_nonlocal_or_https_urls_are_rejected(url):
    with pytest.raises(ValueError, match="non-loopback"):
        assert_loopback_url(url)


def test_ibm_cloud_wxo_url_is_allowed_for_explicit_remote_e2e():
    assert_remote_wxo_url("https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/example")


@pytest.mark.parametrize(
    "url",
    [
        "http://api.us-south.watson-orchestrate.cloud.ibm.com/instances/example",
        "https://example.com/instances/example",
        "http://localhost:4321",
    ],
)
def test_remote_e2e_rejects_non_wxo_urls(url):
    with pytest.raises(ValueError, match="non-WXO"):
        assert_remote_wxo_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080",
        "https://elv-d2c-rag-api-dev.example.test",
        "https://rag-refact-nonprod.example.test",
    ],
)
def test_draft_rag_urls_are_allowed(url):
    assert_draft_rag_url(url)


@pytest.mark.parametrize(
    "url",
    ["https://rag-api.example.test", "http://rag-api-dev.example.test", "not-a-url"],
)
def test_non_draft_rag_urls_are_rejected(url):
    with pytest.raises(ValueError, match="non-draft"):
        assert_draft_rag_url(url)


def test_trace_parser_deduplicates_grouped_and_individual_tool_calls():
    tool_call = {
        "id": "call-1",
        "name": "search_plans",
        "args": {"query": "Question", "plan_ids": ["P1"]},
    }
    run = {
        "id": "run-1",
        "result": {
            "data": {
                "message": {
                    "content": [{"text": "Answer"}],
                    "context": {"session_id": "test"},
                    "step_history": [
                        {
                            "step_details": [
                                {"type": "tool_calls", "tool_calls": [tool_call]},
                                {
                                    "type": "tool_call",
                                    "tool_call_id": "call-1",
                                    "name": "search_plans",
                                    "args": tool_call["args"],
                                },
                                {
                                    "type": "tool_response",
                                    "tool_call_id": "call-1",
                                    "name": "search_plans",
                                    "content": (
                                        '{"outcome":"candidates","results":[{"text":"fact"}]}'
                                    ),
                                },
                            ]
                        }
                    ],
                }
            }
        },
    }

    result = _parse_result(run, 1.25)

    assert result.response_text == "Answer"
    assert len(result.calls_for("search_plans")) == 1
    assert result.responses_for("search_plans")[0].content["outcome"] == "candidates"


def test_run_polling_recovers_from_transient_connection_failure(monkeypatch):
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "run-1", "status": "completed"}

    class Session:
        calls = 0

        def get(self, url, *, timeout):
            del url, timeout
            self.calls += 1
            if self.calls == 1:
                raise requests.ConnectionError("temporary disconnect")
            return Response()

    monkeypatch.setattr("evaluation.clients.wxo.time.sleep", lambda _: None)
    client = WxoClient.__new__(WxoClient)
    client.base_url = "http://localhost:4321"
    client.timeout = 2
    client.session = Session()

    assert client._wait_for_run("run-1")["status"] == "completed"
    assert client.session.calls == 2


def test_direct_wxo_reuses_returned_thread_id_and_isolates_logical_sessions():
    class Response:
        def __init__(self, run_id):
            self.run_id = run_id

        def raise_for_status(self):
            return None

        def json(self):
            return {"run_id": self.run_id}

    class Session:
        def __init__(self):
            self.payloads = []

        def post(self, url, *, json, timeout):
            del url, timeout
            self.payloads.append(json)
            return Response(f"submitted-{len(self.payloads)}")

    runs = iter(
        [
            {
                "id": "run-1",
                "status": "completed",
                "result": {
                    "data": {
                        "message": {
                            "thread_id": "wxo-thread-1",
                            "content": [{"text": "First"}],
                        }
                    }
                },
            },
            {
                "id": "run-2",
                "status": "completed",
                "result": {
                    "data": {
                        "message": {
                            "thread_id": "wxo-thread-1",
                            "content": [{"text": "Second"}],
                        }
                    }
                },
            },
            {
                "id": "run-3",
                "status": "completed",
                "result": {
                    "data": {
                        "message": {
                            "thread_id": "wxo-thread-2",
                            "content": [{"text": "Other conversation"}],
                        }
                    }
                },
            },
        ]
    )
    client = WxoClient.__new__(WxoClient)
    client.base_url = "http://localhost:4321"
    client.agent_id = "agent-1"
    client.session = Session()
    client._thread_ids = {}
    client._wait_for_run = lambda _run_id: next(runs)

    client.run("turn-1", "First", {}, session_id="conversation-1")
    client.run("turn-2", "Second", {}, session_id="conversation-1")
    client.run("turn-1", "Other", {}, session_id="conversation-2")

    assert "thread_id" not in client.session.payloads[0]
    assert client.session.payloads[1]["thread_id"] == "wxo-thread-1"
    assert client.session.payloads[1]["context_variables"]["session_id"] == "conversation-1"
    assert "thread_id" not in client.session.payloads[2]
    assert client.session.payloads[2]["context_variables"]["session_id"] == "conversation-2"
    assert client._thread_ids == {
        "conversation-1": "wxo-thread-1",
        "conversation-2": "wxo-thread-2",
    }


def test_direct_wxo_rejects_changed_thread_id():
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"run_id": "submitted-2"}

    class Session:
        def post(self, url, *, json, timeout):
            del url, json, timeout
            return Response()

    client = WxoClient.__new__(WxoClient)
    client.base_url = "http://localhost:4321"
    client.agent_id = "agent-1"
    client.session = Session()
    client._thread_ids = {"conversation-1": "wxo-thread-1"}
    client._wait_for_run = lambda _run_id: {
        "id": "run-2",
        "status": "completed",
        "result": {
            "data": {
                "message": {
                    "thread_id": "wxo-thread-2",
                    "content": [{"text": "Second"}],
                }
            }
        },
    }

    with pytest.raises(RuntimeError, match="changed thread IDs"):
        client.run("turn-2", "Second", {}, session_id="conversation-1")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"thread_id": "submission-thread"}, "submission-thread"),
        ({"result": {"data": {"thread_id": "data-thread"}}}, "data-thread"),
        (
            {"result": {"data": {"message": {"thread_id": "message-thread"}}}},
            "message-thread",
        ),
    ],
)
def test_thread_id_accepts_supported_wxo_response_shapes(payload, expected):
    assert _thread_id(payload) == expected


def test_artifact_includes_expectation_and_redacts_sensitive_context(tmp_path):
    run = {
        "id": "run-1",
        "result": {
            "data": {
                "message": {
                    "content": [{"text": "Answer"}],
                    "context": {"session_id": "session-1"},
                    "step_history": [],
                }
            }
        },
    }
    result = _parse_result(
        run,
        0.5,
        request_context={"session_id": "session-1", "api_key": "do-not-write"},
    )

    path = write_artifact(
        "example_case",
        "My SSN is 123-45-6789 and alternate is 123456789",
        {"route": "direct"},
        result,
        tmp_path,
    )
    artifact = json.loads(path.read_text())

    assert artifact["case_id"] == "example_case"
    assert artifact["expectation"] == {"route": "direct"}
    assert artifact["result"]["request_context"]["api_key"] == "[REDACTED]"
    assert "123-45-6789" not in artifact["query"]
    assert "123456789" not in artifact["query"]
