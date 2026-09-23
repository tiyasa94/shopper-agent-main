#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
ROOT_DIR="${SCRIPT_DIR}/.."
ENV_FILE="${ROOT_DIR}/.env.local"
ORCHESTRATE_BIN="${ROOT_DIR}/.venv/bin/orchestrate"
TARGET_WXO_ENV="${WXO_TARGET_ENV:-}"
PROFILE="${1:-smoke}"
if [[ $# -gt 0 ]]; then
  shift
fi

if [[ -z "${TARGET_WXO_ENV}" || "${TARGET_WXO_ENV}" == "local" ]]; then
  echo "Error: set WXO_TARGET_ENV to the exact non-local environment name." >&2
  exit 1
fi

ACTIVE_ENV_LIST="$("${ORCHESTRATE_BIN}" env list)"
if ! awk -v target="${TARGET_WXO_ENV}" \
  '$1 == target && /\(active\)/ { found = 1 } END { exit !found }' \
  <<< "${ACTIVE_ENV_LIST}"; then
  echo "Error: active Orchestrate environment does not match '${TARGET_WXO_ENV}'." >&2
  exit 1
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Error: missing ${ENV_FILE}." >&2
  exit 1
fi
set -o allexport
# shellcheck source=/dev/null
source "${ENV_FILE}"
set +o allexport
WXO_API_URL="${WXO_API_URL:-${WO_CLOUD_INSTANCE:-${WO_INSTANCE:-}}}"
WXO_API_KEY="${WXO_API_KEY:-${WO_API_KEY:-}}"
: "${WXO_API_URL:?Missing WXO_API_URL, WO_CLOUD_INSTANCE, or WO_INSTANCE in .env.local}"
: "${WXO_API_KEY:?Missing WXO_API_KEY or WO_API_KEY in .env.local}"
export WXO_API_KEY

case "${WXO_API_URL}" in
  https://*.watson-orchestrate.cloud.ibm.com/*) ;;
  *)
    echo "Error: WXO_API_URL is not an IBM Cloud watsonx Orchestrate instance URL." >&2
    exit 1
    ;;
esac

if [[ "${PROFILE}" != "smoke" ]]; then
  echo "Usage: $0 smoke [pytest args...]" >&2
  exit 2
fi

export LOCAL_WXO_ALLOW_REMOTE=1
export LOCAL_WXO_URL="${WXO_API_URL}"
export LOCAL_WXO_AGENT_NAME="${LOCAL_WXO_AGENT_NAME:-Elevance_Health_Shopper_Portal}"
echo "Remote E2E target verified: ${TARGET_WXO_ENV}"
echo "Agent: ${LOCAL_WXO_AGENT_NAME}"
if [[ -n "${LOCAL_WXO_AGENT_ID:-}" ]]; then
  echo "Agent ID: ${LOCAL_WXO_AGENT_ID}"
fi
cd "${ROOT_DIR}"
exec uv run pytest --live-e2e "${ROOT_DIR}/tests/e2e/test_wxo_diagnostic.py" -v -s "$@"
