# Deployment guide

Deployment is a two-stage process: import to Draft, then promote the tested snapshot to Live. The
shared `deployments/cloud.toml` contract pins the WXO tenant URL, logical agent, and both
environment IDs. The target manifests contain only their Draft- or Live-specific connection
configuration. Deployment stops if the local CLI alias resolves to a different tenant or if the
active tenant does not return the shared identities.

## WXO tenant and CLI setup

The tracked cloud tenant is the following nonproduction WXO instance, accessed through the
`shopperbroker` CLI profile:

- IBM Cloud resource name: `shopper-wxo-nonprod`
- Region: `us-south`
- Instance ID: `c32fc9ba-3122-4c61-a9b0-1d4aa43c9980`
- API URL:
  `https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/c32fc9ba-3122-4c61-a9b0-1d4aa43c9980`

Configure the non-secret local CLI pointer once on each operator or CI host:

```bash
.venv/bin/orchestrate env add \
  --name shopperbroker \
  --url https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/c32fc9ba-3122-4c61-a9b0-1d4aa43c9980 \
  --type ibm_iam
```

Do not run `env add` again when the profile already exists. Confirm it with
`.venv/bin/orchestrate env list`. The ignored `deployments/draft/operator.env` and
`deployments/live/operator.env` files contain only `WO_API_KEY`; tenant identity is not an operator
override. Cloud deployment commands activate `shopperbroker`, then verify its exact URL against
`deployments/cloud.toml` before changing any WXO asset.

## Pre-invoke inference credentials

The classifier and terminal-response composer call WXO's model gateway directly over HTTP using
`watsonx/openai/gpt-oss-120b`, low reasoning, and the existing JSON contracts. They use the team
connection `elevance-wxo-inference`; the WXO inference SDK and LangChain are not required.

Set `WXO_API_KEY` in each target's ignored `runtime-secrets.env` to an IBM Cloud service API key
authorized for the tracked WXO instance. This is a runtime credential, separate from the operator's
`WO_API_KEY` used to import or promote assets. Cloud preflight derives `WXO_API_URL` from the tracked
tenant in `deployments/cloud.toml`. The target manifests supply `WXO_MODEL_ID` and `WXO_IAM_URL`.
Local tools also call the cloud inference gateway; their URL is in the local manifest. No local
development token is needed for inference.

When upgrading from the watsonx.ai classifier, add `WXO_API_KEY` before deploying. The new manifests
import `src/wxo_connection.yaml` (or its local variant), populate the appropriate Draft/Live slot,
and bind the pre-invoke tool to it. They no longer provision the old
`elevance-watsonx-classifier` connection. Existing old connections can remain unused during rollout.
The classifier no longer needs `WATSONX_AI_API_KEY` or `WATSONX_AI_PROJECT_ID`; evaluation judges and
the separate platform services may still need their own watsonx.ai credentials.

The HTTP client reuses a session per thread. IAM tokens are cached in worker memory, scoped by
instance, issuer, and credential, and refreshed before expiry under a lock. One HTTP 401 permits
one token refresh and one additional inference send; a second 401 fails closed. Telemetry counts
every inference send, including that extra retry. Existing bounded generation retries remain for
transient failures and malformed output. These caches last only as long as the worker/module;
this change does not migrate tools to dedicated toolkits or guarantee warm workers.

## Environment topology and deployment order

| Application tier | Shopper Assistant API target | WXO agent state | RAG connection target |
| --- | --- | --- | --- |
| Dev | Tracked Draft environment ID | Draft | RAG API Dev |
| Stage | Tracked Live environment ID | Live release | RAG API Stage |

The deployment configurations enforce this mapping, but the applications are deployed
independently. For a coordinated release:

1. Deploy RAG API Dev and Stage.
2. Deploy the agent to Draft.
3. Validate Draft, then promote the agent to Live.
4. Deploy Shopper Assistant API Dev and Stage.

## Deploy to Draft

Deploy and verify RAG API Dev first. Set `RAG_API_KEY` in the ignored Draft runtime-secrets file to
a client key accepted by that API. Then run after merging to main. The command configures the Draft
connection with the tracked RAG Dev URL and API key, then imports connections, tools, and the agent:

```bash
./deployments/deploy-agent.sh --draft
```

`--dev` remains available as a deprecated alias for `--draft`.

The command captures the Live version before and after the import and fails if it changes. Its
credential-free receipt records the agent ID, Draft/Live environment IDs, and unchanged Live
version.

## Promote to live

Promote the exact verified RAG Dev image to RAG Stage first. Set the ignored Live runtime-secrets
`RAG_API_KEY` to a client key accepted by RAG Stage. Run this manually once the Draft agent and RAG
Stage have been verified. It configures the Live connection with the tracked RAG Stage URL and API
key, then promotes Draft to Live:

```bash
./deployments/promote-to-live.sh
```

The promotion fails unless the Live version advances and writes a receipt containing the version
transition. Shopper Assistant API Dev must use the Draft environment ID; Shopper Assistant API
Stage must use the Live environment ID. A normal agent promotion does not require changing either
Assistant API selector.

## Other environments

To deploy locally for development:

```bash
./deployments/deploy-agent.sh --local
```

## Purging an environment

To remove all deployed resources from an environment:

```bash
./deployments/purge-environment.sh --local
./deployments/purge-environment.sh --cloud
```

`--cloud` targets the shared `shopperbroker` tenant. Because agents, tools, and connections are
tenant-scoped, this affects both draft and live simultaneously.
