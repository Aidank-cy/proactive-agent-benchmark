"""Opt-in semantic check. Requires a real Judge API and makes paid external calls."""
import json
import os
from pathlib import Path

import pytest
import yaml

from probench.agent import MockAgent
from probench.judge import OpenAICompatibleJudge
from probench.runtime import run
from probench.schema import load_task


ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples/travel_beijing_20turn"


@pytest.mark.skipif(os.environ.get("RUN_REAL_TRAVEL_JUDGE") != "1", reason="real Judge API semantic check is opt-in")
def test_real_judge_against_key_ground_truth(tmp_path):
    assert os.environ.get("BENCH_JUDGE_API_KEY") and os.environ.get("BENCH_JUDGE_MODEL"), "real Judge credentials required"
    task = load_task(TASK)
    script = json.loads((TASK / "mock_agent.json").read_text())
    truth = yaml.safe_load((TASK / "ground_truth.yaml").read_text())
    result = run(task, MockAgent(script), OpenAICompatibleJudge(), tmp_path / "real-judge")
    assert result["total_agent_turns"] == truth["expected_agent_turns"]
    statuses = {row["opportunity_id"]: row["status"] for row in result["opportunities"]}
    for opportunity_id, expected in truth["opportunities"].items():
        assert statuses.get(opportunity_id) == expected["final_status"], f"semantic mismatch for {opportunity_id}"
    assert result["false_notification_count"] == truth["expected_metrics"]["false_notification_count"]
    assert result["turn_judgments"][10]["task"]["category"] == "harmful_violation"
