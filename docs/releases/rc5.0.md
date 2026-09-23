# RC5.0 (2026-08-10)

**Status:** Stage validation complete; release review pending

**Audience:** Release review and Client UAT

## Summary

RC5.0 fixes the off-topic failure for selection-shaped continuation replies such as `both` and
strengthens plan authorization, current-turn grounding, shared-document attribution, and safe
fallback behavior. Stage testing was operationally stable and reduced material behavior and
response misses versus the refreshed RC4.0 baseline.

## What changed

- Relaxed G06 classification for ambiguous, short, context-dependent replies and added a narrow
  continuity safeguard for `both`, `either`, ordinal selections, and similar replies. Explicit
  safety and PII guardrails still run first.
- Made available-plan and current-plan application context authoritative inputs to plan
  authorization, with normalized exact, casual, Unicode, current-plan, and similar-name handling;
  plan selection still requires explicit shopper wording or unambiguous conversation context, and
  recommended plans are not selected by default.
- Refined guardrail boundaries for unavailable products, factual plan rationales, recommendations,
  Individual premiums, personalized outcome explanations, eligibility, and unsupported Medicare
  effective-date or renewal outcomes.
- Enforced one current-turn search for supported facts, explicit `candidates` / `insufficient` /
  `error` tool outcomes, exact selected-plan authorization, and the required `Summary answer:` /
  `Details:` response structure after supported non-personalized searches.
- Limited retrieval to one to five resolved plans and four retrieval queries, bounded evidence by
  plan and total prompt budget, added clarification above those limits, and improved deductible
  queries.
- Added deterministic evidence filtering for plan attribution, table conflicts, damaged coverage
  text, and base-plan versus optional-package applicability; unresolved evidence now fails closed.
- Replaced the legacy importer with manifest-driven local/dev/live deployment, preflight checks,
  promotion and purge workflows, import receipts, and reproducible `uv`-locked builds.
- Expanded deterministic E2E checks, evaluation assertions, and local experiment harnesses for
  routing, tool use, response structure, critical policy boundaries, evidence behavior, and the
  Stage-observed bare-`both` regression.

## Compatibility

- Available, recommended, and current-plan context remains supported.
- Shopper Assistant API response, audit, identity, and public RAG contracts remained present in
  Stage.
- Stage uses agent ID `65b0ad24-4e50-471e-979f-e10b60055ff9`.
- RC5.0 requires a complete connection, tool, plugin, and agent reimport; importing only
  `agent.yaml` is insufficient.

---

## Validation against RC4.0

RC5.0 ran through the deployed Stage Shopper Assistant API. RC4.0 was refreshed against the updated
73-question set instead of reusing its published 59-question score. The refresh adds 14 issue-driven
questions and updates the expected contract for seven retained questions; RC5.0 adds the three-turn
bare-`both` regression for 76 questions total.

Both runs used the same AI + human review standard. The rubric added release-comparison continuity
guidance, but its component grading definitions did not change. The historical 59-question RC4.0
result remains in the RC4.0 release notes and is intentionally omitted here.

The sanitized [RC5.0 evaluation snapshot](../../tests/evaluation/release_snapshots/rc5.0/) contains
the frozen Stage corpus, responses, public RAG observations, final AI + human grades,
provenance, and checksums.

### Operational readiness

| Metric | Refreshed RC4.0 Stage (73 questions) | RC5.0 Stage (76 questions) | Outcome |
|---|---:|---:|:---|
| Conversations completed | 52/52 | 53/53 | 100% for both |
| Turns completed | 73/73 | 76/76 | 100% for both |
| Failures / retries / empty responses | 0 / 1 / 0 | 0 / 0 / 0 | No execution failures |
| Harness RAG/no-RAG alignment | 62/73 (84.9%) | 66/76 (86.8%) | RC5 +1.9 points |
| RAG-indicated turns with audit/status | 48/48 | 33/33 | Complete for both |
| Repeated RAG calls in one turn | 0 | 0 | No duplicate searches |
| Median latency | 9.117 s | 6.961 s | RC5 23.6% faster |
| P95 latency | 15.851 s | 11.709 s | RC5 26.1% faster |
| Sensitive or internal-envelope exposure | 0 | 0 | No exposure |
| Repository tests | Not rerun | 654/654 | RC5 passed; RC4 refresh was eval-only |

