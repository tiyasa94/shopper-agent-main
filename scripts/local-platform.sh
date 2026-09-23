#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
ROOT_DIR="${SCRIPT_DIR}/.."
PLATFORM_ROOT="${SHOPPER_PLATFORM_ROOT:-${ROOT_DIR}/../shopper-platform}"
RAG_ROOT="${PLATFORM_ROOT}/apps/rag-api"
SHOPPER_API_ROOT="${PLATFORM_ROOT}/apps/shopper-assistant-api"
STATE_DIR="${ROOT_DIR}/.local-stack"
COMPOSE_FILE="${ROOT_DIR}/compose.local.yaml"
COMPOSE_OVERRIDE="${STATE_DIR}/compose.runtime.json"
RENDERER="${SCRIPT_DIR}/render-local-compose.py"
ORCHESTRATE_BIN="${ROOT_DIR}/.venv/bin/orchestrate"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
AGENT_ENV_FILE="${ROOT_DIR}/.env.local"
AGENT_SECRETS_FILE="${ROOT_DIR}/deployments/local/runtime-secrets.env"
LOCAL_AGENT_NAME="${LOCAL_WXO_AGENT_NAME:-Elevance_Health_Shopper_Portal}"
RAG_PORT="${LOCAL_RAG_API_PORT:-8081}"
SHOPPER_API_PORT="${LOCAL_SHOPPER_API_PORT:-8082}"
ACTION="${1:-status}"

usage() {
  cat <<'EOF'
Usage: ./scripts/local-platform.sh {start|run|stop|restart|status|logs|config|configure-wxo}

Podman Compose manages PostgreSQL, RAG API, and Shopper Assistant API. WXO Developer
Edition remains managed by `orchestrate server` in its Lima VM.

Published endpoints:
  RAG API                 http://127.0.0.1:8081
  Shopper Assistant API   http://127.0.0.1:8082
  WXO Developer Edition   http://127.0.0.1:4321

Environment overrides:
  SHOPPER_PLATFORM_ROOT    Path to the shopper-platform checkout.
  LOCAL_RAG_API_PORT       Published RAG API port (default 8081).
  LOCAL_RAG_BIND_ADDRESS   RAG bind address (default 0.0.0.0 for Lima access).
  LOCAL_SHOPPER_API_PORT   Published Shopper API port (default 8082).
  LOCAL_POSTGRES_PORT      Published PostgreSQL port (default 5432).
  LOCAL_WXO_AGENT_NAME     Imported local agent name.
  SKIP_LOCAL_WXO_IMPORT    Set to 1 to retain existing local WXO imports.
EOF
}

require_layout() {
  for path in "${RAG_ROOT}" "${SHOPPER_API_ROOT}" "${AGENT_ENV_FILE}" \
    "${AGENT_SECRETS_FILE}" "${COMPOSE_FILE}" "${RENDERER}"; do
    if [[ ! -e "${path}" ]]; then
      echo "Missing required local path: ${path}" >&2
      exit 1
    fi
  done
  for command_name in curl podman python3; do
    command -v "${command_name}" >/dev/null 2>&1 || {
      echo "Missing command: ${command_name}" >&2
      exit 1
    }
  done
}

require_podman() {
  if ! podman info >/dev/null 2>&1; then
    cat >&2 <<'EOF'
Podman is installed but its machine is not reachable. Start it with:
  podman machine start
EOF
    exit 1
  fi
}

require_local_wxo() {
  if ! curl -fsS -X POST \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    --data 'username=wxo.archer%40ibm.com&password=watsonx' \
    'http://127.0.0.1:4321/api/v1/auth/token' >/dev/null 2>&1; then
    cat >&2 <<EOF
Local WXO is not reachable. Start it first:
  source ${AGENT_ENV_FILE}
  orchestrate server start --env-file ${AGENT_ENV_FILE} \\
    --service-username "\${SERVICE_USER}" --service-password "\${SERVICE_PASSWORD}"
EOF
    exit 1
  fi
  "${ORCHESTRATE_BIN}" env activate local >/dev/null
}

resolve_local_agent_id() {
  "${ORCHESTRATE_BIN}" agents list -v | "${PYTHON_BIN}" -c '
import json, sys
name = sys.argv[1]
payload = json.load(sys.stdin)
for agent in payload.get("native", []):
    if agent.get("name") == name:
        print(agent["id"])
        raise SystemExit(0)
raise SystemExit(f"Local WXO agent not found: {name}")
' "${LOCAL_AGENT_NAME}"
}

