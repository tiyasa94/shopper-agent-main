#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
ROOT_DIR="${SCRIPT_DIR}/.."
ORCHESTRATE_BIN="${ROOT_DIR}/.venv/bin/orchestrate"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"

usage() {
  echo "Usage: $(basename "$0")" >&2
  echo "Promotes the current draft agent in the shopper-broker environment to live." >&2
  exit 2
}

MANIFEST="${SCRIPT_DIR}/live/manifest.toml"
CLOUD_MANIFEST="${SCRIPT_DIR}/cloud.toml"
SECRETS_FILE="${SCRIPT_DIR}/live/runtime-secrets.env"
OPERATOR_FILE="${SCRIPT_DIR}/live/operator.env"
DEPLOY_ENV="$(mktemp)"
WXO_BEFORE_SNAPSHOT="$(mktemp)"
WXO_AFTER_SNAPSHOT="$(mktemp)"
trap 'rm -f "${DEPLOY_ENV}" "${WXO_BEFORE_SNAPSHOT}" "${WXO_AFTER_SNAPSHOT}"' EXIT

if [[ ! -f "${SECRETS_FILE}" ]]; then
  echo "Error: missing ${SECRETS_FILE}" >&2
  echo "Copy ${SECRETS_FILE}.example to ${SECRETS_FILE} and populate it." >&2
  exit 1
fi
if [[ ! -f "${OPERATOR_FILE}" ]]; then
  echo "Error: missing ${OPERATOR_FILE}" >&2
  echo "Copy ${OPERATOR_FILE}.example to ${OPERATOR_FILE} and populate it." >&2
  exit 1
fi

"${PYTHON_BIN}" "${SCRIPT_DIR}/preflight.py" \
  "${MANIFEST}" "${SECRETS_FILE}" "${DEPLOY_ENV}" "${CLOUD_MANIFEST}"

set -a
# shellcheck source=/dev/null
source "${OPERATOR_FILE}"
# Source tracked deployment values last so operator files cannot override the target identity.
# shellcheck source=/dev/null
source "${DEPLOY_ENV}"
set +a

"${ORCHESTRATE_BIN}" env activate "${WO_ENVIRONMENT_NAME}" --api-key "${WO_API_KEY}"
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/wxo_environment_contract.py" verify-cli-profile \
  --profile-name "${WO_ENVIRONMENT_NAME}" \
  --expected-url "${WXO_TENANT_API_URL}"
echo "Promote target activated: ${WO_ENVIRONMENT_NAME}"

read -r agent_file extra_agent_file <<< "${AGENT_FILES}"
if [[ -z "${agent_file:-}" || -n "${extra_agent_file:-}" ]]; then
  echo "Error: the cloud deployment contract requires exactly one agent file." >&2
  exit 1
fi
agent_name=$("${PYTHON_BIN}" -c "import yaml,sys; print(yaml.safe_load(open(sys.argv[1]))['name'])" "${agent_file}")
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/wxo_environment_contract.py" inspect \
  --agent-name "${agent_name}" \
  --expected-agent-id "${WXO_AGENT_ID}" \
  --expected-draft-environment-id "${WXO_DRAFT_ENVIRONMENT_ID}" \
  --expected-live-environment-id "${WXO_LIVE_ENVIRONMENT_ID}" \
  --output "${WXO_BEFORE_SNAPSHOT}"

echo "Configuring live credentials in ${WO_ENVIRONMENT_NAME}..."
for i in $(seq 0 $(( SET_CREDS_COUNT - 1 ))); do
  var="SET_CREDS_${i}"
  read -r app_id env_slot cred_args <<< "${!var}"
  cred_flags=()
  for kv in ${cred_args}; do
    cred_flags+=(-e "${kv}")
  done
  "${ORCHESTRATE_BIN}" connections set-credentials \
    -a "${app_id}" --env "${env_slot}" "${cred_flags[@]}"
done

echo "Deploying agents to live in ${WO_ENVIRONMENT_NAME}..."
for agent_file in ${AGENT_FILES}; do
  "${ORCHESTRATE_BIN}" agents deploy --name "${agent_name}"
  echo "Agent '${agent_name}' promoted to live in ${WO_ENVIRONMENT_NAME}."
done

"${PYTHON_BIN}" "${ROOT_DIR}/scripts/wxo_environment_contract.py" inspect \
  --agent-name "${agent_name}" \
  --expected-agent-id "${WXO_AGENT_ID}" \
  --expected-draft-environment-id "${WXO_DRAFT_ENVIRONMENT_ID}" \
  --expected-live-environment-id "${WXO_LIVE_ENVIRONMENT_ID}" \
  --output "${WXO_AFTER_SNAPSHOT}"
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/wxo_environment_contract.py" assert-transition \
  --before "${WXO_BEFORE_SNAPSHOT}" \
  --after "${WXO_AFTER_SNAPSHOT}" \
  --operation live_promotion

PROMOTION_RECEIPT_TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PROMOTION_RECEIPT_PATH="${ROOT_DIR}/artifacts/import-receipts/live-${PROMOTION_RECEIPT_TIMESTAMP}.json"
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/write_import_receipt.py" \
  --root "${ROOT_DIR}" \
  --target-environment live \
  --operation live_promotion \
  --before-snapshot "${WXO_BEFORE_SNAPSHOT}" \
  --after-snapshot "${WXO_AFTER_SNAPSHOT}" \
  --output "${PROMOTION_RECEIPT_PATH}"
echo "Promotion receipt: ${PROMOTION_RECEIPT_PATH}"
