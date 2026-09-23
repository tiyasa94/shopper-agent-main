"""Output-hook delivery, deduplication, and whole-answer fallback exclusions."""

import pytest
from ibm_watsonx_orchestrate.agent_builder.tools.types import (
    AgentPostInvokePayload,
    GlobalContext,
    JSONContent,
    Message,
    PluginContext,
    TextContent,
)
from ibm_watsonx_orchestrate.run.context import AgentRun

from shared.response_addenda import (
    RESPONSE_ADDENDA_CONTEXT_KEY,
    ResponseAddenda,
    record_response_addendum,
)
from tools.addendum_plugin import shopper_addenda

NOTICE = "Please provide your date of birth."
FALLBACK = "The available documents do not establish the requested fact."
ANSWER = "Summary answer:\nPlan G costs $180.19.\n\nDetails:\nPlan G: $180.19 per month."


def _invoke(text, state):
    payload = AgentPostInvokePayload(
        agent_id="agent",
        messages=[Message(role="assistant", content=TextContent(type="text", text=text))],
    )
    context = PluginContext(
        global_context=GlobalContext(request_id="request"),
        state={"context": {RESPONSE_ADDENDA_CONTEXT_KEY: state.model_dump_json()}},
    )
    result = shopper_addenda.fn(context, payload)
    assert result.continue_processing
    assert payload.messages[0].content.text == text
    return result.modified_payload.messages[0].content.text


def test_appends_multiple_notices_at_end_and_is_idempotent():
    state = ResponseAddenda(notices=[NOTICE, "Please read the plan documents."])
    expected = ANSWER + "\n\n" + "\n\n".join(state.notices)
    assert _invoke(ANSWER, state) == expected
    assert _invoke(expected, state) == expected


@pytest.mark.parametrize("spacing", [" ", "\u00a0", "\n"])
def test_removes_existing_notice_and_reappends_once_verbatim(spacing):
    duplicate = spacing.join(NOTICE.split())
    answer = ANSWER + "\n\n" + duplicate + "\nMore supported detail.\n" + NOTICE
    output = _invoke(answer, ResponseAddenda(notices=[NOTICE]))
    assert output.count(NOTICE) == 1
    assert output.endswith(NOTICE)
    assert "More supported detail." in output


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Which plan do you mean?",
        FALLBACK,
        "The requested plan details are currently unavailable. Please try again later.",
        f"Summary answer:\n{FALLBACK}\n\nDetails:\n{FALLBACK}",
    ],
)
def test_does_not_append_to_empty_clarification_or_whole_answer_fallback(text):
    assert (
        _invoke(text, ResponseAddenda(notices=[NOTICE], unsupported_responses=[FALLBACK])) == text
    )


def test_partial_answer_keeps_notice_when_another_fact_is_unsupported():
    answer = ANSWER + "\nFor the other requested benefit: " + FALLBACK
    assert _invoke(
        answer, ResponseAddenda(notices=[NOTICE], unsupported_responses=[FALLBACK])
    ).endswith(NOTICE)


def test_no_current_turn_notice_leaves_answer_unchanged():
    assert _invoke(ANSWER, ResponseAddenda()) == ANSWER


def test_skips_trailing_user_message_and_updates_assistant_response():
    payload = AgentPostInvokePayload(
        agent_id="agent",
        messages=[
            Message(role="assistant", content=TextContent(type="text", text=ANSWER)),
            Message(role="user", content=TextContent(type="text", text="Follow-up")),
        ],
    )
    context = PluginContext(
        global_context=GlobalContext(request_id="request"),
        state={
            "context": {
                RESPONSE_ADDENDA_CONTEXT_KEY: ResponseAddenda(notices=[NOTICE]).model_dump_json()
            }
        },
    )

    result = shopper_addenda.fn(context, payload)

    assert result.modified_payload.messages[0].content.text.endswith(NOTICE)


def test_non_text_assistant_response_is_unchanged():
    payload = AgentPostInvokePayload(
        agent_id="agent",
        messages=[Message(role="assistant", content=JSONContent(type="text", text={"answer": 1}))],
    )
    context = PluginContext(
        global_context=GlobalContext(request_id="request"),
        state={
            "context": {
                RESPONSE_ADDENDA_CONTEXT_KEY: ResponseAddenda(notices=[NOTICE]).model_dump_json()
            }
        },
    )

    result = shopper_addenda.fn(context, payload)

    assert result.modified_payload is payload


def test_no_assistant_response_is_unchanged():
    payload = AgentPostInvokePayload(
        agent_id="agent",
        messages=[Message(role="user", content=TextContent(type="text", text="Question"))],
    )
    context = PluginContext(
        global_context=GlobalContext(request_id="request"),
        state={
            "context": {
                RESPONSE_ADDENDA_CONTEXT_KEY: ResponseAddenda(notices=[NOTICE]).model_dump_json()
            }
        },
    )

    result = shopper_addenda.fn(context, payload)

    assert result.modified_payload is payload


def test_recording_deduplicates_and_later_failure_does_not_erase_notice():
    runtime = AgentRun(request_context={RESPONSE_ADDENDA_CONTEXT_KEY: "{}"})
    record_response_addendum(runtime, NOTICE, unsupported_response=FALLBACK)
    record_response_addendum(runtime, NOTICE, unsupported_response=FALLBACK)
    record_response_addendum(runtime, None)
    state = ResponseAddenda.model_validate_json(
        runtime.request_context[RESPONSE_ADDENDA_CONTEXT_KEY]
    )
    assert state.notices == [NOTICE]
    assert state.unsupported_responses == [FALLBACK]
    assert RESPONSE_ADDENDA_CONTEXT_KEY in runtime.get_context_updates()


@pytest.mark.parametrize("fallback_first", [False, True])
@pytest.mark.parametrize("supported", [False, True])
def test_notice_free_fallback_excludes_only_wholly_unsupported_answers(fallback_first, supported):
    """Keep fallback exclusions independently of notices, regardless of tool order."""
    runtime = AgentRun(request_context={RESPONSE_ADDENDA_CONTEXT_KEY: "{}"})
    records = [(NOTICE, None), (None, FALLBACK)]
    if fallback_first:
        records.reverse()
    for notice, fallback in records:
        record_response_addendum(runtime, notice, unsupported_response=fallback)
    state = ResponseAddenda.model_validate_json(
        runtime.request_context[RESPONSE_ADDENDA_CONTEXT_KEY]
    )
    assert state.notices == [NOTICE]
    assert state.unsupported_responses == [FALLBACK]
    answer = (
        ANSWER + "\nOther requested benefit: " + FALLBACK
        if supported
        else f"Summary answer:\n{FALLBACK}\n\nDetails:\n{FALLBACK}"
    )
    assert _invoke(answer, state) == (answer + "\n\n" + NOTICE if supported else answer)


def test_sdk_wrapper_publishes_updates_after_async_work_completes():
    import asyncio

    from ibm_watsonx_orchestrate.agent_builder.tools import tool

    from shared.response_addenda import capture_context_after_completion

    @tool(description="Exercise the SDK context-update boundary")
    @capture_context_after_completion
    async def publisher(context: AgentRun) -> str:
        await asyncio.sleep(0)
        record_response_addendum(context, NOTICE)
        return "done"

    runtime = AgentRun(request_context={RESPONSE_ADDENDA_CONTEXT_KEY: "{}"})
    result = publisher(context=runtime)
    assert result.content == "done"
    assert ResponseAddenda.model_validate_json(
        result.context_updates[RESPONSE_ADDENDA_CONTEXT_KEY]
    ).notices == [NOTICE]
