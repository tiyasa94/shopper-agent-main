# Shopper Agent E2E

E2E proves that the locally running components can complete representative shopper workflows. It
does not grade response quality or exhaustively test agent decisions; those responsibilities belong
to the behavioral evaluation suite.

The release profile sends seven isolated workflows containing eight total turns through Shopper
Assistant API, WXO, the shopper agent, and the configured RAG and Plans APIs. It covers:

- authentication and a basic no-tool response
- general-document retrieval
- named-plan and current-plan document retrieval
- IOLS and MOLS structured details
- multi-turn plan continuity
- public response, metadata, evidence, request ID, and run ID contracts

Assertions are limited to durable integration facts: valid response structure, expected service
actions, catalog IDs, detail types, evidence propagation, and session continuity. Response wording,
semantic sufficiency, clarification choices, recommendation boundaries, and repeated reliability
trials are evaluated by `tests/evaluation/behavioral_questions.yaml`.

## Running locally

Start the local platform, then run the three-workflow smoke set or complete functional set:

```bash
./scripts/local-platform.sh start
./scripts/run-local-e2e.sh smoke
./scripts/run-local-e2e.sh full
```

The optional `wxo` profile runs two requests directly against WXO to isolate tool wiring from
Shopper Assistant API:

```bash
./scripts/run-local-e2e.sh wxo
```

Extra arguments are forwarded to pytest:

```bash
./scripts/run-local-e2e.sh full -k structured
```

Ordinary `pytest` excludes live modules unless `--live-e2e` is supplied. RAG API endpoint contracts
live with the RAG API in `../shopper-platform/apps/rag-api/tests`; synthetic tool and normalization
contracts remain deterministic tests in this repository.
