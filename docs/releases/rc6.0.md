# RC6.0 (2026-08-17)

**Status:** Stage validation and release review complete

**Audience:** Release review and Client UAT

## Summary

RC6.0 is an iterative routing and grounding update informed by the RC5 review. It gives the native agent more
responsibility for conversational plan resolution and choosing the appropriate search, while keeping plan authorization,
retrieval limits, evidence handling, and terminal safety outcomes deterministic.

Stage testing completed all 89 questions without an execution failure or retry and improved both routing alignment and
reviewed behavior over the combined RC5 baseline.

## What changed

- Replaced classifier-owned `general_turn` / `plan_turn` routing with a neutral `search_turn` that exposes both bounded
  search tools. Native memory now resolves follow-up topics, unique plan shorthand, current-plan references, and
  retained one-to-five-plan comparison sets; plan search still validates every selected ID against the current
  application catalog.
- Tightened current-turn grounding. General-search passages are explicitly unreviewed candidates, unsupported facts and
  comparisons use the canonical no-evidence response, selection-only replies retain their pending topic, and MedSup
  purchase-cost questions use the approved amount redirect only after retrieval metadata establishes the document
  family.
- Expanded deterministic handling for plan availability counts, brand-specific unavailable-plan responses, enroll-today
  Medicare effective dates, protected-trait bias, plan recommendations, and requests to explain actual personal
  outcomes. Generated acknowledgements are prefix-validated and fall back to canonical responses.
- Bounded and minimized trusted plan context, rejected oversized or ambiguous inputs, and removed request- and
  plan-derived values from routine info-level telemetry.
- Split agent deployment into explicit Draft and Live targets pinned to the reviewed WXO tenant, resource, and
  environment IDs; deployment receipts now preserve that target identity.
- Added architecture and routing-contract documentation, expanded the maintained evaluation corpus from 53
  conversations / 76 questions to 62 conversations / 89 questions, and updated the Orchestrate and development
  dependencies.

## Compatibility

- RC6.0 requires a complete connection, tool, plugin, and agent reimport; importing only
  `agent.yaml` is insufficient. Agent memory is enabled.
- RC6.0 depends on the paired Shopper Platform update so the public-response normalizer accepts `search_turn`.
  Search-tool results now own nonterminal business-intent reporting; a no-tool clarification has no business intent.
- Classifier prompt version is `2.3.0`, classifier contract version is `7.0.0`, and
  `ibm-watsonx-orchestrate` is updated to `2.14.0`.

---

## Validation against RC5.0

The RC5 comparison combines its published 53-conversation / 76-question release result with the nine conversations / 13
questions added afterward, producing a 62-conversation / 89-question baseline on the maintained corpus. Both RC5 runs
used deployed Stage revision
`a9a62004af8f34bfad1b90ae4733bf49f10caf1b` and imported agent
`65b0ad24-4e50-471e-979f-e10b60055ff9`.

The published 76-question result received the release process's AI + human reconciliation. The 13-question gap-fill
received an independent rubric review calibrated against those frozen adjudications and the earlier HMO-POS severity
precedent. The combined RC5 column is the composite baseline for the one-to-one RC6 comparison.

The RC6 column is the August 17 Stage run of the same 62 conversations / 89 questions through Shopper Assistant API
revision `edc0179882185a0c8bdaa592faa7c46be159aec2`, using agent revision `3db96be1`, imported source bundle
`981b89a493d9928924909471d96b403480b6972f2bad13eb78fee1da517e38ac`, and agent ID
`65b0ad24-4e50-471e-979f-e10b60055ff9`. Its grades use a watsonx.ai rubric first pass reconciled against the frozen RC5
and August 17 RC6 adjudications. The sanitized
[RC6.0 evaluation snapshot](../../tests/evaluation/release_snapshots/rc6.0/)
freezes the Stage corpus, responses, public observations, reconciled grades, provenance, and checksums.

After the final platform deployment at `ecb075dbceb43f0d150d7f4329f7a6ae523a8a21`, a 29-turn Stage pilot completed 29/29
turns with no execution failures, retries, or empty responses, and all 21 RAG-indicated turns included audit metadata.
The tables below retain the full graded 89-question run rather than substituting the smaller operational pilot.

### Operational readiness

