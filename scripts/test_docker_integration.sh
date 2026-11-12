#!/usr/bin/env bash
set -euo pipefail

# This exercises real OpenClaw and MCP with local API test doubles. It does not
# measure the quality of an evaluated model or of LLM Judge reasoning.
export BENCH_TASK_NAME=travel_beijing_realistic
export BENCH_TASKS_HOST_DIR=./examples
export OPENCLAW_IMAGE="${OPENCLAW_IMAGE:-ghcr.io/openclaw/openclaw:latest}"
export AGENT_API_BASE_URL=http://mock-model:8001/v1
export AGENT_API_KEY=offline-test-key
export AGENT_MODEL=fixture-model
export BENCH_JUDGE_BASE_URL=http://mock-model:8001/v1
export BENCH_JUDGE_API_KEY=offline-test-key
export BENCH_JUDGE_MODEL=fixture-judge
export OPENCLAW_GATEWAY_TOKEN="${OPENCLAW_GATEWAY_TOKEN:-offline-gateway-token}"

if [[ "${BENCH_SKIP_BUILD:-0}" != "1" ]]; then
  docker compose build benchmark
fi
for suffix in a b; do
  export COMPOSE_PROJECT_NAME="probench-realistic-offline-${suffix}"
  export BENCH_RUN_NAME="realistic-offline-${suffix}"
  docker compose up -d --no-build openclaw world model-proxy mock-model
  docker compose run --rm benchmark bench validate examples/travel_beijing_realistic
  docker compose run --rm benchmark bench run examples/travel_beijing_realistic --agent openclaw --judge openai --output "runs/${BENCH_RUN_NAME}"
  docker compose exec -T openclaw sh -lc 'test ! -e /app/examples/travel_beijing_realistic/ground_truth.yaml && test -z "${BENCH_JUDGE_API_KEY+x}" && test -z "${AGENT_API_KEY+x}"'
  docker compose down -v --remove-orphans
done
python3 - <<'PY'
import json
from pathlib import Path
roots = [Path('runs/realistic-offline-a'), Path('runs/realistic-offline-b')]
reports = [json.loads((root / 'report.json').read_text()) for root in roots]
turns = [[json.loads(path.read_text()) for path in sorted((root / 'turns').glob('*.json'))] for root in roots]
for report, run_turns in zip(reports, turns):
    assert report['R_task'] is not None and report['R_notify'] is not None
    assert len(run_turns) == 15
    assert any(action['tool'] == 'flight.list' and action['success'] for turn in run_turns for action in turn['action_trace'])
    assert len({turn['agent_session_reference'] for turn in run_turns}) == 1
    assert all('ground_truth' not in json.dumps(turn['agent_observation']) for turn in run_turns)
assert turns[0][0]['agent_session_reference'] != turns[1][0]['agent_session_reference']
assert turns[0][0]['world_pre']['flight']['air_seg_mu5101']['status'] == 'scheduled'
assert turns[1][0]['world_pre']['flight']['air_seg_mu5101']['status'] == 'scheduled'
print('Realistic-task Docker integration passed (mock APIs; real OpenClaw).')
PY
