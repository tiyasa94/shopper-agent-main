# Coding-agent runbook: incoming issue validation

Use this runbook when a coding agent is asked to validate an issue export. It is an evidence-collection and adjudication task unless the user explicitly authorizes current-release replay, local reproduction, or code changes.

## Objective

For every reported issue:

1. preserve the source claim;
2. pull the referenced backend run and conversation;
3. decide whether the evidence demonstrates the claimed behavior;
4. attribute each execution to a release using backend dates;
5. replay the exact scenario on the current released Stage agent only when requested;
6. pull and inspect every replay; and
7. produce an auditable verdict and evidence bundle.

Never treat the reporter's classification, spreadsheet wording, or pasted response as ground truth.

## Scope boundary

- Validation-only work must not modify application code, prompts, tools, deployments, or cloud resources.
- Do not test a local candidate or implement a fix unless the user explicitly asks for that phase.
- A Stage replay creates sessions and WXO runs. Do not perform it when the requested scope stops at historical validation and release attribution.
- Preserve unrelated work in the repository.
- Treat pulled conversations and context as sensitive. Never commit secrets, credentials, or unredacted data that repository policy excludes.

## Required intake

The reporting team needs to provide only:

> - the issue description
> - every associated run ID

Derive the conversation, exact input, context, request/trace/thread/session IDs, timestamps, route, tool history, release, and observed response from the pulled backend records. Do not ask the reporter to transcribe data that the run already contains.

Preserve the source workbook/CSV or ticket export as received. Treat any reporter classification or release label as an unverified claim.

## Procedure

### 1. Inspect existing conventions

Before writing artifacts:

1. inspect the target repository's instructions and worktree state;
2. inspect the most recent directory under `artifacts/issues/`;
3. inspect `docs/releases/manifest.yaml`; and
4. discover available collection/replay scripts with `rg --files scripts`.

Follow the established artifact naming for the current testing round.

### 2. Normalize the source export

Create one `defect-NN/intake.md` per real issue containing only the reported description and supplied run IDs. If the
source export must also be retained, keep it as optional source evidence; it does not replace `intake.md`.

Normalize run IDs as strings. Deduplicate them only for network collection; preserve every source association and copy
the archived evidence into each issue directory that references the run. Do not confuse spreadsheet row numbers with
defect numbers.

For each run ID, record one of: `pulled`, `not_found`, `unauthorized`, `malformed`, or `fetch_failed`. Never omit a run ID merely because it could not be retrieved.

### 3. Pull direct backend evidence

The supported WXO utilities live in the Shopper Assistant API component of `shopper-platform`:

```text
apps/shopper-assistant-api/scripts/pull_wxo_run.sh
apps/shopper-assistant-api/scripts/debug_wxo_conversation.py
```

From `shopper-platform/apps/shopper-assistant-api`, use a protected environment file and write into the canonical
`defect-NN/runs/<run-id>/` directory:

```bash
(
  set -euo pipefail
  set -o noclobber
  umask 077

  RUN_ID='replace-with-run-id'
  VALIDATION_ENV_FILE='/absolute/path/to/protected/.env'
  ISSUE_RUNS_DIR='/absolute/path/to/artifacts/issues/YYYY-MM-DD/defect-NN/runs'
  RUN_OUTPUT_DIR="$ISSUE_RUNS_DIR/$RUN_ID"

  mkdir -p "$RUN_OUTPUT_DIR"

  ./scripts/pull_wxo_run.sh "$RUN_ID" "$VALIDATION_ENV_FILE" > "$RUN_OUTPUT_DIR/wxo-run.json"

  uv run --frozen python scripts/debug_wxo_conversation.py "$RUN_ID" \
    --env-file "$VALIDATION_ENV_FILE" \
    --format json \
    --show-context \
    --output "$RUN_OUTPUT_DIR/wxo-conversation.json"

  jq -n --arg run_id "$RUN_ID" \
    '{run_id: $run_id, status: "pulled"}' \
    > "$RUN_OUTPUT_DIR/fetch-status.json"
)
```

Do not print credentials or overwrite existing evidence. If collection fails, write the specific failure to
`fetch-status.json`. If repository-specific batch collectors already exist for the testing round, inspect and reuse them
rather than reimplementing retrieval.

### 4. Correlate the run to the conversation

Do not locate a run by visually scanning adjacent messages. Establish the chain:

```text
run ID
  -> run trace_id/request_id/thread_id
  -> matching assistant conversation message
  -> parent_message_id or chronological association
  -> originating user message and its context
```

Determine authorship from the message's `role` field. Agent configuration metadata does not make a message an assistant message.

For each execution, extract at least:

- run/request/trace/thread/session IDs;
- start/completion and message timestamps;
- user input and message role;
- context attached to the relevant turn;
- assistant output;
- route and tool calls;
- tool arguments and summarized results; and
- errors, fallbacks, escalation, or missing child flows.

### 5. Attribute the release

Use `started_at`, `completed_at`, and conversation timestamps from the pulled backend records. Compare those dates with `docs/releases/manifest.yaml`.

Record the evidence and confidence for release attribution. An agent ID can be supporting metadata but is not sufficient because IDs may be reused across releases. If a run falls on a deployment boundary and no immutable revision is exposed, mark the attribution uncertain.

Keep original-ticket, earlier-retest, and current-retest release attribution separate.

### 6. Validate the historical claim

Answer these questions from backend evidence:

1. Did the alleged user message occur, with `role: user`?
2. Does the input recorded in the report match the correlated backend user message?
3. Was the material context present on that turn?
4. Did required prior turns exist for a follow-up?
5. Did the assistant produce the alleged response?
6. Which route and tools ran?
7. What did retrieval/tools actually return?
8. Does the response contradict those results or the approved behavior?
9. Is the alleged failing layer correct: client, transport, context, routing, tool, retrieval, or synthesis?
10. Was the session contaminated by unrelated issue tests?

