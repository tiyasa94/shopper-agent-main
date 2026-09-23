# RC4.0 (2026-08-03)

**Status:** Stage validation complete; release review pending

**Audience:** Release review and Client UAT

## Summary

RC4.0 promotes the packaged shopper agent, adds reusable customized-plus-canned guardrail
responses, and improves classification, context handling, and multi-plan queries. Stage testing
was operationally stable and reduced material behavior and response misses versus RC3.1.

## What changed

- Added a reusable, validated one-slot composer for customized recommendation and enrollment-action
  responses while preserving required canned language and safe fallback behavior.
- Reworked classification and routing around terminal, general, plan, and continuation outcomes.
- Normalized duplicate plan-search queries and confined plan identity to validated `plan_ids`.
- Added exact plan-ID resolution, plan-disambiguation regressions, and live RAG/classifier
  connection declarations.
- Promoted shared packaged modules and removed legacy flow and duplicate tool paths.
- Simplified prompt and YAML formatting to avoid unintended model-visible line breaks.

## Compatibility

- Available, recommended, and current-plan context remains supported.
- Shopper Assistant API response and metadata contracts remained present in Stage.
- Stage uses agent ID `65b0ad24-4e50-471e-979f-e10b60055ff9`.

---

## Validation against RC3.1

RC4.0 ran through the deployed Stage Shopper Assistant API. The 57 RC3.1 turns were retained and
two clarification cases were added, producing 40 conversations and 59 turns. Direct comparisons
use the 57 shared turns.

The sanitized [RC4.0 evaluation snapshot](../../tests/evaluation/release_snapshots/rc4.0/) contains
the frozen corpus, responses, public RAG observations, human grades, provenance, and checksums.

### Operational readiness

| Metric | RC3.1 Stage | RC4.0 Stage | Outcome |
|---|---:|---:|:---|
| Conversations completed | 38/38 | 40/40 | 100% for both |
| Turns completed | 57/57 | 59/59 | 100% for both |
| Failures / retries / empty responses | 0 / 0 / 0 | 0 / 0 / 0 | No issues |
| Validated RAG/no-RAG behavior | 55/57 (96.5%) | 57/59 (96.6%) | 2 unnecessary searches |
| Evidence-exposed RAG turns with audit/status | 36/36 | 34/34 | Complete when exposed |
| Repeated RAG calls in one turn | 0 | 0 | No duplicate searches |
| Median latency | 10.172 s | 9.167 s | 9.9% faster |
| P95 latency | 14.113 s | 14.863 s | 5.3% slower |
| Sensitive or internal-envelope exposure | 0 | 0 | No exposure |
| Repository tests | Not recorded | 524/524 | All passed |

> All 59 turns completed without runtime failures. Median latency improved, all exposed RAG
> evidence retained audit/status metadata, and two ambiguous plan references triggered unnecessary
> searches instead of clarification.

### Human-reviewed results

Every turn was reviewed using the
[Qualitative Evaluation Rubric](../../tests/evaluation/QUALITATIVE_GRADING_RUBRIC.md).

#### Agent behavior

| Grade | RC3.1 Stage | RC4.0 Stage |
|---|---:|---:|
| Meets | 48/57 (84.2%) | 47/59 (79.7%) |
| Mixed | 5/57 (8.8%) | 9/59 (15.3%) |
| Misses | 4/57 (7.0%) | 3/59 (5.1%) |

> RC4.0 avoided a material behavior miss on 56/59 turns (94.9%), compared with 53/57 (93.0%) in
> RC3.1. On shared turns, 5 improved, 7 regressed, and 45 were unchanged.

#### Response outcome

| Grade | RC3.1 Stage | RC4.0 Stage |
|---|---:|---:|
| Supported and useful | 32/57 (56.1%) | 36/59 (61.0%) |
| Useful with limitations | 14/57 (24.6%) | 16/59 (27.1%) |
| Material answer issue | 11/57 (19.3%) | 7/59 (11.9%) |

> Useful responses increased from 46/57 (80.7%) to 52/59 (88.1%), and critical response issues
> decreased from 9 to 6. On shared turns, 12 improved, 7 regressed, and 38 were unchanged.

#### Returned context coverage

| Grade | RC3.1 Stage | RC4.0 Stage |
|---|---:|---:|
| Complete | 23/36 (63.9%) | 25/34 (73.5%) |
| Partial | 8/36 (22.2%) | 5/34 (14.7%) |
| No supporting context | 5/36 (13.9%) | 4/34 (11.8%) |

> Complete-or-partial context increased from 86.1% to 88.2%. Retrieval misses were graded
> separately and did not automatically count as agent-behavior misses.

### Multi-turn validation

| Agent-behavior outcome | RC3.1 | RC4.0 |
|---|---:|---:|
| Meets | 10/15 (66.7%) | 9/15 (60.0%) |
| Mixed | 3/15 (20.0%) | 5/15 (33.3%) |
| Misses | 2/15 (13.3%) | 1/15 (6.7%) |

> Fourteen of 15 RC4.0 multi-turn conversations avoided a material behavior miss, compared with
> 13 of 15 in RC3.1.

### Findings

Improvements:

- General financial-assistance answers, unknown-plan handling, topic corrections, HMO/HMO-POS
  education, and several selected-plan continuations improved.
- Recommendation, enrollment-action, provider-lookup, and live-agent guardrails remained correct;
  both new plan-disambiguation cases passed.

Follow-up:

- General deductible and coinsurance follow-ups sometimes requested unnecessary plan selection.
- Ambiguous plan references could trigger broad searches, followed by no fresh search after an
  explicit selection.
- Personalized cost questions and a current-plan network question crossed policy or plan-selection
  boundaries.
- Grounding issues remained in Gold/Platinum out-of-pocket, outpatient surgery, Plan G, and vision
  allowance answers; some general answers also exposed internal source markers.

### Conclusion

RC4.0 is operationally reliable and a net qualitative improvement over RC3.1. It can proceed
through release review with the remaining clarification, plan-selection, grounding, and
source-marker issues tracked.
