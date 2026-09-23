# RC3.0 (2026-07-27)

**Status:** Deployed to Stage
**Audience:** Client UAT

## Summary

RC3.0 improves the Shopper Agent's handling of plan context, follow-up questions, retrieved evidence, and guarded
requests while preserving the Shopper Assistant API contract and established guardrail responses.

## Added

- Multi-turn handling for unambiguous plan and topic references from recent conversation history.
- Bounded reference material for general health-plan questions; plan-specific answers continue to require retrieved
  evidence.
- Explicit handling for complete, partial, conflicting, irrelevant, insufficient, and failed retrieval results.

## Changed

- Consolidated guardrails, authoritative plan selection, retrieval, evidence assessment, and audit sequencing into
  `elevance_health_orchestrator`.
- Reduced the top-level agent from three independently coordinated tools to one orchestration tool.
- Moved `search_plans` into the guarded workflow and adopted structured retrieval intents with authoritative plan-ID
  filters.
- Updated the agent to the supported `react_core` style and watsonx Orchestrate ADK 2.13.0.

## Improved

- Member references such as “my plan” prioritize the current plan supplied by the application.
- Explicit plan names override implicit current-plan or recommended-plan defaults.
- Ambiguous plan references request clarification instead of silently selecting multiple plans.
- Partial answers distinguish supported information from details that cannot be confirmed instead of treating missing
  evidence as proof that a benefit is absent.
- Retrieved passages are assessed and attributed to the applicable plan before response generation.
- Median and P95 response latency decreased by approximately 26% in the paired stage evaluation.

## Compatibility

- Existing available, recommended, and current-plan context remains supported.
- Existing `business_intent`, escalation, plan, RAG-context, and agent metadata remain available to Shopper Assistant
  API.
- G01-G09 and G11-G13 retain their established routing priority, canonical responses, and audit behavior. G10 remains
  inactive.

See the [Guardrail and Routing Contract](../guardrail-routing-contract.md) for details.

---

## Validation

RC3.0 and the cached pre-RC3 stage deployment ran the same 62 conversations and 133 turns through the stage Shopper
Assistant API. The current non-live automated suite also passes 297 tests, with 173 live or integration cases
deselected.

The sanitized [RC3.0 release-evaluation snapshot](../../tests/evaluation/release_snapshots/rc3.0/)
commits the reusable responses, public observations, component review, run summaries, provenance,
and checksums. Large exploratory and raw runtime artifacts remain excluded from Git.

### Quantitative results

| Metric                            | Pre-RC3 stage | RC3.0 stage |        Change |
|-----------------------------------|--------------:|------------:|--------------:|
| Completed turns                   |       133/133 |     133/133 | No regression |
| Execution failures                |             0 |           0 | No regression |
| Median latency                    |      24.266 s |    17.992 s |   25.9% lower |
| P95 latency                       |      33.576 s |    24.942 s |   25.7% lower |
| `business_intent` present         |        70/133 |     133/133 |           +63 |
| Agent identity present            |       126/133 |     133/133 |            +7 |
| Audit/escalation metadata present |        25/133 |      76/133 |           +51 |
| Turns with exposed RAG evidence   |        53/133 |      59/133 |            +6 |
| Sensitive-value echoes            |             0 |           0 | No regression |

### Qualitative results

Every paired turn was reviewed separately for agent behavior, final response quality, and retrieval quality. Reference
answers were review aids rather than exact-match requirements. See the
[Qualitative Evaluation Rubric](../../tests/evaluation/QUALITATIVE_GRADING_RUBRIC.md) for the definitions and examples applied to Meets, Mixed,
and Misses.

#### Agent behavior

Correct route, guardrail, plan/context selection, and conversation handling.

| Grade  | Pre-RC3 stage | RC3.0 stage | Change |
|--------|--------------:|------------:|-------:|
| Meets  |    83 (62.4%) | 122 (91.7%) |    +39 |
| Mixed  |    26 (19.5%) |    9 (6.8%) |    -17 |
| Misses |    24 (18.0%) |    2 (1.5%) |    -22 |

#### Final response quality

Correct and useful use of the information available to the agent, with appropriate qualifications.

| Grade  | Pre-RC3 stage | RC3.0 stage | Change |
|--------|--------------:|------------:|-------:|
| Meets  |    61 (45.9%) |  93 (69.9%) |    +32 |
| Mixed  |    48 (36.1%) |  32 (24.1%) |    -16 |
| Misses |    24 (18.0%) |    8 (6.0%) |    -16 |

> Overall, 94.0% of RC3.0 responses (125/133) avoided a material miss and were graded Meets or Mixed—safe or at least
> partly useful—up from 82.0% (109/133) before RC3.

#### Retrieval quality

Relevant, plan-correct, and sufficiently complete evidence. Retrieval was graded only when the public API exposed RAG
evidence. Percentages use the evidence-exposed turns for that candidate as the denominator.

| Grade                         | Pre-RC3 stage | RC3.0 stage |
|-------------------------------|--------------:|------------:|
| Evidence-exposed turns graded |            53 |          59 |
| Meets                         |    19 (35.8%) |  36 (61.0%) |
| Mixed                         |    20 (37.7%) |  11 (18.6%) |
| Misses                        |    14 (26.4%) |  12 (20.3%) |

The [complete component review](../../tests/evaluation/release_snapshots/rc3.0/component-review.csv)
contains the before-and-after grades and component-specific reviewer notes for every turn.

### Validation conclusions

- Desired agent routing and context-use behavior increased from 62.4% to 91.7%.
- Final responses meeting the desired standard increased from 45.9% to 69.9%, while material misses fell from 18.0% to
  6.0%.
- Among turns with exposed evidence, retrieval meeting the desired standard increased from 35.8% to 61.0%. This
  comparison is directional because the evidence-exposed subsets differ.
- The primary improvement opportunity is the 24.1% of RC3.0 responses graded Mixed, principally safe but incomplete
  answers, unnecessary fallbacks, and unclear handling of incomplete or conflicting evidence.

## UAT focus

- Guardrail responses and escalation payloads.
- Member current-plan and shopper recommended-plan behavior.
- Ambiguous, unavailable, and multi-turn plan references.
- Answers produced from partial, conflicting, irrelevant, or missing evidence.
- Cases where retrieval is invoked unnecessarily or useful retrieval is omitted.
