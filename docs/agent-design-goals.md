# Shopper agent design goals

The shopper agent should use each part of the system for the work it does best:

- The language model interprets natural language, follows conversation context, asks useful
  clarifying questions, and explains evidence clearly.
- Trusted application context establishes what plans and request data are authoritative.
- Search tools validate agent-selected IDs against the authoritative plan catalog, enforce retrieval
  constraints, and return auditable evidence.
- Deterministic guardrails own terminal policy outcomes and exact responses that must not vary.

The goal is not to reproduce the language model's semantic reasoning in classifiers, regular
expressions, or routing code. Those mechanisms should enforce hard boundaries and trusted contracts;
the native WXO agent should handle the conversational decisions inside those boundaries.

## Target behaviors

### Resolve naturally; validate deterministically

The agent should interpret plan references using the current message and native WXO conversation
history. This includes:

- exact plan names;
- unique shorthand such as `Prime`;
- references to the authoritative current plan;
- pronouns and phrases such as `that plan` or `same plan`; and
- a plan or comparison set established earlier in the conversation.

The agent may infer which catalog entry the shopper means, but it may not invent a plan identity.
Both plan tools validate every selected ID against the current application catalog. Matcher-derived
plan names are neither hints nor authorization. The agent should ask the minimum clarification only
when the current message, trusted context, and conversation history genuinely leave no unique valid
selection. That clarification should ask the shopper to choose from the plans already displayed in
the application without repeating catalog names.

This division lets the language model use its natural reference-resolution ability while keeping
plan identity and availability deterministic.

### Let the agent choose the search; let tools enforce the contract

Every nonterminal turn exposes `get_plan_details`, `search_plans`, and `search_general_documents` through the
neutral `search_turn` route. The agent decides which tool matches the shopper's actual question:

- use `search_general_documents` for plan-independent education; or
- use `get_plan_details` for supported quote-derived facts, including premiums; or
- use `search_plans` for unsupported plan facts and missing non-premium structured details.

The classifier does not need to predict this retrieval scope. The executed tool owns the resulting
business-intent label.

Tools, rather than the agent, enforce the hard retrieval rules:

- the available-plan allowlist and canonical IDs;
- the one-to-five-plan search limit;
- the behavior when no authoritative plan catalog is available;
- evidence limits and audit metadata; and
- structured insufficient, conflicting, or unavailable outcomes.

Conversation history may supply the plan or pending topic, but it is not factual evidence. A factual
answer must use the appropriate current-turn search. The agent should not reuse a prior answer merely
because the subject is unchanged.

### Let the agent explain evidence, but not reinterpret it

The language model should turn retrieved evidence into a concise, natural response. It must preserve
the evidence's identity and structure, including:

- the plan and document to which a fact applies;
- table headings, row labels, column ownership, amounts, and ranges;
- network, service-setting, package, eligibility, and time-period qualifiers; and
- distinctions between covered, not covered, member-paid, and plan-paid amounts.

Tool output should make these relationships as explicit and structured as practical. When passages
do not directly support the requested fact or comparison, the agent must use the structured
insufficient-evidence outcome instead of filling gaps from model knowledge. Conflicting evidence
must remain visibly unresolved unless the tool provides an authoritative resolution.

This preserves the language model's strength at explanation without treating it as the authority
for plan facts.

## Responsibility boundaries

| Concern | Primary owner | Expected behavior |
| --- | --- | --- |
| Meaning of the current message | Native WXO agent | Interpret paraphrases and the shopper's immediate goal |
| Conversational reference resolution | Native WXO agent | Carry forward plans and pending topics from native history |
| Plan identity and availability | Native WXO agent, trusted context, and plan tools | Agent resolves meaning; tools reject IDs outside the authoritative catalog |
| Retrieval choice | Native WXO agent | Choose the matching exposed tool for the factual question |
| Business-intent reporting | Executed search tool | Derive intent from the action actually taken |
| Retrieval limits and auditing | Search tools | Enforce scope and return structured, audited outcomes |
| Factual support | Current-turn tool evidence | Provide the only source for factual plan and insurance claims |
| Explanation and tone | Native WXO agent | Synthesize supported evidence clearly and naturally |
| Terminal policy responses | Guardrail plugin | Return deterministic policy-owned outcomes without agent variation |

## Behavioral acceptance criteria

The design is working when:

1. An explicit plan, unique alias, authoritative current plan, or unambiguous prior selection is
   searched without redundant clarification.
2. A genuinely ambiguous plan reference produces one concise clarification, no catalog listing,
   and no factual answer.
3. General education uses general search; actual plan facts use plan search.
4. Every factual follow-up performs a fresh search with the resolved plan set and pending topic.
5. Answers preserve plan identity, qualifiers, and table relationships from the retrieved evidence.
6. Missing or conflicting evidence produces a bounded no-evidence response rather than an inferred
   answer.
7. Policy-owned responses remain deterministic and do not invoke retrieval.

These criteria should drive behavioral evaluation. Classifier-label metrics are supporting
diagnostics, not substitutes for evaluating whether the agent selected the right action and returned
a grounded response.

## Non-goals

- Do not add retrieval-scope classification merely to avoid letting the native agent choose a tool.
- Do not build a growing set of regular expressions to approximate conversational understanding.
- Do not remove plan search because plan context is incomplete; let the plan tool return its safe,
  audited unavailable outcome.
- Do not treat conversation memory or model knowledge as plan evidence.
- Do not make tools responsible for conversational prose when a structured result can safely be
  explained by the agent.

See the [guardrail and routing contract](guardrail-routing-contract.md) for the current implementation
of these boundaries.
