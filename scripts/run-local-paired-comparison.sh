#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
ROOT_DIR=$( cd -- "${SCRIPT_DIR}/.." &> /dev/null && pwd )
PLATFORM_ROOT="${SHOPPER_PLATFORM_ROOT:-${ROOT_DIR}/../shopper-platform}"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
ORCHESTRATE_BIN="${ROOT_DIR}/.venv/bin/orchestrate"
LOCAL_PLATFORM="${ROOT_DIR}/scripts/local-platform.sh"
AGENT_RUNTIME_SECRETS="${ROOT_DIR}/deployments/local/runtime-secrets.env"
CORPUS="${ROOT_DIR}/tests/evaluation/behavioral_questions.yaml"
LOCAL_URL="http://127.0.0.1:8082"
AGENT_NAME="${LOCAL_WXO_AGENT_NAME:-Elevance_Health_Shopper_Portal}"
TURN_DELAY_SECONDS="${LOCAL_EVALUATION_TURN_DELAY_SECONDS:-0.5}"
BASELINE_AGENT_REV="${BASELINE_AGENT_REV:-71b57ad98cd8ba59ac07c40db7256ad25faf16a6}"
BASELINE_PLATFORM_REV="${BASELINE_PLATFORM_REV:-1e60d8a553f3a508c74563b86ba4b5db72d8e624}"
PROFILE="${1:-full}"
if [[ $# -gt 0 ]]; then
  shift
fi

if [[ "${PROFILE}" != "pilot" && "${PROFILE}" != "full" ]]; then
  echo "Usage: $0 {pilot|full} [candidate-run options]" >&2
  exit 2
fi

for path in \
  "${PYTHON_BIN}" \
  "${ORCHESTRATE_BIN}" \
  "${LOCAL_PLATFORM}" \
  "${CORPUS}" \
  "${ROOT_DIR}/.env.local" \
  "${AGENT_RUNTIME_SECRETS}" \
  "${PLATFORM_ROOT}/apps/rag-api/.env" \
  "${PLATFORM_ROOT}/apps/shopper-assistant-api/.env.local"; do
  if [[ ! -e "${path}" ]]; then
    echo "Missing paired-comparison input: ${path}" >&2
    exit 1
  fi
done

if ! git -C "${ROOT_DIR}" cat-file -e "${BASELINE_AGENT_REV}^{commit}"; then
  echo "Unknown shopper-agent baseline commit: ${BASELINE_AGENT_REV}" >&2
  exit 1
fi
if ! git -C "${PLATFORM_ROOT}" cat-file -e "${BASELINE_PLATFORM_REV}^{commit}"; then
  echo "Unknown shopper-platform baseline commit: ${BASELINE_PLATFORM_REV}" >&2
  exit 1
fi

source_tree_fingerprint() {
  "${PYTHON_BIN}" -c '
import hashlib, pathlib, subprocess, sys
repo = pathlib.Path(sys.argv[1]).resolve()
files = subprocess.check_output(
    ["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *sys.argv[2:]]
).split(b"\0")
digest = hashlib.sha256()
for raw in sorted(item for item in files if item):
    relative = raw.decode()
    path = repo / relative
    if path.is_file():
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
print(digest.hexdigest())
' "$@"
}

hash_values() {
  "${PYTHON_BIN}" -c '
import hashlib, sys
digest = hashlib.sha256()
for value in sys.argv[1:]:
    digest.update(value.encode())
    digest.update(b"\0")
print(digest.hexdigest())
' "$@"
}

hash_files() {
  "${PYTHON_BIN}" -c '
import hashlib, pathlib, sys
digest = hashlib.sha256()
for raw in sys.argv[1:]:
    path = pathlib.Path(raw)
    digest.update(path.name.encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
print(digest.hexdigest())
' "$@"
}

read_client_key() {
  "${PYTHON_BIN}" -c '
import json, sys
for raw in open(sys.argv[1], encoding="utf-8"):
    if raw.startswith("CLIENT_API_KEYS_JSON="):
        values = json.loads(raw.split("=", 1)[1])
        if isinstance(values, dict) and values:
            print(next(iter(values.values())))
            raise SystemExit(0)
raise SystemExit("CLIENT_API_KEYS_JSON is missing or empty")
' "$1"
}

resolve_agent_id() {
  "${ORCHESTRATE_BIN}" agents list -v | "${PYTHON_BIN}" -c '
import json, sys
name = sys.argv[1]
payload = json.load(sys.stdin)
for agent in payload.get("native", []):
    if agent.get("name") == name:
        print(agent["id"])
        raise SystemExit(0)
raise SystemExit(f"Local WXO agent not found: {name}")
' "${AGENT_NAME}"
}

import_agent_stack() {
  local agent_root="$1"
  if [[ -x "${agent_root}/deployments/deploy-agent.sh" ]]; then
    if [[ "${agent_root}" == "${ROOT_DIR}" ]]; then
      DEPLOY_RUNTIME_SECRETS_FILE="${AGENT_RUNTIME_SECRETS}" \
        "${agent_root}/deployments/deploy-agent.sh" --local
    else
      mkdir -p "${agent_root}/deployments/local"
      cp "${AGENT_RUNTIME_SECRETS}" "${agent_root}/deployments/local/runtime-secrets.env"
      "${agent_root}/deployments/deploy-agent.sh" --local
    fi
  elif [[ -x "${agent_root}/scripts/import-all.sh" ]]; then
    PATH="${ROOT_DIR}/.venv/bin:${PATH}" \
      RAG_API_BASE_URL_OVERRIDE="http://host.lima.internal:8081" \
      WXO_TARGET_ENV=local \
      "${agent_root}/scripts/import-all.sh"
  else
    echo "No supported local deployment entry point in ${agent_root}" >&2
    return 1
  fi
}

stop_platform() {
  SHOPPER_PLATFORM_ROOT="${PLATFORM_ROOT}" "${LOCAL_PLATFORM}" stop
}

start_platform() {
  local platform_root="$1"
  SHOPPER_PLATFORM_ROOT="${platform_root}" \
    SKIP_LOCAL_WXO_IMPORT=1 \
    "${LOCAL_PLATFORM}" start
}

switch_candidate() {
  local agent_root="$1"
  local platform_root="$2"
  stop_platform
  import_agent_stack "${agent_root}"
  start_platform "${platform_root}"
}

validate_cache() {
  local candidate_dir="$1"
  local revision="$2"
  local fingerprint="$3"
  local agent_id="$4"
  "${PYTHON_BIN}" -m evaluation behavioral validate-cache \
    --candidate-dir "${candidate_dir}" \
    --source-revision "${revision}" \
    --source-fingerprint "${fingerprint}" \
    --rag-base-url "local-dev-milvus" \
    --suite "${PROFILE}" \
    --endpoint-url "${LOCAL_URL}" \
    --transport shopper-api \
    --agent-id "${agent_id}" \
    --turn-delay-seconds "${TURN_DELAY_SECONDS}" >/dev/null
}

validate_baseline_source() {
  local candidate_dir="$1"
  local revision="$2"
  local agent_id="$3"
  "${PYTHON_BIN}" -m evaluation behavioral validate-cache \
    --candidate-dir "${candidate_dir}" \
    --source-revision "${revision}" \
    --rag-base-url "local-dev-milvus" \
    --suite "${PROFILE}" \
    --endpoint-url "${LOCAL_URL}" \
    --transport shopper-api \
    --agent-id "${agent_id}" >/dev/null
}

run_candidate() {
  local label="$1"
  local source_ref="$2"
  local revision="$3"
  local fingerprint="$4"
  local agent_id="$5"
  local output="$6"
  local api_key="$7"
  shift 7
  SHOPPER_API_KEY="${api_key}" "${PYTHON_BIN}" \
    -m evaluation behavioral run \
    --transport shopper-api \
    --candidate "${label}" \
    --source-ref "${source_ref}" \
    --source-revision "${revision}" \
    --source-fingerprint "${fingerprint}" \
    --rag-base-url "local-dev-milvus" \
    --suite "${PROFILE}" \
    --output "${output}" \
    --url "${LOCAL_URL}" \
    --agent-id "${agent_id}" \
    --timeout 330 \
    --turn-delay "${TURN_DELAY_SECONDS}" \
    --warmup \
    "$@"
}

TEMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/shopper-paired-eval.XXXXXX")
BASELINE_AGENT_ROOT="${TEMP_ROOT}/shopper-agent"
BASELINE_PLATFORM_ROOT="${TEMP_ROOT}/shopper-platform"
git clone --quiet --no-hardlinks --no-checkout "${ROOT_DIR}" "${BASELINE_AGENT_ROOT}"
git -C "${BASELINE_AGENT_ROOT}" checkout --quiet --detach "${BASELINE_AGENT_REV}"
git clone --quiet --no-hardlinks --no-checkout "${PLATFORM_ROOT}" "${BASELINE_PLATFORM_ROOT}"
git -C "${BASELINE_PLATFORM_ROOT}" checkout --quiet --detach "${BASELINE_PLATFORM_REV}"
ln -s "${ROOT_DIR}/.venv" "${BASELINE_AGENT_ROOT}/.venv"
cp "${ROOT_DIR}/.env.local" "${BASELINE_AGENT_ROOT}/.env.local"
cp "${PLATFORM_ROOT}/apps/rag-api/.env" \
  "${BASELINE_PLATFORM_ROOT}/apps/rag-api/.env"
cp "${PLATFORM_ROOT}/apps/shopper-assistant-api/.env.local" \
  "${BASELINE_PLATFORM_ROOT}/apps/shopper-assistant-api/.env.local"

RESTORE_NEEDED=0
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ "${RESTORE_NEEDED}" == "1" ]]; then
    echo "Restoring the current local shopper stack..." >&2
    stop_platform || true
    import_agent_stack "${ROOT_DIR}" || true
    start_platform "${PLATFORM_ROOT}" || true
  fi
  rm -rf -- "${TEMP_ROOT}"
  exit "${status}"
}
trap cleanup EXIT INT TERM

CORPUS_SHA=$(shasum -a 256 "${CORPUS}" | awk '{print $1}')
WXO_VERSION=$("${ORCHESTRATE_BIN}" --version)
CONFIG_IDENTITY=$(hash_files \
  "${ROOT_DIR}/.env.local" \
  "${AGENT_RUNTIME_SECRETS}" \
  "${PLATFORM_ROOT}/apps/rag-api/.env" \
  "${PLATFORM_ROOT}/apps/shopper-assistant-api/.env.local" \
  "${PLATFORM_ROOT}/apps/rag-api/deployment/environments/local/manifest.toml" \
  "${PLATFORM_ROOT}/apps/shopper-assistant-api/deployment/environments/local/manifest.toml")
HARNESS_FINGERPRINT=$(source_tree_fingerprint \
  "${ROOT_DIR}" \
  evaluation \
  tests/evaluation/behavioral_questions.yaml \
  scripts/run-local-paired-comparison.sh \
  scripts/local-platform.sh \
  scripts/render-local-compose.py \
  compose.local.yaml)
BASELINE_REVISION="shopper-agent@${BASELINE_AGENT_REV}+shopper-platform@${BASELINE_PLATFORM_REV}"
CHALLENGER_AGENT_REV=$(git -C "${ROOT_DIR}" rev-parse HEAD)
CHALLENGER_PLATFORM_REV=$(git -C "${PLATFORM_ROOT}" rev-parse HEAD)
CHALLENGER_REVISION="shopper-agent@${CHALLENGER_AGENT_REV}+shopper-platform@${CHALLENGER_PLATFORM_REV}"
BASELINE_FINGERPRINT=$(hash_values \
  "${BASELINE_REVISION}" "${HARNESS_FINGERPRINT}" "${CONFIG_IDENTITY}" "${WXO_VERSION}")
CHALLENGER_FINGERPRINT=$(hash_values \
  "$(source_tree_fingerprint "${ROOT_DIR}" src)" \
  "$(source_tree_fingerprint "${PLATFORM_ROOT}" apps/rag-api apps/shopper-assistant-api)" \
  "${HARNESS_FINGERPRINT}" "${CONFIG_IDENTITY}" "${WXO_VERSION}")
CURRENT_AGENT_ID=$(resolve_agent_id)

RUN_STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RUN_DIR="${EVALUATION_RUN_DIR:-${ROOT_DIR}/artifacts/evaluations/local-paired-${PROFILE}-${RUN_STAMP}}"
BASELINE_OUT="${RUN_DIR}/prior-snapshot"
CHALLENGER_OUT="${RUN_DIR}/structured-rag"
COMPARISON_OUT="${RUN_DIR}/comparison"
CACHE_ROOT="${PAIRED_CACHE_ROOT:-${ROOT_DIR}/artifacts/evaluations/local-paired-cache}"
BASELINE_CACHE_KEY=$(hash_values \
  "${BASELINE_FINGERPRINT}" "${CORPUS_SHA}" "${PROFILE}" "${LOCAL_URL}" \
  "${TURN_DELAY_SECONDS}" "$*")
CHALLENGER_CACHE_KEY=$(hash_values \
  "${CHALLENGER_FINGERPRINT}" "${CORPUS_SHA}" "${PROFILE}" "${LOCAL_URL}" \
  "${TURN_DELAY_SECONDS}" "$*")
BASELINE_CACHE="${CACHE_ROOT}/baseline/${BASELINE_CACHE_KEY}"
CHALLENGER_CACHE="${CACHE_ROOT}/challenger/${CHALLENGER_CACHE_KEY}"
BASELINE_SOURCE_DIR="${PAIRED_BASELINE_SOURCE_DIR:-}"
mkdir -p "${RUN_DIR}"

cat <<EOF
Exact local paired comparison
  baseline:   ${BASELINE_REVISION}
  challenger: ${CHALLENGER_REVISION} + working-tree fingerprint
  corpus:     ${PROFILE} (${CORPUS_SHA})
  transport:  ${LOCAL_URL} -> local WXO -> local RAG API -> dev Milvus
  turn delay: ${TURN_DELAY_SECONDS}s
  output:     ${RUN_DIR}

This script refuses remote transports and does not deploy cloud assets.
EOF

if [[ -n "${BASELINE_SOURCE_DIR}" ]]; then
  if [[ ! -d "${BASELINE_SOURCE_DIR}" ]] || ! validate_baseline_source \
    "${BASELINE_SOURCE_DIR}" "${BASELINE_REVISION}" "${CURRENT_AGENT_ID}"; then
    echo "Explicit baseline source failed identity or completeness validation: ${BASELINE_SOURCE_DIR}" >&2
    exit 1
  fi
  echo "Reusing explicitly validated baseline: ${BASELINE_SOURCE_DIR}"
  mkdir -p "${BASELINE_OUT}"
  cp -R "${BASELINE_SOURCE_DIR}/." "${BASELINE_OUT}/"
elif [[ "${USE_PAIRED_CACHE:-1}" == "1" && -d "${BASELINE_CACHE}" ]] && \
  validate_cache "${BASELINE_CACHE}" "${BASELINE_REVISION}" \
    "${BASELINE_FINGERPRINT}" "${CURRENT_AGENT_ID}"; then
  echo "Reusing exact baseline cache: ${BASELINE_CACHE}"
  mkdir -p "${BASELINE_OUT}"
  cp -R "${BASELINE_CACHE}/." "${BASELINE_OUT}/"
else
  RESTORE_NEEDED=1
  switch_candidate "${BASELINE_AGENT_ROOT}" "${BASELINE_PLATFORM_ROOT}"
  BASELINE_AGENT_ID=$(resolve_agent_id)
  BASELINE_API_KEY=$(read_client_key \
    "${BASELINE_PLATFORM_ROOT}/apps/shopper-assistant-api/.env.local")
  run_candidate prior-snapshot exact-commits "${BASELINE_REVISION}" \
    "${BASELINE_FINGERPRINT}" "${BASELINE_AGENT_ID}" "${BASELINE_OUT}" \
    "${BASELINE_API_KEY}" "$@"
  if validate_cache "${BASELINE_OUT}" "${BASELINE_REVISION}" \
    "${BASELINE_FINGERPRINT}" "${BASELINE_AGENT_ID}"; then
    mkdir -p "${BASELINE_CACHE}"
    cp -R "${BASELINE_OUT}/." "${BASELINE_CACHE}/"
  fi
  CURRENT_AGENT_ID="${BASELINE_AGENT_ID}"
fi

if [[ "${USE_PAIRED_CACHE:-1}" == "1" && -d "${CHALLENGER_CACHE}" ]] && \
  validate_cache "${CHALLENGER_CACHE}" "${CHALLENGER_REVISION}" \
    "${CHALLENGER_FINGERPRINT}" "${CURRENT_AGENT_ID}"; then
  echo "Reusing exact challenger cache: ${CHALLENGER_CACHE}"
  mkdir -p "${CHALLENGER_OUT}"
  cp -R "${CHALLENGER_CACHE}/." "${CHALLENGER_OUT}/"
  if [[ "${RESTORE_NEEDED}" == "1" ]]; then
    switch_candidate "${ROOT_DIR}" "${PLATFORM_ROOT}"
  fi
else
  RESTORE_NEEDED=1
  switch_candidate "${ROOT_DIR}" "${PLATFORM_ROOT}"
  CHALLENGER_AGENT_ID=$(resolve_agent_id)
  CHALLENGER_API_KEY=$(read_client_key \
    "${PLATFORM_ROOT}/apps/shopper-assistant-api/.env.local")
  run_candidate structured-rag working-tree "${CHALLENGER_REVISION}" \
    "${CHALLENGER_FINGERPRINT}" "${CHALLENGER_AGENT_ID}" "${CHALLENGER_OUT}" \
    "${CHALLENGER_API_KEY}" "$@"
  if validate_cache "${CHALLENGER_OUT}" "${CHALLENGER_REVISION}" \
    "${CHALLENGER_FINGERPRINT}" "${CHALLENGER_AGENT_ID}"; then
    mkdir -p "${CHALLENGER_CACHE}"
    cp -R "${CHALLENGER_OUT}/." "${CHALLENGER_CACHE}/"
  fi
  CURRENT_AGENT_ID="${CHALLENGER_AGENT_ID}"
fi

RESTORE_NEEDED=0
"${PYTHON_BIN}" -m evaluation behavioral compare \
  --baseline "${BASELINE_OUT}" \
  --challenger "${CHALLENGER_OUT}" \
  --output "${COMPARISON_OUT}"

echo
echo "Paired comparison complete."
echo "  Blinded review: ${COMPARISON_OUT}/review-blinded.csv"
echo "  Summary: ${COMPARISON_OUT}/summary.md"
echo "  Candidate key: ${COMPARISON_OUT}/candidate-key.json"
