#!/usr/bin/env python3
"""
MobileWorld 快照管理脚本

通过 Backend API 管理模拟器快照和任务状态重置。

功能:
  - 列出所有可用任务及其对应快照
  - 为指定任务加载快照 (恢复初始状态)
  - 为所有任务批量加载快照
  - 验证任务与快照的对应关系

用法:
  # 列出所有任务及其快照信息
  python snapshot_manager.py list

  # 加载指定任务的快照
  python snapshot_manager.py load --task MattermostCreateChannelTask

  # 加载多个指定任务的快照
  python snapshot_manager.py load --task MattermostCreateChannelTask ReplyEmailTask

  # 加载所有任务的快照 (按顺序)
  python snapshot_manager.py load --all

  # 仅加载 MCP 任务
  python snapshot_manager.py load --all --mcp-only

  # 仅加载 GUI-Only 任务 (不含 MCP 和 User Interaction)
  python snapshot_manager.py load --all --gui-only

  # 加载含 User Interaction 的任务
  python snapshot_manager.py load --all --with-user-interaction

  # 自定义 API 地址和设备
  python snapshot_manager.py list --base-url http://192.168.1.100:6800 --device emulator-5554

  # 通过 SSH 隧道连接远程服务器
  # 先建立隧道: ssh -L 6800:localhost:6800 user@remote
  python snapshot_manager.py load --task MattermostCreateChannelTask
"""

import argparse
import sys
import time
from typing import Any

import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# ──────────────────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────────────────
DEFAULT_BASE_URL = "http://123.60.91.241:9001"
DEFAULT_DEVICE = "emulator-5554"
SNAPSHOT_LOAD_WAIT = 3  # 快照加载后等待秒数


# ──────────────────────────────────────────────────────────
# API 客户端
# ──────────────────────────────────────────────────────────

class SnapshotManagerClient:
    """通过 Backend API 管理快照和任务的客户端。"""

    def __init__(self, base_url: str, device: str):
        self.base_url = base_url.rstrip("/")
        self.device = device

    def _post(self, path: str, data: dict, timeout: int = 60) -> requests.Response:
        url = f"{self.base_url}{path}"
        resp = requests.post(url, json=data, timeout=timeout)
        resp.raise_for_status()
        return resp

    def _get(self, path: str, params: dict | None = None) -> requests.Response:
        url = f"{self.base_url}{path}"
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        return resp

    def health_check(self) -> bool:
        """检查服务器健康状态。"""
        try:
            resp = self._get("/health")
            return resp.json().get("ok", False)
        except Exception:
            return False

    def ensure_initialized(self) -> bool:
        """确保设备已初始化。"""
        try:
            self._post("/init", {"device": self.device})
            return True
        except Exception as e:
            console.print(f"[red]初始化失败: {e}[/red]")
            return False

    def get_task_list(self) -> list[dict[str, Any]]:
        """获取所有任务列表 (含元数据)。"""
        resp = self._get("/task/list")
        return resp.json()

    def get_task_info(self, task_name: str) -> dict[str, Any]:
        """获取单个任务的详细信息。"""
        resp = self._get("/task/metadata", params={"task_name": task_name})
        return resp.json()

    def get_task_info_with_fallback(self, task_meta: dict[str, Any]) -> dict[str, Any]:
        """
        获取任务详情并在 metadata 不可用时回退到 task/list 提供的信息。

        当前部分后端版本的 /task/metadata 不返回 snapshot_tag，因此该方法会统一补齐字段。
        """
        name = task_meta.get("name", "N/A")
        fallback = {
            "name": name,
            "tags": task_meta.get("tags", []),
            "apps": task_meta.get("apps", []),
            "snapshot_tag": None,
        }
        try:
            info = self.get_task_info(name)
            return {
                "name": info.get("name", name),
                "tags": info.get("tags", fallback["tags"]),
                "apps": info.get("apps", fallback["apps"]),
                "snapshot_tag": info.get("snapshot_tag"),
            }
        except Exception:
            return fallback

    def init_task(self, task_name: str) -> bool:
        """
        初始化任务 (自动加载快照)。

        服务器端的 /task/init 端点会:
        1. 从 TaskRegistry 获取任务实例
        2. 调用 task.initialize_task(controller)
        3. 内部自动调用 controller.load_snapshot(task.snapshot_tag)
        """
        try:
            resp = self._post(
                "/task/init",
                {"task_name": task_name, "req_device": self.device},
                timeout=120,
            )
            return True
        except Exception as e:
            console.print(f"[red]初始化任务 {task_name} 失败: {e}[/red]")
            return False

    def tear_down_task(self, task_name: str) -> bool:
        """卸载任务。"""
        try:
            self._post("/task/tear_down", {"task_name": task_name, "req_device": self.device})
            return True
        except Exception:
            return False

    def load_snapshot_via_task_init(self, task_name: str) -> bool:
        """
        通过任务初始化来加载快照。

        这是推荐方式，因为:
        1. 确保任务与快照正确对应
        2. 自动执行快照后的 app_switch + home 操作
        3. 执行时间同步 (如需要)
        4. 清理之前任务的残留状态
        """
        # 先卸载前一个任务 (如果存在)
        self.tear_down_task(task_name)
        time.sleep(1)

        # 初始化任务 (自动加载快照)
        success = self.init_task(task_name)
        if success:
            time.sleep(SNAPSHOT_LOAD_WAIT)

        return success

    def load_snapshot_direct(self, snapshot_tag: str) -> bool:
        """
        直接通过 ADB 加载快照 (不经过任务初始化)。

        适用于需要加载非任务快照的场景。
        注意: 此方法需要通过容器内 ADB 执行，此处仅作为备用方案说明。
        """
        console.print(
            "[yellow]直接快照加载需要通过容器内 ADB 执行:[/yellow]\n"
            f"  adb -s {self.device} emu avd snapshot load {snapshot_tag}"
        )
        return False


