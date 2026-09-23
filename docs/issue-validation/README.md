# Incoming issue intake: five-step checklist

Use this checklist to vet incoming tickets before anyone retests the current release or sends work to engineering.

## At a glance

1. **Receive:** Capture only the issue description and every associated run ID.
2. **Pull:** Retrieve each run and conversation directly from the backend, preserving evidence and recording failures.
3. **Validate:** Confirm the input, context, roles, response, and tool activity support the reported issue.
4. **Decide:** Classify the issue as validated, invalid, or unverifiable; only validated issues proceed.
5. **Document:** Attribute runs to releases using timestamps and the release manifest, then record evidence-backed
   conclusions.

## 1. Receive the tickets

For each ticket, capture only:

> - the issue description
> - every associated run ID

No other intake fields are required. The conversation, input, context, related identifiers, timestamps, and tool history
must be derived from the backend data pulled for those runs.

Treat the spreadsheet and reporter's verdict as claims, not verified facts.

## 2. Pull the related run and conversation

Use every supplied run ID to pull the run and its conversation directly from the backend.

Preserve both the raw evidence and the pull result. If an ID is unavailable, record that it was attempted and could not
be retrieved. Do not silently skip it.

Use a simple folder structure like this:

```text
artifacts/issues/YYYY-MM-DD/
  defect-NN/
    intake.md                 # Issue description and supplied run IDs
    runs/
      <run-id>/
        fetch-status.json     # Pulled, unavailable, or failed
        wxo-run.json          # Run and child-flow payload
        wxo-conversation.json # Conversation, context, and tool-trace report
```

Keep each issue separate and create one run directory for every supplied run ID.

Do not validate a backend issue from a screenshot, copied response, or spreadsheet summary alone.

## 3. Validate the claimed issue

Check the pulled evidence and answer:

> - Was the claimed message actually sent by the shopper?
> - Did it have the context required for the expected behavior?
> - Is the reported assistant response tied to that message and run?
> - Which route and tools ran, and what did they return?
> - Does the evidence demonstrate the claimed failure?
> - Was the conversation contaminated by unrelated ticket tests?

Use the message's `role` field to identify the user and assistant. Do not infer authorship from display order or agent
metadata.

## 4. Reject invalid claims; proceed with valid ones

Give every ticket one initial disposition:

> - **Validated:** the directly pulled evidence demonstrates the claimed issue.
> - **Invalid:** the evidence contradicts the claim, the test input/setup was wrong, or the alleged behavior did not
>   occur.
> - **Unverifiable:** required evidence is missing or unavailable, so the claim cannot be decided.

State the reason and link to the exact evidence. An invalid retest does not prove that a historical defect was fixed; it
only means that test cannot support the claim.

Only validated issues proceed to current-release retesting. Unverifiable issues return to the reporter for missing
information.

## 5. Document the release and conclusions

Use timestamps from the pulled run and conversation, then compare them with `docs/releases/manifest.yaml` to determine
whether each execution belongs to RC2.5, RC3, RC3.1, RC4, or another release window.

Record ticket-origin runs and later retest runs separately. A ticket may originate in an older release but contain newer
retest evidence.

Do not identify the release from the spreadsheet label alone. Agent IDs may be reused and are supporting evidence only.

The completed validation package should look like this:

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

The round-level `<TESTING_ROUND>_VALIDATION.md` contains the overall conclusions: totals, validated/invalid/unverifiable
counts, release breakdown, major evidence problems, and links to every issue. Each issue's `validation.md` contains its
disposition, reasoning, evidence links, release attribution, limitations, and whether it may proceed to current-release
retesting.

Copy the [testing-round validation template](testing-round-validation-template.md) when starting this report.

This intake phase stops here. Do not call Stage, test a local agent, diagnose code, or implement a fix unless that next
phase is explicitly requested.

Coding agents must follow the evidence and correlation rules in
the [coding-agent issue validation runbook](coding-agent-runbook.md).

## Useful commands

The WXO collection tools live in `shopper-platform/apps/shopper-assistant-api`. Run these commands from that component
with a protected environment file that is mode `0600` or stricter and defines `WXO_API_URL`, `WXO_IAM_URL`, and
`WXO_API_KEY`.

Pull one run and its conversation/context into the matching issue directory:

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

Use a new output directory and do not overwrite existing evidence. The resulting files can contain sensitive
conversation and context data. If collection fails, record the specific failure in `fetch-status.json` rather than
silently omitting or relabeling the run.

From the `shopper-agent` repository, these commands help inspect and verify the collected artifacts:

```bash
rg -n --fixed-strings 'replace-with-run-id' artifacts/issues/
jq empty artifacts/issues/YYYY-MM-DD/defect-NN/runs/replace-with-run-id/*.json
rg -n 'RC[0-9]' docs/releases/manifest.yaml
```
