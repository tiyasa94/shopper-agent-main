#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
ROOT_DIR="${SCRIPT_DIR}/.."
ORCHESTRATE_BIN="${ROOT_DIR}/.venv/bin/orchestrate"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"

usage() {
  echo "Usage: $(basename "$0") --local | --draft" >&2
  exit 2
}

case "${1:-}" in
  --local) TARGET="local" ;;
  --draft) TARGET="draft" ;;
  --dev)
    TARGET="draft"
    echo "Warning: --dev is deprecated; use --draft." >&2
    ;;
  *)       usage ;;
esac

MANIFEST="${SCRIPT_DIR}/${TARGET}/manifest.toml"
CLOUD_MANIFEST="${SCRIPT_DIR}/cloud.toml"
SECRETS_FILE="${DEPLOY_RUNTIME_SECRETS_FILE:-${SCRIPT_DIR}/${TARGET}/runtime-secrets.env}"
OPERATOR_FILE="${SCRIPT_DIR}/${TARGET}/operator.env"
DEPLOY_ENV="$(mktemp)"
WXO_BEFORE_SNAPSHOT="$(mktemp)"
WXO_AFTER_SNAPSHOT="$(mktemp)"
trap 'rm -f "${DEPLOY_ENV}" "${WXO_BEFORE_SNAPSHOT}" "${WXO_AFTER_SNAPSHOT}"' EXIT

if [[ ! -f "${SECRETS_FILE}" ]]; then
  echo "Error: missing ${SECRETS_FILE}" >&2
  echo "Copy ${SECRETS_FILE}.example to ${SECRETS_FILE} and populate it." >&2
  exit 1
fi

if [[ "${TARGET}" != "local" ]] && [[ ! -f "${OPERATOR_FILE}" ]]; then
  echo "Error: missing ${OPERATOR_FILE}" >&2
  echo "Copy ${OPERATOR_FILE}.example to ${OPERATOR_FILE} and populate it." >&2
  exit 1
fi

preflight_args=("${MANIFEST}" "${SECRETS_FILE}" "${DEPLOY_ENV}")
if [[ "${TARGET}" != "local" ]]; then
  preflight_args+=("${CLOUD_MANIFEST}")
fi
"${PYTHON_BIN}" "${SCRIPT_DIR}/preflight.py" "${preflight_args[@]}"

set -a
if [[ "${TARGET}" != "local" ]]; then
  # shellcheck source=/dev/null
  source "${OPERATOR_FILE}"
fi
# Source tracked deployment values last so operator files cannot override the target identity.
# shellcheck source=/dev/null
source "${DEPLOY_ENV}"
set +a

if [[ "${TARGET}" == "local" ]]; then
  "${ORCHESTRATE_BIN}" env activate local
else
  "${ORCHESTRATE_BIN}" env activate "${WO_ENVIRONMENT_NAME}" --api-key "${WO_API_KEY}"
  "${PYTHON_BIN}" "${ROOT_DIR}/scripts/wxo_environment_contract.py" verify-cli-profile \
    --profile-name "${WO_ENVIRONMENT_NAME}" \
    --expected-url "${WXO_TENANT_API_URL}"
fi
echo "Deploy target activated: ${WO_ENVIRONMENT_NAME}"

if [[ "${TARGET}" == "draft" ]]; then
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
fi

echo "Importing connections into ${WO_ENVIRONMENT_NAME}..."
for spec in ${CONNECTION_SPECS}; do
  "${ORCHESTRATE_BIN}" connections import -f "${spec}"
done

for i in $(seq 0 $(( SET_CREDS_COUNT - 1 ))); do
  var="SET_CREDS_${i}"
  read -r app_id env_slot cred_args <<< "${!var}"
  cred_flags=()
  for kv in ${cred_args}; do
    cred_flags+=(-e "${kv}")
  done
  if [[ "${TARGET}" == "local" && "${app_id}" == "elevance-rag-tool-anthem" ]]; then
    cred_flags+=(-e "RAG_API_BASE_URL=http://host.lima.internal:${LOCAL_RAG_API_PORT:-8081}")
  fi
  "${ORCHESTRATE_BIN}" connections set-credentials \
    -a "${app_id}" --env "${env_slot}" "${cred_flags[@]}"
done

echo "Importing tools into ${WO_ENVIRONMENT_NAME}..."
for i in $(seq 0 $(( TOOL_COUNT - 1 ))); do
  var="TOOL_${i}"
  read -r tool_file app_id <<< "${!var}"
  import_command=(
    "${ORCHESTRATE_BIN}" tools import -k python
    -f "${tool_file}"
    -p "${TOOLS_PACKAGE_DIR}"
    -r "${TOOLS_REQUIREMENTS}"
  )
  if [[ -n "${app_id}" ]]; then
    import_command+=(-a "${app_id}")
  fi
  "${import_command[@]}"
done

echo "Importing agent into ${WO_ENVIRONMENT_NAME}..."
for agent_file in ${AGENT_FILES}; do
  "${ORCHESTRATE_BIN}" agents import -f "${agent_file}"
done

if [[ "${TARGET}" == "draft" ]]; then
  "${PYTHON_BIN}" "${ROOT_DIR}/scripts/wxo_environment_contract.py" inspect \
    --agent-name "${agent_name}" \
    --expected-agent-id "${WXO_AGENT_ID}" \
    --expected-draft-environment-id "${WXO_DRAFT_ENVIRONMENT_ID}" \
    --expected-live-environment-id "${WXO_LIVE_ENVIRONMENT_ID}" \
    --output "${WXO_AFTER_SNAPSHOT}"
  "${PYTHON_BIN}" "${ROOT_DIR}/scripts/wxo_environment_contract.py" assert-transition \
    --before "${WXO_BEFORE_SNAPSHOT}" \
    --after "${WXO_AFTER_SNAPSHOT}" \
    --operation draft_import
fi

IMPORT_RECEIPT_TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
IMPORT_RECEIPT_PATH="${ROOT_DIR}/artifacts/import-receipts/${TARGET}-${IMPORT_RECEIPT_TIMESTAMP}.json"
receipt_args=(
  --root "${ROOT_DIR}"
  --target-environment "${TARGET}"
  --output "${IMPORT_RECEIPT_PATH}"
)
if [[ "${TARGET}" == "draft" ]]; then
  receipt_args+=(
    --operation draft_import
    --before-snapshot "${WXO_BEFORE_SNAPSHOT}"
    --after-snapshot "${WXO_AFTER_SNAPSHOT}"
  )
fi
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/write_import_receipt.py" "${receipt_args[@]}"
echo "Import receipt: ${IMPORT_RECEIPT_PATH}"
echo "Shopper agent deployed to ${WO_ENVIRONMENT_NAME}."
