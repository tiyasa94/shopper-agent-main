#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
ROOT_DIR=$( cd -- "${SCRIPT_DIR}/.." &> /dev/null && pwd )
PLATFORM_ROOT="${SHOPPER_PLATFORM_ROOT:-${ROOT_DIR}/../shopper-platform}"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
ORCHESTRATE_BIN="${ROOT_DIR}/.venv/bin/orchestrate"
LOCAL_PLATFORM="${ROOT_DIR}/scripts/local-platform.sh"
AGENT_RUNTIME_SECRETS="${ROOT_DIR}/deployments/local/runtime-secrets.env"
LOCAL_URL="http://127.0.0.1:8082"
AGENT_NAME="${LOCAL_WXO_AGENT_NAME:-Elevance_Health_Shopper_Portal}"
TURN_DELAY_SECONDS="${LOCAL_EVALUATION_TURN_DELAY_SECONDS:-0.5}"
TRIALS="${EXPERIMENT_TRIALS:-3}"
CONCURRENCY="${EXPERIMENT_CONCURRENCY:-3}"
PROFILE="${1:-critical}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "${PROFILE}" in
  critical|full|adversarial) ;;
  *)
    echo "Usage: $0 {critical|full|adversarial} [--candidate label,agent_rev,platform_rev[,rag_mode]] [--baseline label] [-- behavioral-run options]" >&2
    exit 2
    ;;
esac

