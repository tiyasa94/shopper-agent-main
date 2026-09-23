#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
ROOT_DIR=$( cd -- "${SCRIPT_DIR}/.." &> /dev/null && pwd )
PLATFORM_ROOT="${SHOPPER_PLATFORM_ROOT:-${ROOT_DIR}/../shopper-platform}"
LOCAL_PLATFORM="${ROOT_DIR}/scripts/local-platform.sh"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
RUN_STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RUN_DIR="${EVALUATION_RUN_DIR:-${ROOT_DIR}/artifacts/evaluations/rag-plan-summary-modes-${RUN_STAMP}}"
declare -a RUN_OPTIONS=("$@")

for path in "${PYTHON_BIN}" "${LOCAL_PLATFORM}" "${PLATFORM_ROOT}/apps/rag-api"; do
  if [[ ! -e "${path}" ]]; then
    echo "Missing experiment input: ${path}" >&2
    exit 1
  fi
done

restore_stack() {
  local status=$?
  trap - EXIT INT TERM
  echo "Restoring protected plan-summary mode..." >&2
  SHOPPER_PLATFORM_ROOT="${PLATFORM_ROOT}" "${LOCAL_PLATFORM}" stop || true
  SHOPPER_PLATFORM_ROOT="${PLATFORM_ROOT}" \
    RETRIEVAL_PLAN_SUMMARY_MODE=protected \
    SKIP_LOCAL_WXO_IMPORT=1 \
    "${LOCAL_PLATFORM}" start || true
  exit "${status}"
}
trap restore_stack EXIT INT TERM

mkdir -p "${RUN_DIR}"
for mode in disabled fused protected; do
  echo "Running direct retrieval arm: ${mode}"
  SHOPPER_PLATFORM_ROOT="${PLATFORM_ROOT}" "${LOCAL_PLATFORM}" stop
  SHOPPER_PLATFORM_ROOT="${PLATFORM_ROOT}" \
    RETRIEVAL_PLAN_SUMMARY_MODE="${mode}" \
    SKIP_LOCAL_WXO_IMPORT=1 \
    "${LOCAL_PLATFORM}" start
  "${PYTHON_BIN}" -m evaluation retrieval run \
    --arm structured \
    --rag-root "${PLATFORM_ROOT}/apps/rag-api" \
    --output "${RUN_DIR}/${mode}" \
    --force \
    ${RUN_OPTIONS[@]+"${RUN_OPTIONS[@]}"}
done

for challenger in fused protected; do
  "${PYTHON_BIN}" -m evaluation retrieval compare \
    --baseline "${RUN_DIR}/disabled" \
    --challenger "${RUN_DIR}/${challenger}" \
    --output "${RUN_DIR}/comparisons/disabled-vs-${challenger}"
done

echo "RAG mode experiments complete: ${RUN_DIR}"
