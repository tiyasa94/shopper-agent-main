#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
ROOT_DIR=$( cd -- "${SCRIPT_DIR}/.." &> /dev/null && pwd )
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
PLATFORM_ROOT="${SHOPPER_PLATFORM_ROOT:-${ROOT_DIR}/../shopper-platform}"
SHOPPER_ROOT="${PLATFORM_ROOT}/apps/shopper-assistant-api"
STAGE_DIR="${SHOPPER_ROOT}/deployment/environments/stage"
LOCAL_DIR="${SHOPPER_ROOT}/deployment/environments/local"
STAGE_MANIFEST="${STAGE_DIR}/manifest.toml"
LOCAL_MANIFEST="${LOCAL_DIR}/manifest.toml"
STAGE_SECRETS="${STAGE_DIR}/runtime-secrets.env"
LOCAL_SECRETS="${LOCAL_DIR}/runtime-secrets.env"
if [[ ! -f "${LOCAL_SECRETS}" ]]; then
  LOCAL_SECRETS="${SHOPPER_ROOT}/.env.local"
fi
PROFILE="${1:-pilot}"
if [[ $# -gt 0 ]]; then
  shift
fi

if [[ "${PROFILE}" != "pilot" && "${PROFILE}" != "full" ]]; then
  echo "Usage: $0 {pilot|full} [candidate-run options]" >&2
  exit 2
fi
for path in \
  "${PYTHON_BIN}" \
  "${STAGE_MANIFEST}" \
  "${LOCAL_MANIFEST}" \
  "${STAGE_SECRETS}" \
  "${LOCAL_SECRETS}"; do
  if [[ ! -e "${path}" ]]; then
    echo "Missing required comparison input: ${path}" >&2
    exit 1
  fi
done

read_manifest_values() {
  "${PYTHON_BIN}" -c '
import sys, tomllib
data = tomllib.loads(open(sys.argv[1], encoding="utf-8").read())
app = data["application"]
config = data["config_map"]
print(
    app["public_url"],
    config["WXO_AGENT_ID"],
    config.get("WXO_AGENT_ENVIRONMENT_ID", ""),
    sep="\t",
)
' "$1"
}

source_tree_fingerprint() {
  "${PYTHON_BIN}" -c '
import hashlib, pathlib, subprocess, sys
repo = pathlib.Path(sys.argv[1]).resolve()
paths = sys.argv[2:]
files = subprocess.check_output(
    ["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *paths]
).split(b"\0")
digest = hashlib.sha256()
for raw in sorted(item for item in files if item):
    relative = raw.decode()
    path = repo / relative
    if not path.is_file():
        continue
    digest.update(relative.encode())
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

service_revision() {
  local endpoint="$1"
  local response_file
  response_file=$(mktemp)
  if ! curl -fsS --max-time 30 "${endpoint}/" -o "${response_file}"; then
    rm -f -- "${response_file}"
    return 1
  fi
  "${PYTHON_BIN}" -c '
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
if data.get("service") != "Shopper Assistant API":
    raise SystemExit("endpoint is not Shopper Assistant API")
print(data.get("version") or "unknown")
' "${response_file}"
  rm -f -- "${response_file}"
}

IFS=$'\t' read -r STAGE_URL STAGE_AGENT_ID STAGE_AGENT_ENVIRONMENT_ID < <(read_manifest_values "${STAGE_MANIFEST}")
IFS=$'\t' read -r _LOCAL_MANIFEST_URL LOCAL_AGENT_ID _LOCAL_AGENT_ENVIRONMENT_ID < <(read_manifest_values "${LOCAL_MANIFEST}")
LOCAL_URL="${LOCAL_SHOPPER_API_URL:-http://127.0.0.1:8082}"
STAGE_API_KEY=$(read_client_key "${STAGE_SECRETS}")
LOCAL_API_KEY=$(read_client_key "${LOCAL_SECRETS}")
STAGE_REVISION=$(service_revision "${STAGE_URL}")
LOCAL_REVISION=$(service_revision "${LOCAL_URL}")
STAGE_IDENTITY="${STAGE_REVISION}:${STAGE_AGENT_ID}:${STAGE_AGENT_ENVIRONMENT_ID}"

RUN_STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RUN_DIR="${EVALUATION_RUN_DIR:-${ROOT_DIR}/artifacts/evaluations/stage-vs-local-${PROFILE}-${RUN_STAMP}}"
STAGE_DIR_OUT="${RUN_DIR}/previous-stage"
LOCAL_DIR_OUT="${RUN_DIR}/current-local"
COMPARISON_DIR="${RUN_DIR}/comparison"
SHOPPER_API_CACHE_ROOT="${SHOPPER_API_CACHE_ROOT:-${BASELINE_CACHE_ROOT:-${ROOT_DIR}/artifacts/evaluations/shopper-api-baseline-cache}}"
CORPUS="${ROOT_DIR}/tests/evaluation/behavioral_questions.yaml"
CORPUS_SHA=$(shasum -a 256 "${CORPUS}" | awk '{print $1}')
CACHE_KEY=$(
  printf '%s\n' "${STAGE_IDENTITY}" "${STAGE_URL}" "${PROFILE}" "${CORPUS_SHA}" "$*" \
    | shasum -a 256 | awk '{print $1}'
)
STAGE_CACHE_DIR="${SHOPPER_API_CACHE_ROOT}/${CACHE_KEY}"
LOCAL_GIT_REVISION=$(git -C "${ROOT_DIR}" rev-parse HEAD)
LOCAL_AGENT_FINGERPRINT=$(source_tree_fingerprint "${ROOT_DIR}" src)
LOCAL_SHOPPER_API_FINGERPRINT=$(
  source_tree_fingerprint "${PLATFORM_ROOT}" apps/shopper-assistant-api
)
LOCAL_RAG_API_FINGERPRINT=$(source_tree_fingerprint "${PLATFORM_ROOT}" apps/rag-api)
LOCAL_FINGERPRINT=$(
  printf '%s\n' \
    "${LOCAL_AGENT_FINGERPRINT}" \
    "${LOCAL_SHOPPER_API_FINGERPRINT}" \
    "${LOCAL_RAG_API_FINGERPRINT}" \
    | shasum -a 256 | awk '{print $1}'
)
LOCAL_CACHE_KEY=$(
  printf '%s\n' \
    "${LOCAL_GIT_REVISION}" \
    "${LOCAL_FINGERPRINT}" \
    "${LOCAL_REVISION}" \
    "${LOCAL_AGENT_ID}" \
    "${LOCAL_URL}" \
    "${PROFILE}" \
    "${CORPUS_SHA}" \
    "$*" \
    | shasum -a 256 | awk '{print $1}'
)
LOCAL_CACHE_DIR="${SHOPPER_API_CACHE_ROOT}/local/${LOCAL_CACHE_KEY}"

mkdir -p "${RUN_DIR}"

validate_stage_cache() {
  "${PYTHON_BIN}" -m evaluation behavioral validate-cache \
    --candidate-dir "$1" \
    --source-revision "${STAGE_IDENTITY}" \
    --rag-base-url "stage-managed" \
    --suite "${PROFILE}" \
    --endpoint-url "${STAGE_URL}" \
    --transport shopper-api \
    --agent-id "${STAGE_AGENT_ID}" >/dev/null
}

validate_local_cache() {
  "${PYTHON_BIN}" -m evaluation behavioral validate-cache \
    --candidate-dir "$1" \
    --source-revision "${LOCAL_GIT_REVISION}" \
    --source-fingerprint "${LOCAL_FINGERPRINT}" \
    --rag-base-url "http://127.0.0.1:8081" \
    --suite "${PROFILE}" \
    --endpoint-url "${LOCAL_URL}" \
    --transport shopper-api \
    --agent-id "${LOCAL_AGENT_ID}" >/dev/null
}

cat <<EOF
Public API behavioral comparison
  previous: ${STAGE_URL}
    service revision: ${STAGE_REVISION}
    agent ID: ${STAGE_AGENT_ID}
  current: ${LOCAL_URL}
    service revision: ${LOCAL_REVISION}
    source: ${LOCAL_GIT_REVISION} (${LOCAL_FINGERPRINT})
    agent ID: ${LOCAL_AGENT_ID}
  suite: ${PROFILE}
  output: ${RUN_DIR}

No WXO assets or cloud applications will be imported, updated, or deployed.
EOF

if [[ "${USE_STAGE_CACHE:-1}" == "1" && -d "${STAGE_CACHE_DIR}" ]] && validate_stage_cache "${STAGE_CACHE_DIR}"; then
  echo "Reusing validated stage baseline: ${STAGE_CACHE_DIR}"
  mkdir -p "${STAGE_DIR_OUT}"
  cp -R "${STAGE_CACHE_DIR}/." "${STAGE_DIR_OUT}/"
else
  SHOPPER_API_KEY="${STAGE_API_KEY}" "${PYTHON_BIN}" \
    -m evaluation behavioral run \
    --transport shopper-api \
    --allow-remote \
    --candidate previous-stage \
    --source-ref deployed-stage \
    --source-revision "${STAGE_IDENTITY}" \
    --source-fingerprint "deployed-service" \
    --rag-base-url stage-managed \
    --suite "${PROFILE}" \
    --output "${STAGE_DIR_OUT}" \
    --url "${STAGE_URL}" \
    --agent-id "${STAGE_AGENT_ID}" \
    --timeout 330 \
    "$@"
  if validate_stage_cache "${STAGE_DIR_OUT}"; then
    mkdir -p "${STAGE_CACHE_DIR}"
    cp -R "${STAGE_DIR_OUT}/." "${STAGE_CACHE_DIR}/"
    echo "Cached complete stage baseline: ${STAGE_CACHE_DIR}"
  fi
fi

if [[ "${USE_LOCAL_CACHE:-1}" == "1" && -d "${LOCAL_CACHE_DIR}" ]] && validate_local_cache "${LOCAL_CACHE_DIR}"; then
  echo "Reusing validated local candidate: ${LOCAL_CACHE_DIR}"
  mkdir -p "${LOCAL_DIR_OUT}"
  cp -R "${LOCAL_CACHE_DIR}/." "${LOCAL_DIR_OUT}/"
else
  SHOPPER_API_KEY="${LOCAL_API_KEY}" "${PYTHON_BIN}" \
    -m evaluation behavioral run \
    --transport shopper-api \
    --candidate current-local \
    --source-ref working-tree \
    --source-revision "${LOCAL_GIT_REVISION}" \
    --source-fingerprint "${LOCAL_FINGERPRINT}" \
    --rag-base-url "http://127.0.0.1:8081" \
    --suite "${PROFILE}" \
    --output "${LOCAL_DIR_OUT}" \
    --url "${LOCAL_URL}" \
    --agent-id "${LOCAL_AGENT_ID}" \
    --timeout 330 \
    "$@"
  if validate_local_cache "${LOCAL_DIR_OUT}"; then
    mkdir -p "${LOCAL_CACHE_DIR}"
    cp -R "${LOCAL_DIR_OUT}/." "${LOCAL_CACHE_DIR}/"
    echo "Cached complete local candidate: ${LOCAL_CACHE_DIR}"
  fi
fi

"${PYTHON_BIN}" -m evaluation behavioral compare \
  --baseline "${STAGE_DIR_OUT}" \
  --challenger "${LOCAL_DIR_OUT}" \
  --output "${COMPARISON_DIR}"

echo
echo "Comparison complete."
echo "  Blinded review: ${COMPARISON_DIR}/review-blinded.csv"
echo "  Summary: ${COMPARISON_DIR}/summary.md"
echo "  Candidate key: ${COMPARISON_DIR}/candidate-key.json"