# ──────────────────────────────────────────────────────────
# 任务列表与过滤
# ──────────────────────────────────────────────────────────

def filter_tasks(
    task_list: list[dict],
    mcp_only: bool = False,
    gui_only: bool = False,
    with_user_interaction: bool = False,
) -> list[dict]:
    """根据标签过滤任务。"""
    filtered = []
    for task in task_list:
        tags = set(task.get("tags", []))

        if mcp_only and "agent-mcp" not in tags:
            continue
        if gui_only and ("agent-mcp" in tags or "agent-user-interaction" in tags):
            continue
        if with_user_interaction and "agent-user-interaction" not in tags:
            continue

        filtered.append(task)
    return filtered


# ──────────────────────────────────────────────────────────
# 显示任务列表
# ──────────────────────────────────────────────────────────

def cmd_list(client: SnapshotManagerClient, args: argparse.Namespace) -> None:
    """列出所有任务及其快照信息。"""
    console.print(Panel("[cyan]获取任务列表...[/cyan]", title="📋 任务列表", border_style="cyan"))

    if not client.health_check():
        console.print("[red]✗ 服务器不健康，请检查容器状态[/red]")
        sys.exit(1)

    if not client.ensure_initialized():
        console.print("[yellow]! 设备初始化失败，仅尝试读取任务列表（可能无法执行 load）[/yellow]")
    task_list = client.get_task_list()

    # 过滤
    task_list = filter_tasks(
        task_list,
        mcp_only=args.mcp_only,
        gui_only=args.gui_only,
        with_user_interaction=args.with_user_interaction,
    )

    # 打印表格
    table = Table(title=f"可用任务 ({len(task_list)} 个)", show_header=True, header_style="bold magenta")
    table.add_column("#", style="cyan", no_wrap=True)
    table.add_column("任务名称", style="green")
    table.add_column("快照标签", style="yellow")
    table.add_column("应用", style="blue")
    table.add_column("标签", style="dim")

    for idx, task in enumerate(task_list, 1):
        name = task.get("name", "N/A")
        info = client.get_task_info_with_fallback(task)
        snapshot_tag = info.get("snapshot_tag") or "(后端未提供)"
        apps = ", ".join(info.get("apps", [])) or "N/A"
        tags = ", ".join(info.get("tags", [])) or "N/A"

        table.add_row(str(idx), name, snapshot_tag, apps, tags)

    console.print(table)

    # 打印快照统计
    snapshot_tags = set()
    for task in task_list:
        info = client.get_task_info_with_fallback(task)
        st = info.get("snapshot_tag")
        if st:
            snapshot_tags.add(st)

    snapshot_summary = (
        f"使用 {len(snapshot_tags)} 个不同快照: {', '.join(sorted(snapshot_tags))}"
        if snapshot_tags
        else "后端 metadata 未提供 snapshot_tag，无法统计快照标签"
    )

    console.print(
        Panel(
            f"[green]总计 {len(task_list)} 个任务[/green]\n"
            f"[cyan]{snapshot_summary}[/cyan]",
            title="📊 统计",
            border_style="green",
        )
    )


