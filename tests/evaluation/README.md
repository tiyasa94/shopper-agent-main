# Evaluation data and contracts

Runnable evaluation code lives in the top-level `evaluation` package. This directory contains
version-controlled corpora, release snapshots, grading guidance, and their deterministic tests.

## Corpora

- `behavioral_questions.yaml`: focused reviewed behavioral suite; the default for agent and
  public-API evaluation.
- `adversarial_questions.yaml`: prompt-injection, mixed-intent, PII, and history-pressure suite.
- `uat_questions.yaml`: full reviewed UAT-readiness suite generated from the imported workbook.
- `external_questions.yaml`: immutable imported workbook provenance.
- `retrieval_questions.yaml`: direct RAG retrieval-quality benchmark.

Reference answers are review aids, not exact-match oracles. Retrieved current plan evidence remains
authoritative for document-backed facts.

Rebuild the imported and reviewed corpora only when their source changes:

```bash
uv run python -m evaluation curate-external --source /path/to/questions.xlsx
uv run python -m evaluation curate-uat
```

## Evaluation command

List supported workflows:

```bash
uv run python -m evaluation --help
```

Run a direct local-WXO behavioral evaluation:

```bash
uv run python -m evaluation behavioral run \
  --candidate working-tree \
  --source-ref working-tree \
  --source-revision "$(git rev-parse HEAD)" \
  --rag-base-url local-dev-milvus \
  --suite pilot \
  --output artifacts/evaluations/local-pilot
```

Select a different corpus with `--corpus`, for example:

```bash
uv run python -m evaluation behavioral run \
  --corpus tests/evaluation/adversarial_questions.yaml \
  --candidate adversarial-local \
  --source-ref working-tree \
  --source-revision "$(git rev-parse HEAD)" \
  --rag-base-url local-dev-milvus \
  --suite full \
  --output artifacts/evaluations/adversarial-local
```

Check the adversarial suite’s fixed refusals and allowed boundary cases locally with:

```bash
uv run python -m evaluation assertions \
  --run artifacts/evaluations/adversarial-local \
  --spec tests/evaluation/adversarial_assertions.yaml --strict
```

The runner checkpoints sanitized `results.jsonl`, writes `results.csv`, `manifest.json`, and
`summary.json`, preserves conversation order within a session, and supports `--resume`. Business
intent reporting separates exact alignment, mismatches, not-applicable clarification turns, and
missing intent so the summary does not count a contract-compliant no-tool clarification as an
intent failure.

Semantically grade a completed run with the qualitative rubric:

```bash
source .env.local
uv run python -m evaluation behavioral judge \
  --run artifacts/evaluations/local-pilot
```

The judge is opt-in and never runs as part of pytest. It sends only fields from the saved sanitized
result to watsonx.ai and writes `judgments.jsonl`, `judgments.csv`, `manifest.json`, and
`summary.json` below the run's `judgments/` directory. Each turn receives `Meets`, `Mixed`, or
`Misses` grades with reasons and evidence references for agent behavior and final response quality;
retrieval quality is `N/A` when no evidence is exposed, and disposition remains `Accept`,
`Follow-up`, or `Defect`. Under rubric v3, every final-response Misses grade is additionally
classified as `Safe` or `Unsafe`; Meets and Mixed are safe outcomes and receive no safety qualifier.
The summary also stratifies grades by intended tool use, guardrail, and direct clarification
behavior; this is a diagnostic cohort view rather than an exact tool-call score.
Configuration comes from
`WATSONX_AI_URL`, `WATSONX_AI_API_KEY`, `WATSONX_AI_PROJECT_ID`, `WATSONX_AI_MODEL_ID`,
`WATSONX_AI_API_VERSION`, and `WATSONX_IAM_URL`; all are required. Use `--resume` to retain already
completed grades.

For a release-facing manual adjudication, calibration against the latest committed release
snapshot is mandatory. Before publishing scores:

1. Read the latest shared-turn `component-review.csv`, stored responses, dispositions, critical
   flags, and reviewer notes under `release_snapshots/`.
2. Reconcile every proposed current Misses/Defect and Unsafe grade with the current response,
   authoritative evidence, concrete shopper consequence, and comparable release precedents.
3. Treat accurate bounded answers as shopper successes even when retrieval is incomplete; grade
   the retrieval limitation separately. Do not turn harmless internal route differences or
   optional omitted detail into response defects.
4. Produce three views: the expanded-suite totals, the paired shared-turn result, and the new
   challenge cohort. Preserve published historical totals rather than rewriting them.

The reviewer must record the material failed conclusion for every response Miss and a plausible
harm pathway for every Unsafe qualifier. If the note cannot do that, the grade is too severe. See
the rubric's **Mandatory release calibration** section for the full severity anchors.

## Supported comparison workflows

- `scripts/run-local-paired-comparison.sh`: exact pinned agent/platform baseline versus the current
  local public stack. It restores the current stack after switching candidates.
- `scripts/run-shopper-api-comparison.sh`: deployed stage public API versus the local public API.
- `scripts/run-stage-client-test.sh`: sequential client-facing stage collection and review bundle.

These wrappers retain environment and endpoint safety checks. Exploratory outputs and caches stay
under `artifacts/` and are not committed.

For an explicitly authorized direct-WXO remote run, invoke `evaluation behavioral run` with the
exact `--url`, `--agent-id`, and `--allow-remote` arguments. The transport validates that the URL is
an IBM Cloud watsonx Orchestrate endpoint; no assets are imported or modified.

## Retrieval benchmark

```bash
uv run python -m evaluation retrieval run --arm control
uv run python -m evaluation retrieval run --arm structured
uv run python -m evaluation retrieval compare \
  --baseline artifacts/retrieval-evaluations/<control-run> \
  --challenger artifacts/retrieval-evaluations/<structured-run> \
  --output artifacts/retrieval-evaluations/<comparison>
```

## Issue replay and diagnostics

Replay an arbitrary review CSV against local WXO:

```bash
uv run python -m evaluation replay-csv /path/to/issues.csv --resume
```

Inspect one or more saved WXO artifacts with shopper-assistant-api's real normalizer:

```bash
uv run python -m evaluation inspect-artifact artifacts/e2e/<case>.json
```

Start an interactive local public-API session:

```bash
./scripts/local-chat.sh
```

The command automatically loads `CLIENT_API_KEYS_JSON` from
`../shopper-platform/apps/shopper-assistant-api/.env.local`; do not source that file. Use
`--env-file /other/path/.env.local` or `--context NAME` when needed.

## Release snapshots

The committed RC3.0, RC3.1, and RC4.0 directories are immutable historical evidence. Their schemas
differ, so the consolidated snapshot package keeps paired and standalone adapters while sharing
file, checksum, sanitization, and behavioral-summary code. The RC5.0 directory freezes the current
reviewed draft candidate; because the release remains a draft, a replacement run must regenerate
the snapshot, notes, and manifest together. Validate the release records with:

```bash
uv run pytest tests/evaluation/test_snapshots.py
```

Release deltas follow the continuity policy in the
[qualitative grading rubric](QUALITATIVE_GRADING_RUBRIC.md): keep published historical scores
immutable, compare the same shared turn IDs under one adjudicated behavior contract, and report
new issue-driven turns as a separate challenge cohort. If the live corpus oracle changes after a
run, retain the frozen run-corpus checksum and record the newer corpus checksum separately.
