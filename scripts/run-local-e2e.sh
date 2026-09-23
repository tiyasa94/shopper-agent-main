#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
ROOT_DIR="${SCRIPT_DIR}/.."
SRC_DIR="${ROOT_DIR}/src"
ORCHESTRATE_BIN="${ROOT_DIR}/.venv/bin/orchestrate"
TOOLS_DIR="${ROOT_DIR}/src/tools"
AGENT_FILE="${ROOT_DIR}/src/agent.yaml"
TOOL_REQUIREMENTS="${ROOT_DIR}/src/requirements.txt"
WORKFLOW_TEST="${ROOT_DIR}/tests/e2e/test_local_shopper_api_workflows.py"
WXO_DIAGNOSTIC_TEST="${ROOT_DIR}/tests/e2e/test_wxo_diagnostic.py"
CLASSIFIER_APP_ID="elevance-wxo-inference"
RAG_APP_ID="elevance-rag-tool-anthem"
PROFILE="${1:-smoke}"
if [[ $# -gt 0 ]]; then
  shift
fi
PYTEST_ARGS=("$@")
PYTEST_ARG_COUNT=$#

require_local_environment() {
  local active_env_list
  active_env_list=$("${ORCHESTRATE_BIN}" env list)
  if ! awk '$1 == "local" && /\(active\)/ { found = 1 } END { exit !found }' \
    <<< "${active_env_list}"; then
    echo "Error: the active Orchestrate environment is not local." >&2
    exit 1
  fi
}

require_shopper_api() {
  if ! curl -fsS "${LOCAL_SHOPPER_API_URL:-http://127.0.0.1:8082}/readyz" \
    >/dev/null 2>&1; then
    cat >&2 <<'EOF'
Local Shopper Assistant API is not ready. Start the complete stack first:
  ./scripts/local-platform.sh start
EOF
    exit 1
  fi
}

refresh_agent() {
  local tool
  for tool in plan_details.py plan_search.py general_search.py; do
    "${ORCHESTRATE_BIN}" tools import -k python \
      -f "${TOOLS_DIR}/${tool}" \
      -p "${SRC_DIR}" \
      -a "${RAG_APP_ID}" \
      -r "${TOOL_REQUIREMENTS}" >/dev/null
  done
  "${ORCHESTRATE_BIN}" tools import -k python \
    -f "${TOOLS_DIR}/guardrail_plugin.py" \
    -p "${SRC_DIR}" \
    -a "${CLASSIFIER_APP_ID}" \
    -r "${TOOL_REQUIREMENTS}" >/dev/null
  "${ORCHESTRATE_BIN}" tools import -k python \
    -f "${TOOLS_DIR}/addendum_plugin.py" \
    -p "${SRC_DIR}" \
    -r "${TOOL_REQUIREMENTS}" >/dev/null
  "${ORCHESTRATE_BIN}" agents import -f "${AGENT_FILE}" >/dev/null
}

check_local_postgres_capacity() {
  command -v limactl >/dev/null 2>&1 || return 0

  local pg_snapshot
  local pg_limit
  local pg_used
  if ! pg_snapshot=$(limactl shell ibm-watsonx-orchestrate \
    docker exec dev-edition-wxo-server-db-1 \
    psql -U postgres -AtF '|' \
    -c "SELECT current_setting('max_connections'), count(*) FROM pg_stat_activity;" \
    2>/dev/null); then
    return 0
  fi
  pg_snapshot="${pg_snapshot##*$'\n'}"
  IFS='|' read -r pg_limit pg_used <<< "${pg_snapshot}"

  if [[ "${pg_limit}" =~ ^[0-9]+$ ]] && (( pg_limit < 200 )); then
    cat >&2 <<EOF
Local Orchestrate PostgreSQL max_connections is ${pg_limit}; agent E2E requires at least 200.
Apply the one-time local Developer Edition fix in docs/local-dev-setup.md, restart
the DB-dependent local services, and rerun this profile. No cloud action is required.
EOF
    exit 1
  fi

  if [[ "${pg_limit}" =~ ^[0-9]+$ && "${pg_used}" =~ ^[0-9]+$ ]] \
    && (( pg_limit - pg_used < 20 )); then
    cat >&2 <<EOF
Local Orchestrate PostgreSQL has only $((pg_limit - pg_used)) free connections
(${pg_used}/${pg_limit} in use). Restart the local WXO DB-dependent services before
running agent E2E; otherwise the run can fail with SQLSTATE 53300.
EOF
    exit 1
  fi
}

run_live_test() {
  local test_path=$1
  if (( PYTEST_ARG_COUNT > 0 )); then
    uv run pytest --live-e2e "${test_path}" -v -s "${PYTEST_ARGS[@]}"
  else
    uv run pytest --live-e2e "${test_path}" -v -s
  fi
}

run_workflows() (
  export E2E_WORKFLOW_SET=$1
  run_live_test "${WORKFLOW_TEST}"
)

prepare_local_e2e() {
  require_local_environment
  check_local_postgres_capacity
  refresh_agent
}

cd "${ROOT_DIR}"
case "${PROFILE}" in
  smoke)
    prepare_local_e2e
    require_shopper_api
    run_workflows smoke
    ;;
  full)
    prepare_local_e2e
    require_shopper_api
    run_workflows full
    ;;
  wxo)
    prepare_local_e2e
    run_live_test "${WXO_DIAGNOSTIC_TEST}"
    ;;
  *)
    echo "Usage: $0 {smoke|full|wxo} [pytest args...]" >&2
    exit 2
    ;;
esac
