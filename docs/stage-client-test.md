# Stage client behavioral test

This command gives client reviewers one supported way to exercise the deployed stage Shopper
Assistant API and collect a consistent, reviewable result set.

## What you need

- A checkout of this `shopper-agent` repository.
- Python 3.13 and `uv`.
- Network access to the deployed stage endpoint.
- A stage Shopper Assistant API client key supplied through an approved secure channel.

Install the locked dependencies once from the repository root:

```bash
uv sync --frozen
```

Do not place the API key in this repository, a command-line argument, a screenshot, or a review
artifact. The test prompts for it without displaying it. Automation may provide it through the
`SHOPPER_STAGE_API_KEY` environment variable.

## Run the test

From the repository root, run:

```bash
./scripts/run-stage-client-test.sh
```

The default pilot is a deterministic, balanced 12-conversation selection from
`tests/evaluation/behavioral_questions.yaml`. The current corpus contains 39 conversations and 58
turns; the balanced pilot covers:

- Individual/Family and Medicare contexts;
- prospect and current-member contexts;
- general-reference, specific-plan, and multiple-plan requests;
- questions that do and do not require plan-document retrieval; and
- multi-turn conversation continuity.

The runner uses the public JSON query endpoint. It checks public login/logout, session reuse,
multi-turn requests, generated responses, public RAG context, plan metadata, escalation metadata,
and request timing. It does **not** test the SSE endpoint, session-summary endpoint, deployment
configuration, or internal WXO tool traces.

The command validates that the target identifies itself as `stage`, runs conversations
sequentially, and retries only transient HTTP or connection failures. It uses version-controlled
synthetic test questions and plan contexts. It creates temporary API sessions and real WXO
threads/runs in stage, but does not import, deploy, or modify cloud assets.

To run all 39 eligible conversations and 58 turns in the current corpus:

```bash
./scripts/run-stage-client-test.sh --full
```

Use the pilot unless the project team specifically requests the full suite.

## Output

Each invocation creates a timestamped directory under `artifacts/stage-client-tests/` containing:

| File | Purpose |
| --- | --- |
| `client-review.csv` | Reviewer workbook; this is the primary deliverable. |
| `summary.json` | Automated counts, errors, retries, and timing. |
| `manifest.json` | Exact endpoint, suite, corpus hash, selection, and run timestamps. |
| `results.csv` | Generated tabular results from the harness; do not edit. |
| `results.jsonl` | Detailed checkpoint data used to resume or diagnose the run; do not edit. |
| `README.md` | A copy of these instructions kept with the artifacts. |
| `QUALITATIVE_GRADING_RUBRIC.md` | Required definitions for grading agent behavior, retrieval, and final responses. |

The runner sanitizes known sensitive test values and never writes the API key. Artifacts still
contain generated responses and retrieved plan-document excerpts. Store and transmit the directory
only through the project-approved location; do not commit it to Git or send it through an
unapproved channel.

## Required review

A zero exit status means only that every selected turn completed without a transport or execution
failure. It is **not** a behavioral acceptance result.

Open `client-review.csv` and review every row in conversation and turn order. Do not change the
generated columns. Complete these columns:

- `Reviewer Name`
- `Agent Behavior (Meets/Mixed/Misses)`
- `Retrieval Quality (Meets/Mixed/Misses/N/A)`
- `Final Response Quality (Meets/Mixed/Misses)`
- `Disposition (Accept/Follow-up/Defect)`
- `Reviewer Notes`

Use `Reference Answer` only as a review aid, not as an exact-answer oracle. Evaluate whether the
response:

1. answers the actual question and preserves multi-turn context;
2. uses the correct current, named, recommended, or compared plans;
3. follows the appropriate guardrail or clarification behavior;
4. is supported by the displayed RAG excerpt or approved general-reference behavior;
5. preserves material amounts, limits, conditions, uncertainty, and plan attribution; and
6. avoids sensitive-value echoes and serialized internal envelopes.

Use the bundled `QUALITATIVE_GRADING_RUBRIC.md` as the grading authority for Meets, Mixed, and
Misses. Its repository source is `tests/evaluation/QUALITATIVE_GRADING_RUBRIC.md`. Use `N/A` for
retrieval quality only when no RAG evidence is exposed. Do not invent alternate scoring criteria.

After reviewing every row, return these three files to the designated project-team contact through
the approved secure channel:

- completed `client-review.csv`;
- `summary.json`; and
- `manifest.json`.

For every `Follow-up` or `Defect`, the reviewer notes must state what was expected, what was
observed, and why it matters. Include the `Conversation ID` and `Turn ID` in any separate defect.
Do not send only screenshots or a verbal pass/fail summary.

## Interrupted or failed runs

Resume an interrupted directory without replaying complete conversations:

```bash
./scripts/run-stage-client-test.sh --resume artifacts/stage-client-tests/pilot-<timestamp>
```

If the command exits nonzero, preserve the entire output directory and send it to the project team
for diagnosis. Do not grade skipped turns as product failures: the harness skips later turns when
an earlier turn in the same conversation cannot complete.
