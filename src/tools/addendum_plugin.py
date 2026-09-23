"""Append tool-owned addenda to final responses with the required answer sections."""

import re

from ibm_watsonx_orchestrate.agent_builder.tools import tool
from ibm_watsonx_orchestrate.agent_builder.tools.types import (
    AgentPostInvokePayload,
    AgentPostInvokeResult,
    PluginContext,
    PythonToolKind,
    TextContent,
)

from shared.response_addenda import RESPONSE_ADDENDA_CONTEXT_KEY, ResponseAddenda


@tool(
    name="shopper_addenda",
    description="Append required current-turn addenda to supported shopper responses.",
    kind=PythonToolKind.AGENTPOSTINVOKE,
)
def shopper_addenda(
    plugin_context: PluginContext, agent_post_invoke_payload: AgentPostInvokePayload
) -> AgentPostInvokeResult:
    """Append each notice once, preserving terminal responses and non-text messages.

    Require both answer sections and exclude canonical whole-answer fallbacks.
    Section headings do not establish factual support or detect paraphrased
    fallbacks. The pre-invoke plugin resets notice state every turn.
    """
    result = AgentPostInvokeResult(modified_payload=agent_post_invoke_payload)
    state = ResponseAddenda.model_validate_json(
        plugin_context.state.get("context", {}).get(RESPONSE_ADDENDA_CONTEXT_KEY, "{}")
    )
    if not state.notices:
        return result
    for index in range(len(agent_post_invoke_payload.messages) - 1, -1, -1):
        message = agent_post_invoke_payload.messages[index]
        if message.role != "assistant":
            continue
        if not isinstance(message.content, TextContent):
            return result
        text = message.content.text
        sections = re.fullmatch(
            r"Summary answer:\s*(.+?)\n\s*Details:\s*(.+)", text.strip(), re.DOTALL
        )
        if sections is None:
            return result
        unsupported = {" ".join(value.split()) for value in state.unsupported_responses}
        if all(" ".join(section.split()) in unsupported for section in sections.groups()):
            return result
        for notice in state.notices:
            pattern = r"\s+".join(re.escape(word) for word in notice.split())
            text = re.sub(pattern, "", text, flags=re.IGNORECASE).rstrip()
        text = f"{text}\n\n" + "\n\n".join(state.notices)
        messages = list(agent_post_invoke_payload.messages)
        messages[index] = message.model_copy(
            update={"content": TextContent(type="text", text=text)}
        )
        result.modified_payload = agent_post_invoke_payload.model_copy(
            update={"messages": messages}
        )
        return result
    return result