declare -a CANDIDATES=()
BASELINE_LABEL="rc4"
declare -a RUN_OPTIONS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --candidate)
      [[ $# -ge 2 ]] || { echo "--candidate requires a value" >&2; exit 2; }
      CANDIDATES+=("$2")
      shift 2
      ;;
    --baseline)
      [[ $# -ge 2 ]] || { echo "--baseline requires a value" >&2; exit 2; }
      BASELINE_LABEL="$2"
      shift 2
      ;;
    --)
      shift
      RUN_OPTIONS+=("$@")
      break
      ;;
    *)
      RUN_OPTIONS+=("$1")
      shift
      ;;
  esac
done

if [[ ${#CANDIDATES[@]} -eq 0 ]]; then
  CANDIDATES=(
    "rc4,4b3291e70aaebe0b8f2a0f83a3dd0d107ca3d25e,2e1d73aaa083a1e1e2534ea62ce3acbfc0a627d4,protected"
    "agent-refactor-only,e1002633928d95aeaf91326283b8e8bf3ee5033e,2e1d73aaa083a1e1e2534ea62ce3acbfc0a627d4,protected"
    "rag-changes-only,4b3291e70aaebe0b8f2a0f83a3dd0d107ca3d25e,1bcf3d36854ee820a6f7723c33b69e389f0b7ee8,protected"
    "combined-snapshot,e1002633928d95aeaf91326283b8e8bf3ee5033e,1bcf3d36854ee820a6f7723c33b69e389f0b7ee8,protected"
  )
fi

for path in \
  "${PYTHON_BIN}" \
  "${ORCHESTRATE_BIN}" \
  "${LOCAL_PLATFORM}" \
  "${ROOT_DIR}/.env.local" \
  "${AGENT_RUNTIME_SECRETS}" \
  "${PLATFORM_ROOT}/apps/rag-api/.env" \
  "${PLATFORM_ROOT}/apps/shopper-assistant-api/.env.local"; do
  if [[ ! -e "${path}" ]]; then
    echo "Missing experiment input: ${path}" >&2
    exit 1
  fi
done

if [[ "${PROFILE}" == "adversarial" ]]; then
  CORPUS="${ROOT_DIR}/tests/evaluation/adversarial_questions.yaml"
else
  CORPUS="${ROOT_DIR}/tests/evaluation/behavioral_questions.yaml"
fi
ASSERTION_SPEC="${ROOT_DIR}/tests/evaluation/behavioral_assertions.yaml"

declare -a FOCUSED_CONVERSATIONS=(
  behavioral_ind_cost_terms
  behavioral_ind_er_casual_selection
  behavioral_ind_personal_premium
  behavioral_ind_correction
  behavioral_ind_comparison
  behavioral_ind_enrollment_education
  behavioral_ind_assistance_options
  behavioral_ind_unknown_plan
  behavioral_ind_personal_bill
  behavioral_medicare_hmo_pos
  behavioral_medicare_benefit_selection
  behavioral_medicare_same_plan
  behavioral_medicare_named_prime
  behavioral_medicare_comparison
  behavioral_medicare_unknown_plan
  behavioral_medicare_ambiguous_hmo
  behavioral_medicare_current_precedence
  behavioral_medicare_conflicting_network
  behavioral_medicare_optional_package
)

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
  local rag_mode="$2"
  SHOPPER_PLATFORM_ROOT="${platform_root}" \
    RETRIEVAL_PLAN_SUMMARY_MODE="${rag_mode}" \
    SKIP_LOCAL_WXO_IMPORT=1 \
    "${LOCAL_PLATFORM}" start
}

switch_candidate() {
  local agent_root="$1"
  local platform_root="$2"
  local rag_mode="$3"
  stop_platform
  import_agent_stack "${agent_root}"
  start_platform "${platform_root}" "${rag_mode}"
}

validate_cache() {
  local candidate_dir="$1"
  local revision="$2"
  local fingerprint="$3"
  "${PYTHON_BIN}" -m evaluation behavioral validate-cache \
    --candidate-dir "${candidate_dir}" \
    --source-revision "${revision}" \
    --source-fingerprint "${fingerprint}" \
    --rag-base-url "local-dev-milvus" \
    --suite full \
    --corpus "${CORPUS}" \
    --endpoint-url "${LOCAL_URL}" \
    --transport shopper-api \
    --turn-delay-seconds "${TURN_DELAY_SECONDS}" >/dev/null
}

materialize_candidate() {
  local label="$1"
  local agent_rev="$2"
  local platform_rev="$3"
  local destination="$4"
  local agent_root="${destination}/shopper-agent"
  local platform_root="${destination}/shopper-platform"
  mkdir -p "${destination}"
  git clone --quiet --shared --no-checkout "${ROOT_DIR}" "${agent_root}"
  git -C "${agent_root}" checkout --quiet --detach "${agent_rev}"
  git clone --quiet --shared --no-checkout "${PLATFORM_ROOT}" "${platform_root}"
  git -C "${platform_root}" checkout --quiet --detach "${platform_rev}"
  ln -s "${ROOT_DIR}/.venv" "${agent_root}/.venv"
  cp "${ROOT_DIR}/.env.local" "${agent_root}/.env.local"
  cp "${PLATFORM_ROOT}/apps/rag-api/.env" "${platform_root}/apps/rag-api/.env"
  cp "${PLATFORM_ROOT}/apps/shopper-assistant-api/.env.local" \
    "${platform_root}/apps/shopper-assistant-api/.env.local"
  printf '%s\t%s\n' "${agent_root}" "${platform_root}"
}

run_candidate() {
  local label="$1"
  local revision="$2"
  local fingerprint="$3"
  local agent_id="$4"
  local output="$5"
  local api_key="$6"
  local -a selection=()
  if [[ "${PROFILE}" == "critical" ]]; then
    for conversation in "${FOCUSED_CONVERSATIONS[@]}"; do
      selection+=(--conversation-id "${conversation}")
    done
  fi
  SHOPPER_API_KEY="${api_key}" "${PYTHON_BIN}" -m evaluation behavioral run \
    --transport shopper-api \
    --candidate "${label}" \
    --source-ref exact-experiment-commits \
    --source-revision "${revision}" \
    --source-fingerprint "${fingerprint}" \
    --rag-base-url local-dev-milvus \
    --corpus "${CORPUS}" \
    --suite full \
    --output "${output}" \
    --url "${LOCAL_URL}" \
    --agent-id "${agent_id}" \
    --timeout 330 \
    --turn-delay "${TURN_DELAY_SECONDS}" \
    --trials "${TRIALS}" \
    --concurrency "${CONCURRENCY}" \
    --warmup \
    ${selection[@]+"${selection[@]}"} \
    ${RUN_OPTIONS[@]+"${RUN_OPTIONS[@]}"}
}

TEMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/shopper-experiment-matrix.XXXXXX")
RESTORE_NEEDED=0
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ "${RESTORE_NEEDED}" == "1" ]]; then
    echo "Restoring the current local shopper stack..." >&2
    stop_platform || true
    import_agent_stack "${ROOT_DIR}" || true
    start_platform "${PLATFORM_ROOT}" "disabled" || true
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
  "${PLATFORM_ROOT}/apps/shopper-assistant-api/.env.local")
RUN_STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RUN_DIR="${EVALUATION_RUN_DIR:-${ROOT_DIR}/artifacts/evaluations/experiment-matrix-${PROFILE}-${RUN_STAMP}}"
CACHE_ROOT="${EXPERIMENT_CACHE_ROOT:-${ROOT_DIR}/artifacts/evaluations/experiment-matrix-cache}"
mkdir -p "${RUN_DIR}"

declare -a LABELS=()
declare -a OUTPUTS=()
BASELINE_FOUND=0

cat <<EOF
Local agent/platform experiment matrix
  profile:     ${PROFILE}
  trials:      ${TRIALS}
  concurrency: ${CONCURRENCY}
  corpus:      ${CORPUS_SHA}
  output:      ${RUN_DIR}