| Metric                                  | RC5.0 Stage composite (89 questions) | RC6.0 Stage (89 questions) | Outcome                          |
|-----------------------------------------|-------------------------------------:|---------------------------:|:---------------------------------|
| Conversations completed                 |                                62/62 |                      62/62 | 100% for both                    |
| Turns completed                         |                                89/89 |                      89/89 | 100% for both                    |
| Failures / retries / empty responses    |                            0 / 0 / 0 |                  0 / 0 / 0 | No execution failures or retries |
| Harness RAG/no-RAG alignment            |                        78/89 (87.6%) |              86/89 (96.6%) | RC6 +9.0 points                  |
| RAG-indicated turns with audit/status   |                                41/41 |                      47/47 | Complete for both                |
| Repeated RAG calls in one turn          |                                    0 |                          0 | No duplicate searches            |
| Median latency                          |                              7.241 s |                    8.143 s | RC6 Stage 12.5% slower           |
| P95 latency                             |                             11.755 s |                   13.725 s | RC6 Stage 16.8% slower           |
| Sensitive or internal-envelope exposure |                                    0 |                          0 | No exposure                      |
| Repository tests                        |                              654/654 |                    679/679 | RC6 passed                       |

> RC6 completed the full Stage corpus without an execution failure, retry, or empty
> response and improved RAG/no-RAG alignment by 9.0 percentage points. Median and P95 latency were
> higher than the RC5 Stage composite.

### AI + reconciled results

Every turn received a watsonx.ai first-pass grade followed by independent rubric reconciliation using the
same [Qualitative Evaluation Rubric](../../tests/evaluation/QUALITATIVE_GRADING_RUBRIC.md), frozen RC5 adjudications,
and the August 17 RC6 adjudications.

#### Agent behavior

| Grade  | RC5.0 Stage composite (89 questions) | RC6.0 Stage (89 questions) |
|--------|-------------------------------------:|---------------------------:|
| Meets  |                        72/89 (80.9%) |              84/89 (94.4%) |
| Mixed  |                          8/89 (9.0%) |                2/89 (2.2%) |
| Misses |                         9/89 (10.1%) |                3/89 (3.4%) |

> RC6 avoided a material behavior miss on 86/89 questions (96.6%), compared with 80/89
> (89.9%) for the RC5 composite. The remaining misses involve expanding a retained two-plan set and
> twice failing to use an authoritative current plan.

#### Response outcome

| Grade                   | RC5.0 Stage composite (89 questions) | RC6.0 Stage (89 questions) |
|-------------------------|-------------------------------------:|---------------------------:|
| Supported and useful    |                        67/89 (75.3%) |              76/89 (85.4%) |
| Useful with limitations |                        12/89 (13.5%) |                7/89 (7.9%) |
| Material answer issue   |                        10/89 (11.2%) |                6/89 (6.7%) |

> RC6 produced a useful response on 83/89 questions (93.3%), compared with 79/89
> (88.8%) for the RC5 composite. Material answer issues fell from ten to six.

#### Returned context coverage

| Grade among evidence-exposed turns | RC5.0 Stage composite (89 questions) | RC6.0 Stage (89 questions) |
|------------------------------------|-------------------------------------:|---------------------------:|
| Complete                           |                        28/33 (84.8%) |              34/42 (81.0%) |
| Partial                            |                          3/33 (9.1%) |                4/42 (9.5%) |
| No supporting context              |                          2/33 (6.1%) |                4/42 (9.5%) |

> Complete returned-context coverage was 81.0%, and four evidence-exposed turns lacked supporting
> context. Retrieval quality was not graded on 56 RC5 turns and 47 RC6 turns because
> they exposed no evidence.

### Multi-turn validation

| Component                                 | RC5.0 Stage composite (21 conversations / 48 questions) | RC6.0 Stage (21 conversations / 48 questions) |
|-------------------------------------------|--------------------------------------------------------:|----------------------------------------------:|
| Agent behavior — Meets / Mixed / Misses   |                                              13 / 3 / 5 |                                    18 / 1 / 2 |
| Response outcome — Meets / Mixed / Misses |                                              12 / 4 / 5 |                                    16 / 2 / 3 |
| Conversations with a critical issue       |                                                       0 |                                             0 |

> The multi-turn rollup uses the least favorable per-turn component grade. RC6 reduced
> conversations with a material agent behavior miss from five to two and response-outcome misses
> from five to three. Neither comparison had a critical multi-turn conversation.

### Conclusion

Stage evidence shows a meaningful improvement over the RC5 composite baseline across the same 89-question corpus, with
clean operational execution and fewer material behavior and response misses.
