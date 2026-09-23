#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
ROOT_DIR=$( cd -- "${SCRIPT_DIR}/.." &> /dev/null && pwd )
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
STAGE_URL="${SHOPPER_STAGE_API_URL:-https://elv-d2c-shopper-assistant-api-stage.2a6km91zabtc.us-south.codeengine.appdomain.cloud}"
STAGE_AGENT_ID="${SHOPPER_STAGE_AGENT_ID:-65b0ad24-4e50-471e-979f-e10b60055ff9}"
PROFILE="pilot"
OUTPUT_DIR=""
RESUME=0
TURN_DELAY_SECONDS="0.5"

usage() {
  cat <<'EOF'
Usage: ./scripts/run-stage-client-test.sh [--full] [--output DIR | --resume DIR]

Runs the client behavioral test through the deployed stage Shopper Assistant API.

Options:
  --full        Run every eligible conversation in the current curated corpus;
                default is the balanced 12-conversation pilot.
  --output DIR  Write a new run to DIR instead of the timestamped default.
  --resume DIR  Resume an interrupted run in DIR.
  -h, --help    Show this help.

The command securely prompts for SHOPPER_STAGE_API_KEY when it is not already set.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full)
      PROFILE="full"
      shift
      ;;
    --output)
      if [[ $# -lt 2 || -z "$2" ]]; then
        echo "Error: --output requires a directory." >&2
        exit 2
      fi
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --resume)
      if [[ $# -lt 2 || -z "$2" ]]; then
        echo "Error: --resume requires a directory." >&2
        exit 2
      fi
      OUTPUT_DIR="$2"
      RESUME=1
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Error: unknown option $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Error: ${PYTHON_BIN} is missing. Run 'uv sync --frozen' from ${ROOT_DIR}." >&2
  exit 1
fi

if [[ -z "${OUTPUT_DIR}" ]]; then
  RUN_STAMP=$(date -u +%Y%m%dT%H%M%SZ)
  OUTPUT_DIR="${ROOT_DIR}/artifacts/stage-client-tests/${PROFILE}-${RUN_STAMP}"
elif [[ "${OUTPUT_DIR}" != /* ]]; then
  OUTPUT_DIR="${ROOT_DIR}/${OUTPUT_DIR}"
fi

STAGE_API_KEY="${SHOPPER_STAGE_API_KEY:-}"
if [[ -z "${STAGE_API_KEY}" ]]; then
  if [[ ! -t 0 ]]; then
    echo "Error: set SHOPPER_STAGE_API_KEY when running without an interactive terminal." >&2
    exit 1
  fi
  read -r -s -p "Stage Shopper Assistant API key: " STAGE_API_KEY
  printf '\n'
fi
if [[ -z "${STAGE_API_KEY}" ]]; then
  echo "Error: the stage API key cannot be empty." >&2
  exit 1
fi

SERVICE_INFO=$("${PYTHON_BIN}" -c '
import sys
import requests

url = sys.argv[1].rstrip("/")
response = requests.get(f"{url}/", timeout=30)
response.raise_for_status()
payload = response.json()
if payload.get("service") != "Shopper Assistant API":
    raise SystemExit("Target is not the Shopper Assistant API")
if payload.get("environment") != "stage":
    raise SystemExit("Target did not identify itself as the stage environment")
print(payload.get("version") or "unknown", payload["environment"], sep="\t")
' "${STAGE_URL}")
IFS=$'\t' read -r SERVICE_VERSION SERVICE_ENVIRONMENT <<< "${SERVICE_INFO}"
SOURCE_REVISION="stage:${SERVICE_VERSION}:${STAGE_AGENT_ID}"
RUN_OPTIONS=()
if [[ "${RESUME}" == "1" ]]; then
  RUN_OPTIONS+=(--resume)
fi

cat <<EOF
Stage client behavioral test
  target: ${STAGE_URL}
  environment: ${SERVICE_ENVIRONMENT}
  service version: ${SERVICE_VERSION}
  suite: ${PROFILE}
  output: ${OUTPUT_DIR}

This sends version-controlled synthetic test conversations to the live stage service.
It creates stage API sessions and WXO runs, but does not deploy or modify cloud assets.
EOF

cd "${ROOT_DIR}"
SHOPPER_API_KEY="${STAGE_API_KEY}" "${PYTHON_BIN}" \
  -m evaluation behavioral run \
  --transport shopper-api \
  --allow-remote \
  --candidate deployed-stage \
  --source-ref deployed-stage \
  --source-revision "${SOURCE_REVISION}" \
  --source-fingerprint "deployed-service" \
  --rag-base-url stage-managed \
  --suite "${PROFILE}" \
  --output "${OUTPUT_DIR}" \
  --url "${STAGE_URL}" \
  --agent-id "${STAGE_AGENT_ID}" \
  --timeout 330 \
  --concurrency 1 \
  --turn-delay "${TURN_DELAY_SECONDS}" \
  ${RUN_OPTIONS[@]+"${RUN_OPTIONS[@]}"}

"${PYTHON_BIN}" -m evaluation review \
  --results "${OUTPUT_DIR}/results.csv" \
  --output "${OUTPUT_DIR}/client-review.csv"
cp "${ROOT_DIR}/docs/stage-client-test.md" "${OUTPUT_DIR}/README.md"
cp "${ROOT_DIR}/tests/evaluation/QUALITATIVE_GRADING_RUBRIC.md" \
  "${OUTPUT_DIR}/QUALITATIVE_GRADING_RUBRIC.md"

if ! "${PYTHON_BIN}" -m evaluation behavioral validate-cache \
  --candidate-dir "${OUTPUT_DIR}" \
  --source-revision "${SOURCE_REVISION}" \
  --source-fingerprint "deployed-service" \
  --rag-base-url stage-managed \
  --suite "${PROFILE}" \
  --endpoint-url "${STAGE_URL}" \
  --transport shopper-api \
  --agent-id "${STAGE_AGENT_ID}" \
  --turn-delay-seconds "${TURN_DELAY_SECONDS}"; then
  echo "The run produced artifacts but has operational errors or missing turns." >&2
  echo "Keep ${OUTPUT_DIR} and send it to the project team for diagnosis." >&2
  exit 1
fi

cat <<EOF

Collection completed with no missing or failed turns.
This is not a behavioral pass until a reviewer completes:
  ${OUTPUT_DIR}/client-review.csv

Follow the required review and handoff instructions in:
  ${OUTPUT_DIR}/README.md
EOF