> RC5.0 completed all 76 questions without an execution failure or retry, improved RAG/no-RAG
> alignment, and reduced both median and P95 latency. Alignment is a harness signal rather than an
> answer-quality score; skipped or unnecessary searches are reflected in the reviewed grades below.

### AI + human-reviewed results

Every turn received a watsonx.ai first-pass grade followed by human reconciliation using the same
[Qualitative Evaluation Rubric](../../tests/evaluation/QUALITATIVE_GRADING_RUBRIC.md) and available
evidence.

#### Agent behavior

| Grade | Refreshed RC4.0 Stage (73 questions) | RC5.0 Stage (76 questions) |
|---|---:|---:|
| Meets | 54/73 (74.0%) | 64/76 (84.2%) |
| Mixed | 6/73 (8.2%) | 6/76 (7.9%) |
| Misses | 13/73 (17.8%) | 6/76 (7.9%) |

> RC5.0 avoided a material behavior miss on 70/76 questions (92.1%), compared with 60/73 (82.2%) for
> refreshed RC4.0. Its six Misses involve lost decisive current-plan or conversation context;
> correct contextual handling that skipped a required fresh search is graded Mixed.

#### Response outcome

| Grade | Refreshed RC4.0 Stage (73 questions) | RC5.0 Stage (76 questions) |
|---|---:|---:|
| Supported and useful | 52/73 (71.2%) | 62/76 (81.6%) |
| Useful with limitations | 9/73 (12.3%) | 9/76 (11.8%) |
| Material answer issue | 12/73 (16.4%) | 5/76 (6.6%) |

> Useful responses increased from 61/73 (83.6%) to 71/76 (93.4%), while material answer issues fell
> from 12 to 5. The five RC5.0 issues were non-critical failures to answer after losing context;
> correct but weakly grounded or incomplete answers remain visible as Useful with limitations.

#### Returned context coverage

| Grade among evidence-exposed turns | Refreshed RC4.0 Stage (73 questions) | RC5.0 Stage (76 questions) |
|---|---:|---:|
| Complete | 35/48 (72.9%) | 25/28 (89.3%) |
| Partial | 10/48 (20.8%) | 2/28 (7.1%) |
| No supporting context | 3/48 (6.3%) | 1/28 (3.6%) |

> Complete returned context increased from 72.9% to 89.3%, and only 1 of 28 evidence-exposed RC5.0
> questions lacked supporting context. Retrieval quality was not applicable to the other 48 questions
> because they exposed no evidence; they are not counted as retrieval misses.

### Multi-turn validation

| Component | Refreshed RC4.0 Stage (17 conversations / 38 questions) | RC5.0 Stage (18 conversations / 41 questions) |
|---|---:|---:|
| Agent behavior — Meets / Mixed / Misses | 10 / 2 / 5 | 11 / 3 / 4 |
| Response outcome — Meets / Mixed / Misses | 11 / 1 / 5 | 11 / 3 / 4 |
| Conversations with a critical issue | 3 | 0 |

> RC5.0 added one conversation and reduced both behavior and response Misses from 5 to 4. Its three
> Mixed outcomes retain the right context but have a grounding or completeness gap; the four Misses
> are genuine continuity failures. None were critical wrong-plan or policy-boundary failures.

### Findings

Improvements:

- The Stage-observed `which plan?` → `both` flow remained in plan context, searched both plans, and
  passed all three agent-behavior checks.
- Supported answers and complete returned context increased, while material response issues fell.
- All questions completed without execution instability, and RC5.0 had no critical issues; refreshed
  RC4.0 had seven critical turns across three conversations.

Follow-up:

- Current-plan continuity still failed in the same-plan network and urgent-care conversation.
- The Medigap conflict follow-up lost Plan G instead of recovering with a fresh search.
- Some exact-value follow-ups retained the correct plans and answer but skipped fresh retrieval;
  these are grounding gaps rather than wrong-plan answers.

### Conclusion

RC5.0 is operationally reliable, fixes the bare-selection guardrail defect, and improves behavior,
response quality, and returned context over the refreshed RC4.0 baseline. It can proceed through
release review and Client UAT with the remaining continuity gaps tracked.
