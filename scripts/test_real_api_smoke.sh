#!/usr/bin/env bash
set -euo pipefail

for key in AGENT_API_BASE_URL AGENT_API_KEY AGENT_MODEL BENCH_JUDGE_BASE_URL BENCH_JUDGE_API_KEY BENCH_JUDGE_MODEL OPENCLAW_GATEWAY_TOKEN; do
  if [[ -z "${!key:-}" ]]; then
    echo "Real API smoke test not executed: missing ${key}" >&2
    exit 77
  fi
done
if [[ "${AGENT_API_BASE_URL}" == *mock-model* || "${BENCH_JUDGE_BASE_URL}" == *mock-model* || "${AGENT_API_KEY}" == offline-test-key || "${BENCH_JUDGE_API_KEY}" == offline-test-key || "${AGENT_MODEL}" == fixture-model || "${BENCH_JUDGE_MODEL}" == fixture-judge ]]; then
  echo "Real API smoke test not executed: offline fixture API settings are still configured" >&2
  exit 77
fi
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-probench-real}"
export BENCH_RUN_NAME="${BENCH_RUN_NAME:-openclaw-real}"
export BENCH_TASK_NAME="${BENCH_TASK_NAME:-synthetic}"
docker compose build benchmark
docker compose up -d --no-build openclaw world model-proxy
docker compose run --rm benchmark bench run "examples/${BENCH_TASK_NAME}" --agent openclaw --judge openai --output "runs/${BENCH_RUN_NAME}"
docker compose run --rm benchmark bench report "runs/${BENCH_RUN_NAME}"
