#!/usr/bin/env python3
"""并发初始化多个模拟器并启动 auto-search 采集。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from auto_explore.adapters.snapshot_init import load_snapshot_manager_client


SnapshotManagerClient = load_snapshot_manager_client()


@dataclass(frozen=True)
class SimulatorConfig:
    name: str
    backend_url: str
    adb_endpoint: str
    init_device: str


def _extract_task_names(task_list: list[dict]) -> set[str]:
    names: set[str] = set()
    for task in task_list:
        if not isinstance(task, dict):
            continue
        name = task.get("name")
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    return names


def _normalize_simulator_entry(entry: dict, index: int) -> SimulatorConfig:
    try:
        return SimulatorConfig(
            name=str(entry["name"]).strip(),
            backend_url=str(entry["backend_url"]).rstrip("/"),
            adb_endpoint=str(entry["adb_endpoint"]).strip(),
            init_device=str(entry.get("init_device", "emulator-5554")).strip(),
        )
    except KeyError as exc:
        raise ValueError(f"simulators[{index}] 缺少字段: {exc.args[0]}") from exc


def load_simulators_from_json(content: str) -> list[SimulatorConfig]:
    payload = json.loads(content)
    entries = payload.get("simulators")
    if not isinstance(entries, list) or not entries:
        raise ValueError("JSON 配置缺少非空 simulators 列表")

    simulators: list[SimulatorConfig] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"simulators[{index}] 必须是对象")
        simulators.append(_normalize_simulator_entry(entry, index))
    return simulators


def parse_simulator_specs(specs: Iterable[str]) -> list[SimulatorConfig]:
    simulators: list[SimulatorConfig] = []
    for spec in specs:
        parts = [part.strip() for part in spec.split("|")]
        if len(parts) not in (3, 4):
            raise ValueError(
                f"模拟器配置格式无效: {spec}，期望 name|backend_url|adb_endpoint[|init_device]"
            )
        simulators.append(
            SimulatorConfig(
                name=parts[0],
                backend_url=parts[1].rstrip("/"),
                adb_endpoint=parts[2],
                init_device=parts[3] if len(parts) == 4 else "emulator-5554",
            )
        )
    return simulators


def load_simulators(simulator_file: str | None, simulator_specs: list[str]) -> list[SimulatorConfig]:
    simulators: list[SimulatorConfig] = []
    if simulator_file:
        content = Path(simulator_file).read_text(encoding="utf-8")
        simulators.extend(load_simulators_from_json(content))
    if simulator_specs:
        simulators.extend(parse_simulator_specs(simulator_specs))
    if not simulators:
        raise ValueError("请至少通过 --simulator-file 或 --simulator 提供一个模拟器配置")
    return simulators


def init_simulators(simulators: list[SimulatorConfig], task_names: list[str]) -> None:
    if not task_names:
        raise ValueError("task_names 不能为空")

    for simulator in simulators:
        client = SnapshotManagerClient(base_url=simulator.backend_url, device=simulator.init_device)
        if not client.health_check():
            raise RuntimeError(f"{simulator.name} backend 不健康: {simulator.backend_url}")
        if not client.ensure_initialized():
            raise RuntimeError(f"{simulator.name} 初始化失败: {simulator.backend_url}")
        try:
            available_task_names = _extract_task_names(client.get_task_list())
        except Exception as exc:
            raise RuntimeError(
                f"{simulator.name} 获取任务列表失败 (backend={simulator.backend_url}): {exc}"
            ) from exc
        missing_tasks = [task_name for task_name in task_names if task_name not in available_task_names]
        if missing_tasks:
            missing_text = ", ".join(missing_tasks)
            raise RuntimeError(
                f"{simulator.name} backend 缺少任务: {missing_text} (backend={simulator.backend_url})"
            )

        for task_name in task_names:
            try:
                success = client.load_snapshot_via_task_init(task_name)
            except Exception as exc:
                raise RuntimeError(
                    f"{simulator.name} 加载任务快照异常: {task_name} "
                    f"(backend={simulator.backend_url}, init_device={simulator.init_device}, adb={simulator.adb_endpoint}): {exc}"
                ) from exc
            if not success:
                raise RuntimeError(
                    f"{simulator.name} 加载任务快照失败: {task_name} "
                    f"(backend={simulator.backend_url}, init_device={simulator.init_device}, adb={simulator.adb_endpoint})"
                )


def build_auto_search_command(
    simulator: SimulatorConfig,
    app_name: str,
    depth: int,
    breadth: int,
    auto_search_args: list[str],
    output_root: Path,
) -> list[str]:
    data_dir = output_root / simulator.name
    return [
        "python",
        "-m",
        "auto_explore.cli.auto_search",
        "--app_name",
        app_name,
        "--depth",
        str(depth),
        "--breadth",
        str(breadth),
        "--adb_endpoint",
        simulator.adb_endpoint,
        "--data_dir",
        str(data_dir),
        *auto_search_args,
    ]


def start_auto_search_processes(
    simulators: list[SimulatorConfig],
    app_name: str,
    depth: int,
    breadth: int,
    auto_search_args: list[str],
    output_root: Path,
) -> list[tuple[SimulatorConfig, subprocess.Popen]]:
    processes: list[tuple[SimulatorConfig, subprocess.Popen]] = []
    output_root.mkdir(parents=True, exist_ok=True)

    for simulator in simulators:
        simulator_dir = output_root / simulator.name
        simulator_dir.mkdir(parents=True, exist_ok=True)
        log_path = simulator_dir / "runner.log"
        cmd = build_auto_search_command(
            simulator=simulator,
            app_name=app_name,
            depth=depth,
            breadth=breadth,
            auto_search_args=auto_search_args,
            output_root=output_root,
        )
        log_file = log_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=str(Path(__file__).resolve().parents[4]),
            env=_build_subprocess_env(),
        )
        processes.append((simulator, process))
    return processes


def _build_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    repo_root = Path(__file__).resolve().parents[4]
    src_root = repo_root / "auto_explore" / "src"
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(src_root) if not existing else f"{src_root}{os.pathsep}{existing}"
    return env


def wait_for_processes(processes: list[tuple[SimulatorConfig, subprocess.Popen]]) -> int:
    exit_code = 0
    for simulator, process in processes:
        return_code = process.wait()
        if return_code != 0 and exit_code == 0:
            exit_code = return_code
        print(f"[{simulator.name}] exit_code={return_code}")
    return exit_code


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="并发启动多个模拟器的 auto-search 采集")
    parser.add_argument("--simulator-file", type=str, default=None, help="模拟器 JSON 配置文件路径")
    parser.add_argument(
        "--simulator",
        action="append",
        default=[],
        help="额外模拟器配置，格式 name|backend_url|adb_endpoint[|init_device]，可重复传入",
    )
    parser.add_argument("--task-name", nargs="+", required=True, help="启动前依次恢复的 task_name 列表")
    parser.add_argument("--app_name", required=True, help="auto-search 的目标 App 名称")
    parser.add_argument("--depth", type=int, required=True, help="auto-search 探索深度")
    parser.add_argument("--breadth", type=int, required=True, help="auto-search 探索广度")
    parser.add_argument("--output-root", type=str, default="", help="并发采集输出根目录")
    parser.add_argument(
        "auto_search_args",
        nargs=argparse.REMAINDER,
        help="透传给 auto-search 的额外参数，使用方式: -- --device Android ...",
    )
    return parser


def normalize_auto_search_args(args: list[str]) -> list[str]:
    if args and args[0] == "--":
        return args[1:]
    return args


def default_output_root(app_name: str) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return Path(__file__).resolve().parents[3] / "data" / app_name / timestamp


def main() -> None:
    parser = create_parser()
    args = parser.parse_args()

    simulators = load_simulators(args.simulator_file, args.simulator)
    auto_search_args = normalize_auto_search_args(args.auto_search_args)
    output_root = Path(args.output_root) if args.output_root else default_output_root(args.app_name)

    print(f"Loaded {len(simulators)} simulators")
    for simulator in simulators:
        print(
            f"  - {simulator.name}: backend={simulator.backend_url}, "
            f"init_device={simulator.init_device}, adb={simulator.adb_endpoint}"
        )

    print(f"Initializing snapshots with tasks: {', '.join(args.task_name)}")
    init_simulators(simulators, args.task_name)

    processes = start_auto_search_processes(
        simulators=simulators,
        app_name=args.app_name,
        depth=args.depth,
        breadth=args.breadth,
        auto_search_args=auto_search_args,
        output_root=output_root,
    )
    print(f"Started {len(processes)} auto-search processes. Logs root: {output_root}")
    raise SystemExit(wait_for_processes(processes))


if __name__ == "__main__":
    main()
