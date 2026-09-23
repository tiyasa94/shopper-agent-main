# Local development

## Prerequisites

- Python 3.13 and `uv`
- watsonx Orchestrate Developer Edition at `http://127.0.0.1:4321`
- Podman for the local RAG and Shopper Assistant API services
- A sibling `../shopper-platform` checkout

## Configure

```bash
uv sync
cp .env.local.example .env.local
chmod 600 .env.local
cp deployments/local/runtime-secrets.env.example deployments/local/runtime-secrets.env
chmod 600 deployments/local/runtime-secrets.env
```

Fill in `.env.local` for the local platform and evaluation scripts. Populate
`deployments/local/runtime-secrets.env` with the RAG API client key and the WXO service API key (`WXO_API_KEY`)
used by the pre-invoke connection. Local tools call the cloud WXO inference gateway with IBM IAM
authentication; they do not require a local gateway token. The local stack registers `RAG_API_KEY` in the RAG
API's `CLIENT_API_KEYS_JSON` and configures the WXO connection with that same key. The deployment
preflight exits before importing anything when a required value is missing.

## Import the canonical agent locally

```bash
./deployments/deploy-agent.sh --local
```

The deployment activates the local environment, validates `deployments/local/manifest.toml`, then
imports two connections, three Python entry points, and `src/agent.yaml`. It does not import a flow
or a durable audit tool.

## Run the complete local platform

```bash
./scripts/local-platform.sh start
./scripts/local-platform.sh status
```

This starts local PostgreSQL, RAG API, and Shopper Assistant API containers while retaining the
locally managed WXO server. Default endpoints are RAG `:8081`, Shopper API `:8082`, and WXO `:4321`.

Stop the containers with:

```bash
./scripts/local-platform.sh stop
```

No local command in this workflow imports or deploys to the live ShopperBroker environment.

## Increase PostgreSQL `max_connections` (one-time fix)

The WXO Developer Edition ships with `max_connections = 100` in its bundled PostgreSQL container.
The agent E2E suite requires at least 200. Apply this fix once after the first server start:

```bash
# 1. Increase the limit
limactl shell ibm-watsonx-orchestrate -- bash -c \
  "docker exec dev-edition-wxo-server-db-1 psql -U postgres -c \
   'ALTER SYSTEM SET max_connections = 200;'"

# 2. Restart the DB container to apply
limactl shell ibm-watsonx-orchestrate -- bash -c \
  "docker restart dev-edition-wxo-server-db-1"

# 3. Wait ~15 s then verify
sleep 15
limactl shell ibm-watsonx-orchestrate -- bash -c \
  "docker exec dev-edition-wxo-server-db-1 psql -U postgres \
   -c \"SELECT setting FROM pg_settings WHERE name='max_connections';\""
# → setting: 200
```

This setting persists inside the Lima VM volume. You only need to do this once per machine. If you
fully destroy and recreate the Lima VM you will need to redo it. After applying, restart the
DB-dependent WXO services:

```bash
limactl shell ibm-watsonx-orchestrate -- bash -c \
  "docker restart dev-edition-wxo-server-1 dev-edition-wxo-tempus-runtime-1"
```
