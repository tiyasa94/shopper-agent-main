# Response identification labels

This reference describes the current agent-owned values of `escalation.identification`, exposed
in the Shopper Assistant API as `metadata.escalation.identification`. Labels describe a tool
outcome or guardrail reason. They do not identify the deployment environment or API hostname.

## Reading the fields

| Field | Meaning |
| --- | --- |
| `type` | Whether this result is flagged for escalation. `true` does not prove that a human handoff occurred. |
| `identification` | The outcome or reason label, using the exact spelling below. |
| `description` | A readable explanation of that outcome or reason. |

A label can accompany `type: false`: successful retrieval is also audited. A response without a
labeled event can have only `type: false`, with identification absent or null. Not every guardrail
produces an identification label. The tables describe values at their emitting source; the public
response summarizes the turn, so inspect individual tool results when multiple tools ran.

## Structured plan details

`get_plan_details` emits these labels for both IOLS and MOLS. The label does not distinguish the
market; the request context maps `IND` to IOLS and `Medicare` to MOLS.

| Label | `type` | When emitted |
| --- | --- | --- |
| `structured_plan_response` | `false` | The tool returned a `complete` or `partial` result. |
| `structured_plan_details_unavailable` | `true` | The tool could not supply supported details and has no specific error code, including input/selection validation failures or unavailable quote data. |
| `structured_plan_connection_unavailable` | `true` | The configured connection could not be loaded. |
| `structured_plan_connection_not_configured` | `true` | The connection lacks a required base URL, API key, or Plans endpoint. |
| `structured_plan_request_failed` | `true` | The HTTP request could not be completed, or its URL was invalid. |
| `structured_plan_request_rejected` | `true` | The immediate service returned HTTP 4xx. |
| `structured_plan_service_unavailable` | `true` | The immediate service returned HTTP 503. |
| `structured_plan_service_error` | `true` | Another unsuccessful HTTP status was returned. |
| `structured_plan_response_invalid` | `true` | The returned JSON or response schema was invalid. |
| `structured_plan_response_inconsistent` | `true` | The returned market differed from the requested market, or the plan count did not match the returned list. |

`structured_plan_response` is assigned from the result, not simply because the tool was invoked.
For example, a complete structured response includes this escalation object:

```json
{
  "type": false,
  "identification": "structured_plan_response",
  "description": "Structured plan details response logging"
}
```

Check `outcome`, `coverage_complete`, `missing_plans`, and `fallback_detail_types` in the
plan-details metadata to assess coverage. `coverage_complete: true` means all requested structured
detail types were returned for every selected plan; it does not mean every possible plan benefit
was retrieved. The raw tool result also provides per-plan `missing_detail_types`.

An empty `rag_context.contexts` list is compatible with a successful structured response: structured
plan facts are returned through plan details, without requiring document passages. For failures,
inspect the raw tool's `error_code`, `http_status` when available, and `retryable`. Those fields
describe the immediate service boundary, not an independently verified upstream provider cause.

Source: [plan-details tool and error codes](../src/tools/plan_details.py).

## Document retrieval

These labels apply to plan-document search (`search_plans`) and general-document search
(`search_general_documents`).

| Label | `type` | When emitted |
| --- | --- | --- |
| `rag_response` | `false` | Retrieval returned candidate evidence. This alone does not establish that the final answer is supported. |
| `rag_insufficient_context` | `true` | Retrieval evidence was ambiguous or insufficient. |
| `Triggered by Guardrails - RAG Error` | `true` | The retrieval tool returned an error or empty result. |

Use the tool's outcome, error message, evidence fields, and `no_results_reason` when present to
understand the result. The error label alone does not identify the failed filter or establish
whether the underlying issue was missing corpus data, configuration, or service access.

Sources: [RAG audit mapping](../src/shared/audit.py), [plan search](../src/tools/plan_search.py),
[general search](../src/tools/general_search.py), and [shared retrieval client](../src/shared/rag.py).

## Guardrails and processing failures

| Label | `type` | When emitted |
| --- | --- | --- |
| `live_agent_request` | `true` | The user explicitly requested a live agent (G03). |
| `plan_recommendation_request` | `true` | The user requested a personalized plan recommendation (G08). |
| `enrollment_action_request` | `true` | The user requested enrollment or an account action (G09). |
| `pii_phi_detected` | `true` | The privacy guardrail detected PII/PHI in the current message (G11). |
| `classifier_error` | `true` | The current message could not be classified or processed, including the emergency terminal fallback. |
| `parse_error` | `false` | Audit normalization could not parse a supplied serialized audit payload. This is a diagnostic fallback, not a successful retrieval label. |

An identification label does not by itself prove audit logging completed. Where available,
`audit_completed` records that separately; the emergency `classifier_error` path sets it to false.

Sources: [guardrail handlers](../src/shared/guardrails.py),
[guardrail plugin](../src/tools/guardrail_plugin.py), [privacy label](../src/shared/context.py),
and [audit normalization](../src/shared/audit.py).

## Validating a reported response

Correlate the request, run, and trace IDs, then inspect the actual tool calls and results. A label
does not establish DEV versus Stage, the number of downstream HTTP requests, or an actual human
handoff. Follow the [issue-validation workflow](issue-validation/README.md) for historical checks
and the [deployment guide](deployment-guide.md) for the environment mapping.

This reference documents current source behavior. Older deployed revisions may emit a different
set of labels. Update the relevant table whenever an emitting tool or guardrail changes its labels.