All candidates use local WXO, the local Shopper API, local RAG API, and read-only development Milvus.
EOF

for spec in "${CANDIDATES[@]}"; do
  IFS=',' read -r label agent_rev platform_rev rag_mode extra <<<"${spec}"
  if [[ -n "${extra:-}" || -z "${label}" || -z "${agent_rev}" || -z "${platform_rev}" ]]; then
    echo "Invalid candidate spec: ${spec}" >&2
    exit 2
  fi
  rag_mode="${rag_mode:-protected}"
  if [[ "${label}" == "${BASELINE_LABEL}" ]]; then
    BASELINE_FOUND=1
  fi
  if ! git -C "${ROOT_DIR}" cat-file -e "${agent_rev}^{commit}"; then
    echo "Unknown shopper-agent revision for ${label}: ${agent_rev}" >&2
    exit 1
  fi
  if ! git -C "${PLATFORM_ROOT}" cat-file -e "${platform_rev}^{commit}"; then
    echo "Unknown shopper-platform revision for ${label}: ${platform_rev}" >&2
    exit 1
  fi

  candidate_root="${TEMP_ROOT}/${label}"
  IFS=$'\t' read -r agent_root platform_root < <(
    materialize_candidate "${label}" "${agent_rev}" "${platform_rev}" "${candidate_root}"
  )
  revision="shopper-agent@${agent_rev}+shopper-platform@${platform_rev}+rag-mode@${rag_mode}"
  fingerprint=$(hash_values \
    "${revision}" "${CONFIG_IDENTITY}" "${WXO_VERSION}" "${CORPUS_SHA}" \
    "${PROFILE}" "${TRIALS}" "${CONCURRENCY}" "${TURN_DELAY_SECONDS}" \
    "${RUN_OPTIONS[*]-}")
  output="${RUN_DIR}/${label}"
  cache_key=$(hash_values "${fingerprint}")
  cache="${CACHE_ROOT}/${label}/${cache_key}"

  if [[ "${USE_EXPERIMENT_CACHE:-1}" == "1" && -d "${cache}" ]] && \
    validate_cache "${cache}" "${revision}" "${fingerprint}"; then
    echo "Reusing exact candidate cache: ${label}"
    mkdir -p "${output}"
    cp -R "${cache}/." "${output}/"
  else
    RESTORE_NEEDED=1
    echo "Running candidate: ${label}"
    switch_candidate "${agent_root}" "${platform_root}" "${rag_mode}"
    agent_id=$(resolve_agent_id)
    api_key=$(read_client_key "${platform_root}/apps/shopper-assistant-api/.env.local")
    run_candidate "${label}" "${revision}" "${fingerprint}" "${agent_id}" "${output}" "${api_key}"
    if validate_cache "${output}" "${revision}" "${fingerprint}"; then
      mkdir -p "${cache}"
      cp -R "${output}/." "${cache}/"
    fi
  fi

  if [[ "${PROFILE}" != "adversarial" ]]; then
    "${PYTHON_BIN}" -m evaluation assertions \
      --run "${output}" \
      --spec "${ASSERTION_SPEC}" \
      --output "${output}/assertions"
  fi
  LABELS+=("${label}")
  OUTPUTS+=("${output}")
done

if [[ "${BASELINE_FOUND}" != "1" ]]; then
  echo "Baseline label was not included in candidates: ${BASELINE_LABEL}" >&2
  exit 2
fi

baseline_output=""
for index in "${!LABELS[@]}"; do
  if [[ "${LABELS[${index}]}" == "${BASELINE_LABEL}" ]]; then
    baseline_output="${OUTPUTS[${index}]}"
    break
  fi
done

for index in "${!LABELS[@]}"; do
  label="${LABELS[${index}]}"
  if [[ "${label}" == "${BASELINE_LABEL}" ]]; then
    continue
  fi
  comparison="${RUN_DIR}/comparisons/${BASELINE_LABEL}-vs-${label}"
  "${PYTHON_BIN}" -m evaluation behavioral compare \
    --baseline "${baseline_output}" \
    --challenger "${OUTPUTS[${index}]}" \
    --output "${comparison}"
done

if [[ "${RUN_EXPERIMENT_JUDGE:-0}" == "1" ]]; then
  set -a
  source "${ROOT_DIR}/.env.local"
  set +a
  for index in "${!LABELS[@]}"; do
    "${PYTHON_BIN}" -m evaluation behavioral judge \
      --run "${OUTPUTS[${index}]}" --resume
  done
fi

echo "Restoring the current local shopper stack..."
stop_platform
import_agent_stack "${ROOT_DIR}"
start_platform "${PLATFORM_ROOT}" "disabled"
RESTORE_NEEDED=0
echo
echo "Experiment matrix complete: ${RUN_DIR}"
