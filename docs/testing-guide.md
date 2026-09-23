# Testing

## Deterministic suite

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Focused source tests:

```bash
uv run pytest tests/unit/shopper_agent tests/unit/rag_client -q
```

## Local WXO and API E2E

Live tests are not collected by ordinary `pytest`. The local runner enables them explicitly and
refuses a non-local active WXO environment. The release profile validates only representative
cross-service workflows; behavioral quality belongs to evaluation.

```bash
./scripts/run-local-e2e.sh smoke
./scripts/run-local-e2e.sh full
```

`smoke` runs three Shopper Assistant API workflows. `full` runs seven workflows containing eight
turns across general retrieval, plan retrieval, structured IOLS and MOLS details, and conversation
continuity. Normal pytest filters can narrow the set:

```bash
./scripts/run-local-e2e.sh full -k structured
```

The optional `wxo` profile sends two diagnostic requests directly to WXO. It is not part of the
release E2E profile:

```bash
./scripts/run-local-e2e.sh wxo
```

RAG API endpoint contracts live in the RAG API repository. Agent behavior, response quality,
clarification, grounding, and reliability are measured only by evaluation.

## Behavioral and retrieval evaluation

Use the consolidated CLI for evaluation, issue replay, snapshots, and diagnostics:

```bash
uv run python -m evaluation --help
```

Version-controlled corpora and detailed workflow guidance are in
`tests/evaluation/README.md`.

## CI

`ci_cd_scripts/bamboo_build.sh` runs Ruff, the deterministic suite, an 85% coverage floor, and
SonarQube analysis. It writes `artifacts/coverage.xml` and `artifacts/junit.xml`.
