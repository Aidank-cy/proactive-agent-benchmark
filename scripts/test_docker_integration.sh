#!/usr/bin/env bash
set -euo pipefail

export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-probench-offline}"
export BENCH_RUN_NAME="${BENCH_RUN_NAME:-openclaw-offline}"
export OPENCLAW_IMAGE="${OPENCLAW_IMAGE:-terrarium/openclaw:latest}"
export AGENT_API_BASE_URL="http://mock-model:8001/v1"
export AGENT_API_KEY="offline-test-key"
export AGENT_MODEL="fixture-model"
export BENCH_JUDGE_BASE_URL="http://mock-model:8001/v1"
export BENCH_JUDGE_API_KEY="offline-test-key"
export BENCH_JUDGE_MODEL="fixture-judge"
export OPENCLAW_GATEWAY_TOKEN="${OPENCLAW_GATEWAY_TOKEN:-offline-gateway-token}"

if [[ "${BENCH_SKIP_BUILD:-0}" != "1" ]]; then
  docker compose build benchmark
fi
docker compose up -d --no-build openclaw world model-proxy mock-model
docker compose run --rm benchmark bench run examples/synthetic --agent openclaw --judge openai --output "runs/${BENCH_RUN_NAME}"
docker compose run --rm benchmark bench report "runs/${BENCH_RUN_NAME}"
python3 - "runs/${BENCH_RUN_NAME}" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
report = json.loads((root / 'report.json').read_text())
turns = [json.loads(p.read_text()) for p in sorted((root / 'turns').glob('*.json'))]
assert report['R_task'] is not None and report['R_notify'] is not None, report
assert len(turns) == 4
assert any(a['tool'] == 'kv.set' and a['success'] for t in turns for a in t['action_trace'])
assert any(a['tool'] == 'kv.get' and a['success'] for t in turns for a in t['action_trace'])
assert any(a['tool'] == 'notify_user' and a['success'] for t in turns for a in t['action_trace'])
assert len({t['agent_session_reference'] for t in turns}) == 1
assert any(t['agent_response']['proactive_notifications'] for t in turns)
assert all('notification_rules' not in json.dumps(t['agent_observation']) for t in turns)
assert turns[1]['world_pre']['kv']['item'] == 1
assert report['required_opportunity_count'] == 1
assert json.loads((root / 'run_metadata.json').read_text())['judge_runtime'] == 'OpenAICompatibleJudge'
print('Docker OpenClaw integration assertions passed')
PY
docker compose logs mock-model | grep 'MOCK_MODEL_REQUEST model=fixture-model'
docker compose logs mock-model | grep 'MOCK_MODEL_REQUEST model=fixture-judge'
docker compose exec -T openclaw sh -lc 'test ! -e /app/examples/synthetic/judge/notification_rules.yaml && test -z "${BENCH_JUDGE_API_KEY+x}" && test -z "${AGENT_API_KEY+x}"'

first_run="${BENCH_RUN_NAME}"
first_project="${COMPOSE_PROJECT_NAME}"
export BENCH_RUN_NAME="${first_run}-isolation"
export COMPOSE_PROJECT_NAME="${first_project}-isolation"
docker compose up -d --no-build openclaw world model-proxy mock-model
docker compose run --rm benchmark bench run examples/synthetic --agent openclaw --judge openai --output "runs/${BENCH_RUN_NAME}"
python3 - "runs/${first_run}" "runs/${BENCH_RUN_NAME}" <<'PY'
import json, pathlib, sys
a, b = [pathlib.Path(x) for x in sys.argv[1:]]
first_a = json.loads((a / 'turns/turn_0001.json').read_text())
first_b = json.loads((b / 'turns/turn_0001.json').read_text())
assert first_a['world_pre']['kv']['item'] == first_b['world_pre']['kv']['item'] == 0
assert first_a['agent_session_reference'] != first_b['agent_session_reference']
assert json.loads((a / 'turns/turn_0004.json').read_text())['world_post']['kv']['item'] == 1
print('Docker task and session isolation assertions passed')
PY
docker compose down -v --remove-orphans
