# Release evaluation snapshots

This directory contains immutable, sanitized evaluation evidence for named release candidates.
Exploratory runs remain under the gitignored `artifacts/` directory.

Each release snapshot retains the paired questions, generated responses, public evaluation
observations, evidence excerpts, component grades, review rationale, aggregate metrics, provenance,
and checksums needed for later comparison. It excludes credentials, runtime context, internal tool
responses, and WXO request, run, and trace identifiers.

An unreleased candidate may have a draft notes file and a `docs/releases/manifest.yaml` entry with
`artifacts_committed: false`, null deployment/results fields, and a planned snapshot path. Do not
create a placeholder snapshot: add the directory only after the frozen run and reviews are complete,
then replace the draft fields with measured provenance and validation results.

Standalone release validations retain the same evidence without requiring an unchanged comparison
candidate. They also freeze the exact question corpus and record multi-turn conversation grades.

Create a snapshot after completing and reviewing both evaluation arms:

```bash
uv run python -m evaluation snapshot-paired create \
  --release rc3.0 \
  --released-on 2026-07-27 \
  --baseline artifacts/evaluations/<baseline-run> \
  --candidate artifacts/evaluations/<candidate-run> \
  --review artifacts/evaluations/<comparison>/component-review.csv \
  --output tests/evaluation/release_snapshots/<release>
```

The command refuses to overwrite an existing snapshot unless `--force` is supplied. Use `--force`
only while preparing an unreleased candidate; committed release snapshots are append-only.
An already enriched `component-review.csv` from a release snapshot is also accepted as `--review`
when regenerating checksums or migrating the snapshot schema.

Validate a frozen snapshot without calling WXO or another external service:

```bash
uv run python -m evaluation snapshot-paired validate \
  --snapshot tests/evaluation/release_snapshots/rc3.0
```

Future candidate evaluations can use the committed responses as a cached behavioral baseline.
The old deployed agent only needs to be rerun when that baseline asset or evaluation corpus changes.

Materialize either frozen candidate into the format accepted by `evaluation behavioral compare`:

```bash
uv run python -m evaluation snapshot-paired materialize \
  --snapshot tests/evaluation/release_snapshots/rc3.0 \
  --side candidate \
  --output artifacts/evaluations/rc3.0-frozen-baseline
```

Create a standalone snapshot after its turn and conversation reviews are complete:

```bash
uv run python -m evaluation snapshot-standalone create \
  --release rc3.1 \
  --released-on 2026-07-29 \
  --run artifacts/stage-client-tests/<run> \
  --review artifacts/stage-client-tests/<run>/client-review.csv \
  --critical-review artifacts/stage-client-tests/<critical-review.csv> \
  --conversation-review artifacts/stage-client-tests/<conversation-review.csv> \
  --corpus tests/evaluation/behavioral_questions.yaml \
  --output tests/evaluation/release_snapshots/rc3.1
```

Validate it without calling an external service:

```bash
uv run python -m evaluation snapshot-standalone validate \
  --snapshot tests/evaluation/release_snapshots/rc3.1
```
