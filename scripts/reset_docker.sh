#!/usr/bin/env bash
set -euo pipefail
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-probench-demo}"
docker compose down -v --remove-orphans
if [[ -n "${BENCH_RUN_NAME:-}" && "${BENCH_RUN_NAME}" =~ ^[A-Za-z0-9_-]+$ && ( -f "runs/${BENCH_RUN_NAME}/run_metadata.json" || -f "runs/${BENCH_RUN_NAME}/agent_config.json" ) ]]; then
  rm -rf -- "runs/${BENCH_RUN_NAME}"
fi
