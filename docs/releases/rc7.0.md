# RC7.0 (2026-08-24)

**Status:** Dev validation complete; ready for release review and Client UAT

**Audience:** Release review and Client UAT

## Summary

RC7 removes native guidelines and skills, disables persistent agent memory, runs search tools
asynchronously, and supports RAG API-key authentication and the Shopper Assistant API
chat-completions path.

RC7 matches RC6's measured quality with lower latency, fewer material failures, and clean
operation. Multi-turn misses and critical issues are lower than RC6.

## What changed

- Moved the conversation contract into the agent prompt, pre-invoke control, and search tools.
- Disabled persistent memory and flow synchronization; application context remains authoritative.
- Relaxed plan and topic continuity while retaining catalog authorization and the five-plan limit.
- Made search tools asynchronous and retained source-owned audit metadata.
- Added RAG API-key authentication and Shopper Assistant API chat-completions compatibility.
- Limited deterministic recommendation matching to explicit requests; nuanced intent remains
  classifier-owned.
- Directed the agent to explain evidence naturally instead of reproducing source formatting.

## Compatibility

- A full agent re-import is required; RC7 is not a prompt-only update.
- Evaluated through the dev Shopper Assistant API revision
  `ca978005a5fac0eb966ef724a61ff316b81bef48`, agent
  `65b0ad24-4e50-471e-979f-e10b60055ff9`.
- Evaluated source bundle:
  `204f636e779e1c15d1d3f19ce99636b4d134d5b557e49a7f0ca63d9b11344592`.
- The API contract is unchanged. Stage/live promotion follows release approval.

---

## Validation against RC6.0

The comparison uses a warmed RC7 dev run and frozen RC6 responses for the same 62 conversations
and 89 turns. Each RC7 turn received manual rubric-v2 review; RC6 grades remain unchanged.

The sanitized [RC7.0 evaluation snapshot](../../tests/evaluation/release_snapshots/rc7.0/) contains
the corpus, responses, public observations, reconciled grades, provenance, and checksums.

### Operational readiness

| Metric | RC6.0 | RC7.0 | Outcome |
|---|---:|---:|:---|
| Conversations completed | 62/62 | 62/62 | 100% for both |
| Turns completed | 89/89 | 89/89 | 100% for both |
| Failures / retries / empty responses | 0 / 0 / 0 | 0 / 0 / 0 | Clean completion for both |
| Harness RAG/no-RAG alignment | 86/89 (96.6%) | 86/89 (96.6%) | Parity |
| RAG-indicated turns with audit/status | 47/47 | 49/49 | Complete for both |
| Repeated RAG calls in one turn | 0 | 0 | No duplicate searches |
| Median latency | 8.143 s | 7.471 s | **RC7 0.672 s faster** |
| P95 latency | 13.725 s | 9.898 s | **RC7 3.827 s faster** |
| Sensitive or internal-envelope exposure | 0 | 0 | No exposure |
| Repository tests | 679/679 | 746/746 | Passed across native runtimes |

> RC7 was faster on 63/89 matched turns. Median latency improved from 9.844 to 8.249 seconds when
> both releases used retrieval, from 6.074 to 5.840 seconds when neither did, and from 8.891 to
> 7.737 seconds on multi-turn questions.
>
> RC7 also completed the concurrency-three run without retries or failures. The dev RAG API passed
> all 15 authentication, retrieval, and load checks.

### Current-rubric pairwise results

#### Agent behavior

| Grade | RC6.0 re-adjudicated | RC7.0 |
|---|---:|---:|
| Meets | 84/89 (94.4%) | 83/89 (93.3%) |
| Mixed | 4/89 (4.5%) | 5/89 (5.6%) |
| Misses | 1/89 (1.1%) | 1/89 (1.1%) |

> Material behavior misses are at parity. RC7 was better on 4 turns, tied on 81, and worse on 4.

#### Response outcome

| Grade | RC6.0 re-adjudicated | RC7.0 |
|---|---:|---:|
| Supported and useful | 74/89 (83.1%) | 74/89 (83.1%) |
| Useful with limitations | 9/89 (10.1%) | 13/89 (14.6%) |
| Material answer issue | 6/89 (6.7%) | **2/89 (2.2%)** |

> Fully supported answers are at parity. RC7 reduced material issues from six to two and critical
> turns from five to one; it was better on 8 turns, tied on 74, and worse on 7.

#### Returned context coverage

| Grade among evidence-exposed turns | RC6.0 re-adjudicated | RC7.0 |
|---|---:|---:|
| Complete | 34/42 (81.0%) | 33/43 (76.7%) |
| Partial | 4/42 (9.5%) | 4/43 (9.3%) |
| No supporting context | 4/42 (9.5%) | 6/43 (14.0%) |

> RC7 was better on 3 comparable turns, tied on 34, and worse on 4. Three misses are the known
> HMO/HMO-POS corpus gap; the agent safely withheld unsupported conclusions.

### Multi-turn validation

| Component | RC6.0 re-adjudicated | RC7.0 |
|---|---:|---:|
| Agent behavior — Meets / Mixed / Misses | 18 / 2 / 1 | 17 / 3 / 1 |
| Response outcome — Meets / Mixed / Misses | 14 / 3 / 4 | **12 / 7 / 2** |
| Conversations with a critical issue | 3 | **1** |

> RC7 kept behavior misses at parity, halved response misses, and reduced critical conversations
> from three to one. Its additional Mixed grades are useful answers with limited evidence or an
> omitted condition.

## Conclusion

RC7 is ready for release review and Client UAT. It matches RC6 on fully supported answers and
material behavior misses while improving latency, failure severity, and operational stability.
The known HMO/HMO-POS corpus gap and premium-notice retrieval remain follow-up items.