Important interpretations:

- An assistant clarification copied into a replay as user input makes that replay invalid.
- Several calls in one thread are a multi-turn sequence, not independent repetitions.
- `results: []` in an agent-facing wrapper does not by itself prove that the retrieval backend returned zero candidates; inspect tool and flow evidence.
- Correct backend behavior does not resolve a UI/VDI-only claim without correlated client evidence.
- An unavailable original record makes the historical baseline unverifiable; a successful current response alone cannot prove a fully verified fix.

### 7. Build an equivalent Stage replay when authorized

Reconstruct the smallest complete scenario:

- identical user wording;
- identical prior user/assistant turns for multi-turn cases;
- identical material context keys and values;
- same plan availability and selection state; and
- same endpoint and authentication class.

Do not reuse the reporter's existing thread. Use a fresh API session and WXO thread for every independent attempt. Run at least three independent attempts for ordinary behavioral claims. Performance or intermittent claims need an explicit measurement boundary and a larger agreed sample.

Before execution, save the exact replay payload. After execution, save the public response and every returned identifier. Then pull each resulting backend run and conversation and validate them using steps 4-6.

Do not grade only the visible answer.

### 8. Assign a verdict

Use one primary verdict:

| Verdict | Required basis |
| --- | --- |
| `confirmed_current_defect` | Equivalent current Stage trials reproduce the problem and backend evidence supports it. |
| `confirmed_fixed` | The historical defect is backend-verified and equivalent current trials no longer reproduce it. |
| `not_reproduced_current_release` | Valid current trials pass, but evidence is insufficient to claim a verified historical fix. |
| `invalid_retest` | Input, history, context, environment, role, or independence differs materially. |
| `unverifiable` | Required backend record, payload, expectation, or correlation is unavailable. |
| `backend_correct_ui_unverified` | Backend evidence is correct; the remaining claim is UI/VDI behavior without correlated client proof. |
| `performance_inconclusive` | No SLA, controlled measurement boundary, or sufficient sample exists. |
| `new_issue_discovered` | Evidence demonstrates a different failure; split it from the original claim. |

For a scope that stops before current Stage replay, state only whether the historical claim is `validated`, `invalid`, or `unverifiable`; do not guess a current-release verdict.

`fixed`, `persisting`, and `regression` require comparable valid evidence from both release windows. A changed prompt, missing turn, changed context, or contaminated session cannot establish those labels.

### 9. Write the evidence bundle

The checklist and template are the canonical output contract. Use this structure:

```text
artifacts/issues/YYYY-MM-DD/
  <TESTING_ROUND>_VALIDATION.md # Overall conclusions, counts, and issue links
  ISSUE_INDEX.md               # One-line disposition and link for each issue
  defect-NN/
    intake.md                  # Issue description and supplied run IDs
    validation.md              # Issue conclusion, evidence, release, and limitations
    runs/
      <run-id>/
        fetch-status.json     # Pulled, unavailable, or failed
        wxo-run.json          # Run and child-flow payload
        wxo-conversation.json # Conversation, context, and tool-trace report
```

Optional machine-generated indexes such as `manifest.json` and `validation.json` may be added at the round root.
Optional derived or replay artifacts such as `run-report.json`, `public-response.json`, and replay payloads must remain
inside the relevant issue/run area and must not replace the required files above.

Each `validation.md` must be understandable without the spreadsheet and contain:

1. primary verdict;
2. reported issue description and behavior assessment;
3. ticket-origin and execution release context;
4. evidence table mapping request/run/trace/thread/session IDs;
5. input/context equivalence assessment;
6. backend findings by layer;
7. independence/contamination assessment;
8. unavailable evidence and limitations; and
9. next action.

The round summary must state how many run IDs were supplied, attempted, pulled, and unavailable; how many unique
runs/threads/sessions were found; which executions belong to each release window; and, when current replay was in scope,
which issues had no valid current retest.

Start the round summary from the [testing-round validation template](testing-round-validation-template.md).

Formatting must match the checklist and template:

- retain the template's headings and order unless the testing scope makes a section inapplicable;
- use blockquoted lists for visually prominent intake fields, counts, dispositions, and performed-scope results;
- use fenced `text` blocks for directory structures;
- use tables for release attribution, issue conclusions, and identifier/evidence mappings;
- put the primary disposition near the top of each `validation.md`; and
- link conclusions to the exact issue and run evidence instead of pasting raw JSON into the report.

The workbook/CSV is an index only. Keep cells concise, link to evidence, avoid merged or duplicate columns, and serialize IDs and currency as text.

## Mandatory quality checks

Before reporting completion:

- confirm every supplied run ID has a `fetch-status.json`, including failures;
- confirm every verdict links to direct evidence;
- confirm conversation roles and run/message correlations were not inferred from display order;
- confirm release attribution cites backend timestamps and the release manifest;
- confirm repeated trials use unique sessions/threads or are labeled non-independent;
- confirm issue counts match the source's real defect numbers, not row count;
- confirm reports distinguish original evidence from each retest generation;
- validate JSON and check Markdown links/formatting;
- inspect repository status and preserve unrelated changes; and
- state explicitly whether Stage replay, local reproduction, and remediation were in or out of scope.

## Stop conditions

Record `unverifiable` when no usable run ID was supplied or the required backend record cannot be retrieved. Stop before Stage calls, local execution, deployment, or source changes when that action is outside the user's stated scope.

If a confirmed current defect proceeds to engineering, preserve the exact reproducer, add regression coverage, reproduce locally with the same payload, identify the failing layer, implement the smallest safe fix, and revalidate after deployment. That is a separate authorized phase.
