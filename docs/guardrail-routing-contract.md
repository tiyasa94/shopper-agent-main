# Guardrail and routing contract

This document defines the current implementation contract. See
[Shopper agent design goals](agent-design-goals.md) for the architectural rationale and behavioral
acceptance criteria behind it.

See [Response identification labels](response-identification-labels.md) for the audit and
escalation labels emitted by retrieval tools and guardrails, including `structured_plan_response`.

When `get_plan_details` returns premium values, `premium_summary` supplies computed lowest
and highest monthly amounts, all tied plan identities, and unknown plans. Total and subsidized
premiums are ranked separately. Zero is a known value; missing and nonfinite amounts are
excluded from rankings. `response_instructions` remains reserved for non-premium document fallback.

When `get_plan_details` returns supported Medicare Supplement (`MED_SUPP`) facts in the
Medicare market, missing gender, date of birth, or either Medicare Part A/B effective date in
the current quote triggers the MedSup-tab notice. This applies to premium and non-premium facts.
Complete quote inputs, Medicare Advantage-only facts, Individual facts, and unavailable MedSup
facts do not trigger it. Tobacco usage is not checked because the Medicare quote mapping does
not retain that field. The available-plans guardrail keeps its code-composed disclaimer.

`get_plan_details` and `search_plans` record applicable notice text in `_response_addenda`;
`required_response_addendum` remains in tool results for API consumers. The `shopper_addenda`
post-invoke plugin appends the recorded MedSup, extra-benefits, or Essential Extras notices
verbatim once at the end of the final answer. No prompt instructs the model to generate them.
The pre-invoke plugin clears this state every turn, including terminal and emergency routes.
The two async retrieval tools use a synchronous registration wrapper so the SDK captures their
context updates after their async work completes.

The output hook requires nonempty `Summary answer:` and `Details:` sections and excludes
canonical whole-answer fallbacks. These checks do not establish factual support: a paraphrased
unsupported answer with both headings can still receive a notice. That limitation remains
unresolved. `required_response` and `required_response_if_unsupported` continue to prescribe
fallback answers independently of addenda.

When `get_plan_details` supplies permitted `fallback_detail_types`, its `response_instructions`
requires `search_plans` before a final unavailable answer. Instructions identify each exact plan
and only its missing non-premium facts, preserving any structured values already returned.
This also applies to eligible service failures. Premium amounts, missing quote context, and
full-catalog requests retain their existing no-document-fallback behavior.

For selected-plan `primary_care` and `specialist` requests, the tool also returns
service-setting evidence limits and scoped `response_instructions`. An ordinary visit price
does not establish an unstated service setting or provider arrangement. If the shopper asks
about those missing terms, the agent must search the same plans' documents and retain the
supplied visit facts. This adds no category or automatic service classifier. Full-catalog
requests do not receive the setting-search instruction. The instructions guide the agent;
they do not deterministically enforce a search or validate its final interpretation.

`shopper_guardrails` runs before the native agent. It examines only the current user message,
merges trusted request context, blocks PII/PHI deterministically, and uses the WXO-backed classifier
for the remaining current-turn decisions.

The pre-invoke plugin manages these private context variables:

- `_shopper_search_control` authorizes a nonterminal search turn and supplies the private plan
  lookup used to enforce the authoritative application catalog.
- `_response_addenda` is reset to an empty object before tools collect current-turn notices.
- `_turn_result` describes the route, escalation, and synchronous audit receipt. Terminal policy
  outcomes also carry their policy-owned business intent; neutral search turns do not.

Terminal guardrail outcomes, including G01–G12 and G14–G16, remove all tools. G03 live-agent requests,
G08 recommendation requests, G09 enrollment/action requests, and G11 privacy blocks emit their
audit event before the terminal result is returned.
An enroll-today coverage-start question is also terminal when Medicare context supplies a valid
`user_requested_eff_date`. The classifier's `enroll_now_effective_date_question` signal handles
semantic paraphrases; a narrow deterministic matcher remains as a false-negative fallback for the
reported wording. Existing guardrails resolve first. The plugin then returns the context date with
fixed eligibility, application-review, and approval caveats before the native answer model or RAG
sees the turn. Missing or malformed dates use the existing safe unestablished-date response.

G15 returns a fixed refusal for requests about Medicaid itself, including comparisons involving
Medicaid. Incidental Medicaid background in a request about Medicare or another supported plan
does not trigger G15.

G16 uses a fixed response for explicit requests to bypass required retrieval or policy, including
user-supplied system/developer role claims. Ordinary presentation preferences and corrections to
the shopper’s own request remain allowed. G07 also covers unnamed providers such as “my doctor”;
general network rules and service coverage remain ordinary factual questions.

Every nonterminal request uses one neutral route and exposes all registered retrieval tools. Retrieval
scope is deliberately not part of the classifier contract:

| Route | Registered tools | Expected interpretation |
|---|---|---|
| `search_turn` | `get_plan_details`, `search_plans`, `search_general_documents` | The native agent resolves the subject from the current message and history, then chooses the matching tool or asks the minimum clarification |
| `terminal` | None | Return the canonical guardrail response |

Conversation dependence is not a classifier field. The native agent interprets every nonterminal
message in its WXO conversation history when needed and chooses the matching retrieval tool. History
may identify the search scope, subject, or intended plan set, but it is never factual benefit
evidence. Prior tool-flow payloads are not synchronized into later turns. Every nonterminal control
carries `current_turn_search_required=true`: the native agent is instructed to use a search after
the current message for any factual response, while unresolved inputs require clarification instead.
This is a model instruction rather than an infrastructure-enforced tool-choice constraint; a
separate downstream check is required if the platform must reject every no-tool factual response.

