"""Current-turn addenda passed from trusted tools to the output plugin."""

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps

from ibm_watsonx_orchestrate.run.context import AgentRun
from pydantic import BaseModel, Field

RESPONSE_ADDENDA_CONTEXT_KEY = "_response_addenda"


def capture_context_after_completion[**P, T](function: Callable[P, Awaitable[T]]) -> Callable[P, T]:
    """Finish async tool work before the SDK's synchronous wrapper snapshots context.

    PythonTool.__call__ captures updates immediately after invoking its function,
    before awaiting coroutine results. Register a synchronous entry point while
    retaining async I/O inside the tool.
    """

    @wraps(function)
    def completed(*args: P.args, **kwargs: P.kwargs) -> T:
        return asyncio.run(function(*args, **kwargs))

    return completed


class ResponseAddenda(BaseModel):
    """Required notices and whole-answer fallbacks that must not receive them."""

    notices: list[str] = Field(default_factory=list)
    unsupported_responses: list[str] = Field(default_factory=list)


def record_response_addendum(
    runtime: AgentRun, notice: str | None, *, unsupported_response: str | None = None
) -> None:
    """Record notices and fallback exclusions independently, preserving earlier tool results."""
    if not notice and not unsupported_response:
        return
    state = ResponseAddenda.model_validate_json(
        runtime.request_context.get(RESPONSE_ADDENDA_CONTEXT_KEY, "{}")
    )
    if notice and notice not in state.notices:
        state.notices.append(notice)
    if unsupported_response and unsupported_response not in state.unsupported_responses:
        state.unsupported_responses.append(unsupported_response)
    runtime.request_context[RESPONSE_ADDENDA_CONTEXT_KEY] = state.model_dump_json()