# ──────────────────────────────────────────────────────────
# 加载快照
# ──────────────────────────────────────────────────────────

def cmd_load(client: SnapshotManagerClient, args: argparse.Namespace) -> None:
    """加载指定任务或所有任务的快照。"""
    console.print(
        Panel(
            f"[cyan]目标设备:[/cyan] {client.device}\n"
            f"[cyan]API 地址:[/cyan] {client.base_url}",
            title="🔧 快照加载配置",
            border_style="cyan",
        )
    )

    if not client.health_check():
        console.print("[red]✗ 服务器不健康，请检查容器状态[/red]")
        sys.exit(1)

    if not client.ensure_initialized():
        console.print("[red]✗ 设备初始化失败，无法执行快照加载[/red]")
        sys.exit(1)
    task_list = client.get_task_list()

    # 过滤任务
    task_list = filter_tasks(
        task_list,
        mcp_only=args.mcp_only,
        gui_only=args.gui_only,
        with_user_interaction=args.with_user_interaction,
    )

    if args.load_all:
        # 加载所有任务
        tasks_to_load = task_list
        console.print(
            Panel(
                f"[yellow]准备加载 {len(tasks_to_load)} 个任务的快照[/yellow]",
                title="⏳ 批量加载",
                border_style="yellow",
            )
        )
    elif args.task:
        # 加载指定任务
        task_names = set(t["name"] for t in task_list)
        tasks_to_load = [t for t in task_list if t["name"] in args.task]

        # 检查指定的任务是否存在
        for t in args.task:
            if t not in task_names:
                console.print(f"[red]✗ 任务 '{t}' 不在任务列表中[/red]")

        if not tasks_to_load:
            console.print("[red]✗ 没有有效任务可加载[/red]")
            sys.exit(1)
    else:
        console.print("[red]请指定 --task 或 --all 参数[/red]")
        sys.exit(1)

    # 执行加载
    success_count = 0
    failed_tasks = []

    for idx, task_meta in enumerate(tasks_to_load, 1):
        task_name = task_meta["name"]
        console.print(f"\n[{idx}/{len(tasks_to_load)}] [cyan]加载任务:[/cyan] {task_name}")

        info = client.get_task_info_with_fallback(task_meta)
        snapshot_tag = info.get("snapshot_tag")
        console.print(
            f"  快照标签: [yellow]{snapshot_tag if snapshot_tag else '(后端未提供，仍可按任务初始化加载)'}[/yellow]"
        )

        success = client.load_snapshot_via_task_init(task_name)
        if success:
            console.print(f"  [green]✓ 快照加载成功[/green]")
            success_count += 1
        else:
            console.print(f"  [red]✗ 快照加载失败[/red]")
            failed_tasks.append(task_name)

        # 卸载任务，为下一个任务准备干净状态
        if idx < len(tasks_to_load):
            client.tear_down_task(task_name)
            time.sleep(1)

    # 打印结果汇总
    console.print()
    if failed_tasks:
        console.print(
            Panel(
                f"[green]✓ 成功: {success_count}/{len(tasks_to_load)}[/green]\n"
                f"[red]✗ 失败任务: {', '.join(failed_tasks)}[/red]",
                title="📊 加载结果",
                border_style="yellow",
            )
        )
    else:
        console.print(
            Panel(
                f"[green]✓ 全部 {success_count} 个任务快照加载成功![/green]",
                title="🎉 加载完成",
                border_style="green",
            )
        )


# ──────────────────────────────────────────────────────────
# 验证任务-快照对应关系
# ──────────────────────────────────────────────────────────