Persistent WXO memory is disabled. Native history within the application-owned thread remains
available for conversational reference resolution, while the application-provided current plan is
the authoritative value for first-person current-plan references. The plugin appends only the typed
`<shopper_control>` JSON to the system prompt; it does not generate a second route-specific prose
prompt. One always-on, precedence-ordered routing block defines recommended-plan display,
unresolved-plan clarification, resolved-product search, and general education. The decision ladder
defines the detailed interpretation rules, and the retrieval-tool descriptions define their local
semantic boundary. Native WXO guidelines are intentionally empty so routing behavior has one
inspectable prompt source. The active behavioral corpus retains regression coverage for each route.

When one to five exact current-catalog plan names consume a reply, plan control marks
`selection_only=true`. This narrow signal does not expose, select, or authorize those plans; it only
tells the native agent to resume the latest unanswered factual question when one exists and
otherwise ask for the missing topic. The pre-invoke payload does not reliably expose prior turns,
so continuation detection and plan resolution stay in the native history-aware agent.

Catalog size alone never creates a terminal route or removes the plan tools. When no authoritative
catalog is loaded, control sets `plan_catalog_available=false`. General education still uses
`search_general_documents`; an actual plan-fact request calls `search_plans` with `plan_ids=[]`.
The tool does not call RAG in that state. It synchronously audits and returns the canonical
plan-catalog-unavailable response, preventing invented IDs or substitution of general evidence.

Recommendation and other hard guardrails resolve normally, while a factual comparison with an
unresolved or over-five plan scope remains nonterminal
so the native agent can reuse a bounded historical selection or ask the minimum clarification. The
clarification asks the shopper to choose from the plans displayed in the application without
repeating catalog names. The plan-search tool independently enforces the one-to-five-plan limit and
current catalog allowlist.
Requests to identify the best-fitting plan for the shopper's needs or circumstances remain
recommendation guardrail G08; explicit comparisons of documented plan values such as premiums or
deductibles remain factual except for the MedSup amount boundary described below.
Protected-trait stereotypes and group capability judgments terminate as G10 even when they mention
Medicare or another in-scope insurance subject. Neutral eligibility, accessibility, caregiver-benefit,
and factual insurance questions remain eligible for ordinary routing. G10 resolves before G06 when
both signals apply.

G06 and G10 prepend a validated, question-specific acknowledgement to the existing canonical
scope-redirection response. The acknowledgement cannot answer or repeat the premise; composer
failure returns the unchanged canned response alone.

G14 handles requests to explain the cause of an actual personal claim, bill, charge, cost, rate,
eligibility, or coverage outcome. It prepends a constrained acknowledgement to a canonical
limitation and contact response, then terminates without search. General questions about documented
plan costs, coverage rules, referral requirements, common denial reasons, or appeals remain normal
search turns. A narrow deterministic matcher promotes clear denial/bill phrasings when the
classifier misses them; it feeds the same G14 response path rather than maintaining a second canned
response. The native agent also retains a broad role boundary against inferring actual personal
outcomes in case a request slips through, but it has no instructions tied to the classifier field.

The retrieval tools validate the private nonterminal control marker rather than requiring a matching
route hint. Both plan tools validate every selected ID against the current application catalog and
reject invalid or unavailable selections. `get_plan_details` uses session quote context as the
authority for premiums and supported common costs; `search_plans` is the fallback for unsupported
facts and missing non-premium details. All retrieval tools synchronously attach audit fields to
their result. `shared/rag.py` is an ordinary HTTP client, not a registered WXO tool.

`search_general_documents` returns conditional guidance for personal rating questions. General
lists of premium factors do not establish a rate effect for the shopper's state, insurance product,
or each activity mentioned. The answer must leave an unsupported personal effect unconfirmed,
including when the source mentions tobacco but does not establish vaping or e-cigarette rules.
General education about premium factors remains answerable. This uses the existing tool response
instructions; it adds no topic classifier, system-prompt rule, or inference call.

Structured plan-detail failures expose a safe `error_code`, the immediate service `http_status`
when available, and whether the failure is plausibly `retryable`; these fields describe only what
this boundary can establish and do not infer an undisclosed provider cause. The error code is also
promoted into escalation identification, such as `structured_plan_service_unavailable` for HTTP
503 or `structured_plan_request_rejected` for HTTP 422, so it remains visible in public response
diagnostics. A premium-only error also supplies the exact required response “Premium information
is currently unavailable. Please try again later.” Because plan documents are not authoritative
for premiums, no document fallback is permitted for that unresolved amount.

MedSup identity is currently established from the RAG-owned
`mols-medicare-supplement-plans` document type rather than inferred from plan names or undocumented
ID formats. After one plan search establishes that document family, a question about the premium or
personal cost of buying the plan returns the client-approved MedSup amount message as
`required_response` and suppresses retrieved values. Deductibles, copays, allowances, other benefit
cost sharing, benefit applicability, and percentage-coverage questions continue through the normal
document-evidence path. For `selection_only` replies, the resolved tool query supplies the pending
subject, so a reply such as “Plan N” still preserves a prior premium question.

The Shopper Assistant API consumes `_turn_result` as the private route/escalation contract. For a
nonterminal turn, the executed search tool's source-owned response supplies `business_intent`:
`search_general_documents` emits `generic_info`, while either plan tool emits `specific_plan` or
`broad_plans` after its validated plan selection. A clarification with no tool call has no business
intent. This keeps post-call intent derivation out of the classifier and avoids a post-invoke plugin.
