# Tests

- `unit/shopper_agent/`: agent YAML, pre-invoke, and registered search adapters
- `unit/rag_client/`: shared RAG HTTP client and response-contract validation
- `unit/audit/`: synchronous structured audit receipts
- `e2e/`: deterministic contracts plus the minimal opt-in local Shopper API workflow suite
- `evaluation/`: curated behavioral corpora, release snapshots, and their contract tests

Runnable clients and reporting workflows live in the top-level `evaluation/` package.

Run the default deterministic suite with `uv run pytest`. Networked E2E tests are skipped unless
their explicit environment flags are set; prefer `scripts/run-local-e2e.sh` for those profiles.