def cmd_verify(client: SnapshotManagerClient, args: argparse.Namespace) -> None:
    """验证任务与快照的对应关系。"""
    console.print(Panel("[cyan]验证任务与快照对应关系...[/cyan]", title="🔍 验证", border_style="cyan"))

    if not client.health_check():
        console.print("[red]✗ 服务器不健康[/red]")
        sys.exit(1)

    if not client.ensure_initialized():
        console.print("[red]✗ 设备初始化失败，无法验证任务与快照对应关系[/red]")
        sys.exit(1)
    task_list = client.get_task_list()

    task_list = filter_tasks(
        task_list,
        mcp_only=args.mcp_only,
        gui_only=args.gui_only,
        with_user_interaction=args.with_user_interaction,
    )

    # 收集快照标签分布
    snapshot_to_tasks = {}
    missing_snapshot = []

    for task in task_list:
        info = client.get_task_info_with_fallback(task)
        st = info.get("snapshot_tag")
        if st:
            snapshot_to_tasks.setdefault(st, []).append(task["name"])
        else:
            missing_snapshot.append(task["name"])

    table = Table(title="快照-任务对应关系", show_header=True, header_style="bold magenta")
    table.add_column("快照标签", style="yellow")
    table.add_column("任务数量", justify="right", style="cyan")
    table.add_column("任务列表", style="dim")

    for snapshot_tag, tasks in sorted(snapshot_to_tasks.items()):
        table.add_row(
            snapshot_tag,
            str(len(tasks)),
            ", ".join(tasks[:5]) + ("..." if len(tasks) > 5 else ""),
        )

    console.print(table)

    if missing_snapshot:
        console.print(
            Panel(
                f"[red]{len(missing_snapshot)} 个任务缺少快照标签:[/red]\n"
                + "\n".join(f"  - {t}" for t in missing_snapshot[:10])
                + ("..." if len(missing_snapshot) > 10 else ""),
                title="⚠ 警告",
                border_style="yellow",
            )
        )

    console.print(
        Panel(
            f"[green]{len(snapshot_to_tasks)} 个不同快照[/green] 对应 [cyan]{len(task_list)} 个任务[/cyan]",
            title="✅ 验证完成",
            border_style="green",
        )
    )


# ──────────────────────────────────────────────────────────
# CLI 参数解析
# ──────────────────────────────────────────────────────────

def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MobileWorld 快照管理脚本 - 通过 Backend API 管理模拟器快照和任务状态",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s list                                    # 列出所有任务
  %(prog)s list --gui-only                         # 仅列出 GUI-Only 任务
  %(prog)s load --task MattermostCreateChannel     # 加载指定任务快照
  %(prog)s load --all                              # 加载所有任务快照
  %(prog)s load --all --gui-only                   # 仅加载 GUI-Only 任务
  %(prog)s verify                                  # 验证任务-快照对应关系
        """,
    )

    parser.add_argument(
        "command",
        choices=["list", "load", "verify"],
        help="命令: list (列出任务), load (加载快照), verify (验证对应关系)",
    )

    parser.add_argument(
        "--task", "-t",
        nargs="+",
        default=None,
        help="指定要加载快照的任务名称 (可多个)",
    )
    parser.add_argument(
        "--all", "-a",
        action="store_true",
        dest="load_all",
        help="加载所有任务的快照",
    )

    # 过滤选项
    filter_group = parser.add_argument_group("过滤选项")
    filter_group.add_argument(
        "--mcp-only",
        action="store_true",
        help="仅包含 MCP 任务 (标签: agent-mcp)",
    )
    filter_group.add_argument(
        "--gui-only",
        action="store_true",
        help="仅包含 GUI-Only 任务 (排除 agent-mcp 和 agent-user-interaction)",
    )
    filter_group.add_argument(
        "--with-user-interaction",
        action="store_true",
        help="包含 User Interaction 任务 (标签: agent-user-interaction)",
    )

    # 连接选项
    conn_group = parser.add_argument_group("连接选项")
    conn_group.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Backend API 地址 (默认: {DEFAULT_BASE_URL})",
    )
    conn_group.add_argument(
        "--device",
        default=DEFAULT_DEVICE,
        help=f"设备标识符 (默认: {DEFAULT_DEVICE})",
    )

    return parser


# ──────────────────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────────────────

def main():
    parser = create_parser()
    args = parser.parse_args()

    client = SnapshotManagerClient(
        base_url=args.base_url,
        device=args.device,
    )

    if args.command == "list":
        cmd_list(client, args)
    elif args.command == "load":
        if not args.task and not args.load_all:
            parser.error("load 命令需要指定 --task 或 --all 参数")
        cmd_load(client, args)
    elif args.command == "verify":
        cmd_verify(client, args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
