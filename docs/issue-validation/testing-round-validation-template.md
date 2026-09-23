# [Testing round] issue validation

> Copy this file to `artifacts/issues/YYYY-MM-DD/<TESTING_ROUND>_VALIDATION.md` and replace the bracketed placeholders.

Date: `<YYYY-MM-DD>`

Source: `<ticket export, CSV, workbook, or intake location>`

Scope: Historical evidence validation and release attribution. Current Stage replay, local testing, diagnosis, and remediation are excluded unless explicitly listed below.

## Overall conclusion

_[Summarize what the evidence establishes in a few sentences. State whether the supplied reporting was generally supported, invalid, or limited by unavailable evidence.]_

> - **Issues received:** `<count>`
> - **Validated:** `<count>`
> - **Invalid:** `<count>`
> - **Unverifiable:** `<count>`
> - **Eligible for current-release retest:** `<count>`

## Evidence collection

> - **Run IDs supplied:** `<count>`
> - **Run IDs attempted:** `<count>`
> - **Runs pulled:** `<count>`
> - **Runs unavailable or failed:** `<count>`
> - **Unique conversations:** `<count>`

All verdicts in this report are based on runs and conversations pulled directly from the backend. List any exceptions under Limitations.

## Release attribution

Release attribution is based on timestamps in the pulled runs/conversations and `docs/releases/manifest.yaml`, not only on spreadsheet labels or agent IDs.

| Release | Execution dates | Run count | Attribution notes |
| --- | --- | ---: | --- |
| `<RC2.5 / RC3 / RC3.1 / RC4 / other>` | `<date or range>` | `<count>` | `<timestamp evidence and confidence>` |

Distinguish original ticket runs from later retest runs. A ticket can originate in one release and contain retest evidence from another.

## Issue conclusions

| Issue | Description | Disposition | Release | Evidence | Conclusion / next action |
| --- | --- | --- | --- | --- | --- |
| `#NN` | `<reported issue>` | `validated / invalid / unverifiable` | `<release>` | [validation](defect-NN/validation.md) | `<short evidence-based conclusion>` |

Use the same disposition names throughout:

> - **Validated:** directly pulled evidence demonstrates the reported issue.
> - **Invalid:** evidence contradicts the claim, the test setup was wrong, or the alleged behavior did not occur.
> - **Unverifiable:** required backend evidence is missing or unavailable.

## Cross-issue findings

_[Record patterns that affect multiple issues, such as shared/contaminated conversations, malformed retests, missing runs, incorrect role assumptions, or repeated release-label errors. Do not generalize beyond the evidence.]_

## Limitations

- `<Unavailable run IDs, missing source descriptions, uncertain release boundaries, UI-only evidence, or other constraints>`

## Recommended next actions

- `<Return invalid or unverifiable submissions for correction>`
- `<List validated issues that may proceed to current-release retesting>`
- `<List any separate issues discovered during validation>`

## Scope performed

> - **Historical backend validation:** `yes / no`
> - **Current Stage replay:** `yes / no`
> - **Local-agent validation:** `yes / no`
> - **Diagnosis or remediation:** `yes / no`