configure_wxo() {
  require_local_wxo
  "${SCRIPT_DIR}/../deployments/deploy-agent.sh" --local
}

secret_file() {
  local preferred="$1"
  local fallback="$2"
  if [[ -f "${preferred}" ]]; then
    printf '%s\n' "${preferred}"
  elif [[ -f "${fallback}" ]]; then
    printf '%s\n' "${fallback}"
  else
    echo "Missing local secrets file: ${preferred} (fallback: ${fallback})" >&2
    exit 1
  fi
}

render_compose_override() {
  local rag_secrets
  local shopper_secrets
  local agent_id
  rag_secrets=$(secret_file \
    "${RAG_ROOT}/deployment/environments/local/runtime-secrets.env" \
    "${RAG_ROOT}/.env")
  shopper_secrets=$(secret_file \
    "${SHOPPER_API_ROOT}/deployment/environments/local/runtime-secrets.env" \
    "${SHOPPER_API_ROOT}/.env.local")
  agent_id=$(resolve_local_agent_id)
  "${PYTHON_BIN}" "${RENDERER}" \
    --platform-root "${PLATFORM_ROOT}" \
    --rag-secrets "${rag_secrets}" \
    --shopper-secrets "${shopper_secrets}" \
    --agent-secrets "${AGENT_SECRETS_FILE}" \
    --agent-id "${agent_id}" \
    --output "${COMPOSE_OVERRIDE}"
}

compose() {
  SHOPPER_PLATFORM_ROOT="${PLATFORM_ROOT}" \
    LOCAL_RAG_API_PORT="${RAG_PORT}" \
    LOCAL_SHOPPER_API_PORT="${SHOPPER_API_PORT}" \
    podman compose \
      --project-directory "${ROOT_DIR}" \
      --file "${COMPOSE_FILE}" \
      --file "${COMPOSE_OVERRIDE}" \
      "$@"
}

wait_for_url() {
  local name="$1"
  local url="$2"
  for _ in {1..120}; do
    if curl -fsS "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "${name} did not become ready at ${url}." >&2
  compose logs --tail 100 >&2 || true
  return 1
}

prepare_stack() {
  require_layout
  require_podman
  require_local_wxo
  mkdir -p "${STATE_DIR}"
  if [[ "${SKIP_LOCAL_WXO_IMPORT:-0}" != "1" ]]; then
    configure_wxo
  fi
  render_compose_override
}

start_stack() {
  prepare_stack
  compose up --build --detach
  wait_for_url "RAG API" "http://127.0.0.1:${RAG_PORT}/health"
  wait_for_url "Shopper Assistant API" "http://127.0.0.1:${SHOPPER_API_PORT}/readyz"
  show_status
}

show_status() {
  if curl -fsS 'http://127.0.0.1:4321/api/v1/auth/token' \
    -X POST --data 'username=wxo.archer%40ibm.com&password=watsonx' >/dev/null 2>&1; then
    echo "WXO: running at http://127.0.0.1:4321"
  else
    echo "WXO: not reachable"
  fi
  if [[ -f "${COMPOSE_OVERRIDE}" ]] && podman info >/dev/null 2>&1; then
    compose ps
  else
    echo "Podman Compose stack: not configured or Podman unavailable"
  fi
}

case "${ACTION}" in
  start)
    start_stack
    ;;
  run)
    start_stack
    trap 'compose down' EXIT INT TERM
    compose logs --follow
    ;;
  stop)
    require_layout
    require_podman
    if [[ -f "${COMPOSE_OVERRIDE}" ]]; then
      compose down
    else
      echo "Podman Compose stack has not been configured."
    fi
    echo "WXO was left running; use 'orchestrate server stop' when desired."
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    show_status
    ;;
  logs)
    require_layout
    require_podman
    [[ -f "${COMPOSE_OVERRIDE}" ]] || {
      echo "Run '$0 start' before requesting logs." >&2
      exit 1
    }
    compose logs --follow --tail 100
    ;;
  config)
    prepare_stack
    compose config --quiet
    echo "Podman Compose configuration is valid."
    ;;
  configure-wxo)
    require_layout
    configure_wxo
    ;;
  -h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
