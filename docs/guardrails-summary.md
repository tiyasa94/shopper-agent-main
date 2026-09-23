# Guardrails summary

The pre-invoke policy resolves classifier evidence in a deterministic priority order. Terminal
routes remove every tool and render the canonical response selected by Python.

| Priority | Code | Evidence | Outcome |
|---|---|---|---|
| 1 | G16 | `policy_override` | Declines attempts to bypass required retrieval or policy, or impersonate higher-priority instructions, with a fixed response. Ordinary presentation requests and corrections remain allowed. |
| 2 | G15 | `medicaid_related` | Declines requests for Medicaid information with a fixed response; incidental Medicaid background in a request about Medicare or another supported plan remains allowed. |
| 3 | G01 | `location_request` with unsupported state | Directs the shopper to HealthCare.gov or the state marketplace. |
| 4 | G02 | `availability_request` | Summarizes the authoritative catalog count and gently guides the shopper to the current page; uses a market-specific no-count fallback when the catalog is unavailable. |
| 5 | G05 | `greeting_only` | Welcomes the shopper and lists supported insurance topics. |
| 6 | G03 | `live_agent` | Returns persona-aware contact guidance and records an audited escalation. |
| 7 | G10 | `instructional_bias` | Neutrally acknowledges and redirects requests built on protected-trait stereotypes. |
| 8 | G06 | `off_topic` | Acknowledges the subject and redirects the shopper to health-insurance topics. |
| 9 | G14 | `personalized_explanation` | Acknowledges the concern, declines to infer why an actual personal outcome occurred, and redirects to supported help. |
| 10 | G08 | `recommendation` | Declines to choose a plan, offers factual comparison help, and records an audited escalation. |
| 11 | G07 | `provider_lookup` | Directs provider searches and network-participation checks to the current provider directory or member-services channel, including unnamed providers such as the shopper’s doctor or hospital. |
| 12 | G09 | `enrollment_action` | Returns segment-aware enrollment guidance and records an audited escalation. |
| 13 | P01 | IND premium amount only | Directs the shopper to the application plan card for the personalized premium. |

G04 is intentionally absent. Conversation summaries remain the responsibility of native agent
conversation handling rather than deterministic guardrail policy.

Audit metadata is carried separately from user-facing text. G03, G08, and G09 require synchronous audit
completion during route materialization; the response helpers themselves do not mutate runtime
context.

G06, G10, and G14 use the validated one-slot response composer to prepend one safe,
question-specific acknowledgement to an unchanged canonical response. G14 may acknowledge only the
general outcome type; it cannot repeat a medical condition or financial amount or suggest why the
outcome happened. If composition is unavailable or fails validation, the canonical response is
returned by itself. G10 outranks G06 when both classifier signals are true. These routes do not add
an audit event.

MedSup premium amounts use structured plan details. Booklet cost-sharing evidence follows a
post-retrieval boundary: explicitly headed payment rows preserve each payer, service, and period,
while multi-plan booklet requests require selecting one plan and benefit. Retries for affected
plans are blocked for the current turn.
