#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
ROOT_DIR="${SCRIPT_DIR}/.."
ORCHESTRATE_BIN="${ROOT_DIR}/.venv/bin/orchestrate"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"

usage() {
  echo "Usage: $(basename "$0") --local | --cloud" >&2
  echo "" >&2
  echo "  --local   Remove all resources from the local WXO Developer Edition." >&2
  echo "  --cloud   Remove all resources from the shared cloud tenant (shopper-broker)." >&2
  echo "            WARNING: This affects both draft and live — agents, tools, and" >&2
  echo "            connections are tenant-scoped and cannot be selectively removed." >&2
  exit 2
}

case "${1:-}" in
  --local) TARGET="local" ;;
  --cloud) TARGET="draft" ;;
  *)       usage ;;
esac

MANIFEST="${SCRIPT_DIR}/${TARGET}/manifest.toml"
CLOUD_MANIFEST="${SCRIPT_DIR}/cloud.toml"
SECRETS_FILE="${SCRIPT_DIR}/${TARGET}/runtime-secrets.env"
OPERATOR_FILE="${SCRIPT_DIR}/${TARGET}/operator.env"
DEPLOY_ENV="$(mktemp)"
trap 'rm -f "${DEPLOY_ENV}"' EXIT

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
echo "Purge target activated: ${WO_ENVIRONMENT_NAME}"

FAILURES=()

echo "Deleting agents from ${WO_ENVIRONMENT_NAME}..."
for agent_file in ${AGENT_FILES}; do
  agent_name=$("${PYTHON_BIN}" -c "import yaml,sys; print(yaml.safe_load(open(sys.argv[1]))['name'])" "${agent_file}")
  agent_kind=$("${PYTHON_BIN}" -c "import yaml,sys; print(yaml.safe_load(open(sys.argv[1]))['kind'])" "${agent_file}")
  if ! output=$("${ORCHESTRATE_BIN}" agents remove -n "${agent_name}" -k "${agent_kind}" 2>&1); then
    echo "${output}" >&2
    FAILURES+=("agent: ${agent_name}")
  else
    echo "${output}"
  fi
done

echo "Deleting tools from ${WO_ENVIRONMENT_NAME}..."
for i in $(seq 0 $(( TOOL_COUNT - 1 ))); do
  var="TOOL_NAME_${i}"
  tool_name="${!var}"
  if ! output=$("${ORCHESTRATE_BIN}" tools remove -n "${tool_name}" 2>&1); then
    echo "${output}" >&2
    FAILURES+=("tool: ${tool_name}")
  else
    echo "${output}"
  fi
done

echo "Deleting connections from ${WO_ENVIRONMENT_NAME}..."
for spec in ${CONNECTION_SPECS}; do
  app_id=$("${PYTHON_BIN}" -c "import yaml,sys; print(yaml.safe_load(open(sys.argv[1]))['app_id'])" "${spec}")
  if ! output=$("${ORCHESTRATE_BIN}" connections remove -a "${app_id}" 2>&1); then
    echo "${output}"
    if ! echo "${output}" | grep -qi "not found"; then
      FAILURES+=("connection: ${app_id}")
    fi
  else
    echo "${output}"
  fi
done

if [[ ${#FAILURES[@]} -gt 0 ]]; then
  echo "" >&2
  echo "Purge failed for ${#FAILURES[@]} resource(s): ${FAILURES[*]}" >&2
  exit 1
fi

echo "Purge complete for ${WO_ENVIRONMENT_NAME}."
