# Shopper Agent

The watsonx Orchestrate agent used by the Shopper Assistant API. The repository contains one native
agent, a pre-invoke guardrail plugin, scoped plan/general search tools, and their shared Python
implementation.

## Setup

Requirements: Python 3.13 and `uv`.

```bash
uv sync
cp .env.local.example .env.local
```

Populate `.env.local` with local service and evaluation configuration. Target-specific connection
credentials belong in the ignored `deployments/<target>/runtime-secrets.env` files; import scripts
do not read environment files from `shopper-platform`.

## Source layout

```text
src/
├── agent.yaml
├── rag_api_connection.yaml
├── wxo_connection.yaml
├── requirements.txt
├── tools/
│   ├── guardrail_plugin.py
│   ├── addendum_plugin.py
│   ├── plan_search.py
│   └── general_search.py
└── shared/
    ├── audit.py
    ├── classifier.py
    ├── context.py
    ├── guardrails.py
    └── rag.py

evaluation/
├── cli.py
├── behavioral.py
├── retrieval.py
├── contexts.py
├── clients/
└── snapshots/
```

## Local development

Deploy the agent to the local WXO Developer Edition:

```bash
cp deployments/local/runtime-secrets.env.example deployments/local/runtime-secrets.env
# populate runtime-secrets.env, then:
./deployments/deploy-agent.sh --local
```

Run deterministic checks with 100% branch coverage:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=src --cov-branch --cov-fail-under=100
```

The complete local platform uses services from the `shopper-platform` repository. Before starting
it, clone that repository alongside this one at `../shopper-platform`, or set
`SHOPPER_PLATFORM_ROOT` to the checkout's path.

Run the complete local platform and its minimal functional E2E workflows:

```bash
./scripts/local-platform.sh start
./scripts/run-local-e2e.sh full
```

Chat through the locally running public API. The command reads the API key directly from the
sibling shopper-platform `.env.local`, so no `source`, `jq`, or manual export is needed:

```bash
./scripts/local-chat.sh
```

List behavioral, retrieval, replay, snapshot, and diagnostic evaluation commands:

```bash
uv run python -m evaluation --help
```

See [docs/README.md](docs/README.md) for detailed contracts and test guidance.
