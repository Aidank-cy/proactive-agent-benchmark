"""`bench` 命令行入口；具体执行逻辑由任务加载器和运行时负责。"""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path

from .agent import MockAgent
from .judge import MockJudge, OpenAICompatibleJudge
from .openclaw_agent import OpenClawAgent
from .runtime import run, verify_evidence
from .schema import load_task
from .service_plan import write_plan


def main():
    """提供任务校验、公开工具计划、运行和报告四个入口。"""
    parser = argparse.ArgumentParser(prog="bench")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("task_path")
    prepare = commands.add_parser("prepare")
    prepare.add_argument("task_path")
    prepare.add_argument("--output", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("task_path")
    execute.add_argument("--agent", default="openclaw", help="openclaw, mock, or importable module:Class implementing run_turn")
    execute.add_argument("--judge", default="openai", choices=["mock", "openai"])
    execute.add_argument("--output", default="runs/example")
    execute.add_argument("--resume", action="store_true", help="continue after the last completed Tick")
    execute.add_argument("--max-ticks", type=int, help="stop after this many newly completed Ticks")
    report = commands.add_parser("report")
    report.add_argument("run_directory")
    args = parser.parse_args()
    if args.command == "validate":
        task = load_task(args.task_path)
        print(f"Valid: {task.manifest.task_id} ({len(task.events)} events)")
    elif args.command == "prepare":
        # 公开计划只给 World MCP 服务使用，不包含 Judge 专属配置。
        task = load_task(args.task_path)
        write_plan(task, Path(args.output))
        print(f"Prepared public service plan: {args.output}")
    elif args.command == "run":
        task = load_task(args.task_path)
        if args.agent == "openclaw":
            agent = OpenClawAgent()
        elif args.agent == "mock":
            script = json.loads((task.root / "mock_agent.json").read_text(encoding="utf-8"))
            agent = MockAgent(script)
        else:
            module_name, separator, class_name = args.agent.partition(":")
            if not separator:
                parser.error("--agent must be openclaw, mock, or module:Class")
            agent = getattr(importlib.import_module(module_name), class_name)()
        result = run(task, agent, MockJudge() if args.judge == "mock" else OpenAICompatibleJudge(), Path(args.output), resume=args.resume, max_ticks=args.max_ticks)
        print(json.dumps(result if result.get("status") == "paused" else {"R_task": result["R_task"], "R_notify": result["R_notify"]}, indent=2))
    else:
        folder = Path(args.run_directory)
        # 展示报告前先检查冻结证据是否被改动。
        verify_evidence(folder)
        print((folder / "report.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()

