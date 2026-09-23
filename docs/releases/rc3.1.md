# RC3.1 (2026-07-29)

**Status:** Deployed to Dev and Stage; Client UAT in progress **Audience:** Client UAT

## Summary

RC3.1 redesigns the Shopper Agent's primary conversational path. The new design replaces the flow-based orchestration
path with simpler native conversation handling, scoped retrieval tools, and a pre-invoke guardrail layer. Client UAT
feedback to date is materially more positive about the natural conversation behavior and responsiveness.

## Added

- Direct, exact-ID plan retrieval and plan-independent general-document retrieval.
- Current-turn guardrail classification before the agent or retrieval tools process the request.
- Conversational plan and topic continuity through native watsonx Orchestrate history.
- RAG and guardrail audit metadata compatible with the Shopper Assistant API.

## Changed

- Promoted the redesigned Shopper Agent as the main UAT agent.
- Removed the flow-based orchestrator from the normal response path.
- Kept plan identity in validated tool parameters while keeping plan names and IDs out of retrieval-query text.
- Required a fresh scoped search for every plan fact instead of treating conversation history as benefit evidence.

## Improved

- Clarification and short plan replies are more conversational.
- Unique casual and Unicode plan references resolve to authoritative plans.
- Follow-ups can retain one plan, a comparison set, or a pending topic while allowing the shopper to change only the
  requested benefit.
- Current-plan references use application context without defaulting unrelated questions to recommended plans.

## Compatibility

- Existing available, recommended, and current-plan context remains supported.
- Existing `business_intent`, escalation, plan, RAG-context, and agent metadata remains available to Shopper Assistant
  API consumers.
- Existing guardrail responses remain canonical. The redesigned agent continues to decline personal plan recommendations
  while allowing factual plan comparisons.

See the [Guardrail and Routing Contract](../guardrail-routing-contract.md) for details.

---

## Validation

RC3.1 was tested independently against the current curated behavioral corpus in
[`behavioral_questions.yaml`](../../tests/evaluation/behavioral_questions.yaml). The corpus has been revised since
RC3.0, so these results are intentionally not compared with the prior cached responses.

The deployed Stage Shopper Assistant API ran all 38 conversations and 57 turns sequentially. The suite contains 15
multi-turn conversations covering plan clarification, casual selection, current-plan context, corrections, comparison
follow-ups, and topic changes.

The sanitized [RC3.1 release-evaluation snapshot](../../tests/evaluation/release_snapshots/rc3.1/) commits the exact
question corpus, generated responses, public RAG observations, per-turn grades and review notes, conversation-level
grades, run summary, provenance, and checksums. Credentials, provider correlation identifiers, runtime context, and
internal tool payloads remain excluded.

### Operational readiness

| Metric                         | RC3.1 stage | Outcome               |
|--------------------------------|------------:|:----------------------|
| Conversations completed        |       38/38 | 100% completed        |
| Turns completed                |       57/57 | 100% completed        |
| Execution failures             |           0 | No failures           |
| Transport retries              |           0 | No retries            |
| Empty responses                |           0 | No empty responses    |
| Expected RAG/no-RAG behavior   |       55/57 | 96.5% aligned         |
| RAG calls with audit metadata  |       36/36 | 100% audited          |
| Repeated RAG calls in one turn |           0 | No duplicate searches |
| Median latency                 |    10.172 s | —                     |
| P95 latency                    |    14.113 s | —                     |
| Sensitive-value echoes         |           0 | No exposure           |
| Serialized internal envelopes  |           0 | No exposure           |

### Qualitative results

Every turn was reviewed separately for agent behavior, response outcome, and the coverage of returned RAG context.
Reference answers were review aids rather than exact-match requirements. See the
[Qualitative Evaluation Rubric](../../tests/evaluation/QUALITATIVE_GRADING_RUBRIC.md) for the definitions and examples
applied to Meets, Mixed, and Misses.

The [complete component review](../../tests/evaluation/release_snapshots/rc3.1/component-review.csv) records all three
grades and the review rationale for every turn.

#### Agent behavior

Correct route, guardrail, plan/context selection, and conversation handling.

| Grade  | RC3.1 stage |
|--------|------------:|
| Meets  |  48 (84.2%) |
| Mixed  |    5 (8.8%) |
| Misses |    4 (7.0%) |

> Overall, 93.0% of responses (53/57) avoided a material agent-behavior miss. Desired routing,
> guardrail, and context behavior was fully achieved by 84.2% of turns.

#### Response outcome

User-visible correctness, usefulness, grounding, and appropriate qualification.

| Outcome                 | RC3.1 stage |
|-------------------------|------------:|
| Supported and useful    |  32 (56.1%) |
| Useful with limitations |  14 (24.6%) |
| Material answer issue   |  11 (19.3%) |

> Overall, 80.7% of responses (46/57) were fully or partly useful. Grounding quality is a principal opportunity for the
> next release.

#### Returned context coverage

This measures whether the RAG context returned to the agent contained the information needed for an appropriately
grounded answer. It was assessed on the 36 turns where the public API exposed RAG evidence.

| Coverage              | RC3.1 stage |
|-----------------------|------------:|
| Complete              |  23 (63.9%) |
| Partial               |   8 (22.2%) |
| No supporting context |   5 (13.9%) |

> Overall, 86.1% of evidence-exposed turns (31/36) returned complete or partially useful context, and every RAG call
> included the expected audit metadata.

These results assess the context actually returned; they do not measure recall against every document in the source
collection. Source-document answerability was not independently established for every question, so a context gap may
reflect retrieval, source coverage, or both.

### Conversational validation

The 15 multi-turn conversations contained 34 turns and exercised context retention, clarification, plan selection,
corrections, comparison follow-ups, and topic changes. Ten scenarios fully met the conversation-behavior rubric, three
were mixed, and two missed; 86.7% (13/15) therefore avoided a material conversational-behavior miss.

The [conversation review](../../tests/evaluation/release_snapshots/rc3.1/conversation-review.csv) records the included
turn IDs, applied grades, and rationale for each scenario. Conversation behavior uses the least favorable behavior grade
among its turns, preventing one successful turn from masking a routing, plan-selection, or continuity issue elsewhere.

| Outcome | Conversations | Percent |
|---------|--------------:|--------:|
| Meets   |            10 |   66.7% |
| Mixed   |             3 |   20.0% |
| Misses  |             2 |   13.3% |

> Overall, 86.7% of multi-turn conversations (13/15) avoided a material conversational-behavior miss.

### Validation conclusions

- All 57 turns completed without failures, retries, empty responses, sensitive-value echoes, or exposed internal
  envelopes.
- Desired agent routing, guardrail, plan/context, and conversation behavior was fully achieved in 84.2% of turns; 93.0%
  avoided a material behavior miss.
- Multi-turn behavior fully met expectations in 10 of 15 scenarios, and 13 of 15 avoided a material conversational miss.
- Retrieval invocation aligned with expectations on 96.5% of turns, and 100% of RAG calls exposed the required audit
  metadata without duplicate searches.
- Client UAT feedback favors the redesigned agent's more natural conversational behavior. RC3.1 therefore establishes
  the new design as the primary behavioral baseline while grounding improvements continue.
