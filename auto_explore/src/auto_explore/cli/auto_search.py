import argparse
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from auto_explore.adapters.device import AndroidDevice, HarmonyDevice
from auto_explore.core import settings
from auto_explore.core.dfs import explore_dfs, init_decider_client, init_explorer_client
from auto_explore.core.explorer import ExplorerCache, ScreenStateCache
from auto_explore.core.ui_collect import (
    _load_ui_page_index,
    _mark_unfinished_collect_tasks,
    _next_page_id,
    ui_collect_worker,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


DECIDER_MODEL_PLACEHOLDER = settings.DECIDER_MODEL_PLACEHOLDER


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MobiAgent Auto Search (DFS + backtracking)")
    parser.add_argument("--app_name", type=str, required=True, help="目标App名称（与设备映射一致）")
    parser.add_argument("--depth", type=int, required=True, help="探索深度 D")
    parser.add_argument("--breadth", type=int, required=True, help="每层探索广度 H")

    parser.add_argument("--device", type=str, default="Android", choices=["Android", "Harmony"], help="设备类型")
    parser.add_argument("--adb_endpoint", type=str, default="", help="Android 设备 ADB 连接地址，例如 127.0.0.1:5555")
    parser.add_argument("--service_ip", type=str, default="localhost", help="Decider 服务IP")
    parser.add_argument("--decider_port", type=int, default=8000, help="Decider 服务端口")

    parser.add_argument("--decider_api_key", type=str, default=os.getenv("DECIDER_API_KEY", "mobiagent-key"), help="Decider API Key")
    parser.add_argument("--decider_base_url", type=str, default="", help="Decider Base URL（优先于 service_ip+port）")
    parser.add_argument("--decider_model", type=str, default="", help="Decider 模型名（为空时使用占位符）")

    parser.add_argument("--openrouter_base_url", type=str, default="https://openrouter.ai/api/v1", help="Explorer 的 Base URL")
    parser.add_argument("--openrouter_api_key", type=str, default=os.getenv("OPENROUTER_API_KEY", ""), help="OpenRouter API Key")
    parser.add_argument("--explorer_model", type=str, default="google/gemini-3-flash-preview", help="通用大模型名称")

    parser.add_argument("--use_qwen3", choices=["on", "off"], default="on", help="是否按Qwen3坐标格式换算")
    parser.add_argument(
        "--allow_hierarchy_text_decider",
        choices=["on", "off"],
        default="on",
        help="是否允许使用层级文本作为 decider 输出动作",
    )
    parser.add_argument("--data_dir", type=str, default=None, help="结果目录")
    parser.add_argument("--enable_ui_semantic_collect", choices=["on", "off"], default="off", help="是否启用页面图标采集")
    parser.add_argument("--ui_collect_async", choices=["on", "off"], default="on", help="页面采集是否异步执行")
    parser.add_argument("--ui_collect_queue_size", type=int, default=256, help="页面采集任务队列容量")
    parser.add_argument("--ui_collect_num_workers", type=int, default=1, help="页面采集工作线程数，默认1（兼容原行为）；推荐 2-4")
    parser.add_argument("--ui_collect_drain_on_exit", choices=["on", "off"], default="on", help="退出前是否等待采集队列清空")
    parser.add_argument("--ui_collect_drain_timeout_sec", type=int, default=180, help="退出前等待采集队列清空的超时时间(秒)")
    parser.add_argument("--ui_collect_use_vlm", choices=["on", "off"], default="on", help="页面采集是否启用VLM")
    parser.add_argument("--ui_collect_vlm_text_only", choices=["on", "off"], default="off", help="页面采集文本是否全走VLM")
    parser.add_argument("--ui_collect_vlm_model", type=str, default="qwen/qwen3-vl-30b-a3b-instruct", help="页面采集VLM模型")
    parser.add_argument("--ui_collect_base_url", type=str, default="", help="页面采集 VLM 的 Base URL；为空时复用 openrouter_base_url")
    parser.add_argument("--ui_collect_api_key", type=str, default="", help="页面采集 VLM 的 API Key；为空时复用 openrouter_api_key")
    parser.add_argument("--ui_collect_max_items", type=int, default=32, help="页面采集最多元素数")
    parser.add_argument("--ui_collect_max_vlm_calls", type=int, default=12, help="页面采集VLM调用预算")
    parser.add_argument("--ui_collect_min_area", type=int, default=16, help="页面采集最小框面积")
    parser.add_argument("--page_load_wait_sec", type=float, default=1.5, help="动作后固定等待秒数（等页面开始渲染）")
    parser.add_argument("--page_load_stable_max_polls", type=int, default=6, help="页面稳定轮询最大次数（每次0.5s，总最大等待=次数×0.5s）")
    parser.add_argument("--bbox_iou_threshold", type=float, default=0.3, help="BBox精炼IoU阈值(0~1)，模型坐标越不准确则调低")
    parser.add_argument("--bbox_center_dist_ratio", type=float, default=0.08, help="BBox精炼中心距/对角线比例，模型偏差大则调高")
    parser.add_argument("--bbox_area_ratio_min", type=float, default=0.5, help="BBox精炼面积比下限")
    parser.add_argument("--bbox_area_ratio_max", type=float, default=2.0, help="BBox精炼面积比上限")
    parser.add_argument("--popup_dismiss_max_attempts", type=int, default=2, help="弹窗自动关闭最大尝试次数，0 表示禁用")
    return parser.parse_args()


def _validate_args(args: argparse.Namespace) -> None:
    if args.depth <= 0:
        raise ValueError("depth 必须 > 0")
    if args.breadth <= 0:
        raise ValueError("breadth 必须 > 0")
    if not args.openrouter_api_key:
        raise ValueError("请通过 --openrouter_api_key 或环境变量 OPENROUTER_API_KEY 提供密钥")
    if args.ui_collect_vlm_text_only == "on" and args.ui_collect_use_vlm != "on":
        raise ValueError("ui_collect_vlm_text_only=on requires ui_collect_use_vlm=on")
    if args.ui_collect_queue_size <= 0:
        raise ValueError("ui_collect_queue_size 必须 > 0")
    if args.ui_collect_drain_timeout_sec < 0:
        raise ValueError("ui_collect_drain_timeout_sec 必须 >= 0")


def _apply_runtime_overrides(args: argparse.Namespace) -> None:
    settings.PAGE_LOAD_WAIT_SEC = args.page_load_wait_sec
    settings.PAGE_LOAD_STABLE_MAX_POLLS = args.page_load_stable_max_polls
    settings.BBOX_REFINE_IOU_THRESHOLD = args.bbox_iou_threshold
    settings.BBOX_REFINE_CENTER_DIST_RATIO = args.bbox_center_dist_ratio
    settings.BBOX_REFINE_AREA_RATIO_MIN = args.bbox_area_ratio_min
    settings.BBOX_REFINE_AREA_RATIO_MAX = args.bbox_area_ratio_max


def _resolve_data_dir(args: argparse.Namespace) -> str:
    if args.data_dir:
        return args.data_dir
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return str(Path(__file__).resolve().parents[3] / "data" / args.app_name / timestamp)


def _init_device(args: argparse.Namespace):
    if args.device == "Android":
        return AndroidDevice(adb_endpoint=args.adb_endpoint or None)
    return HarmonyDevice()


def main() -> None:
    args = parse_args()
    _validate_args(args)
    _apply_runtime_overrides(args)

    data_dir = _resolve_data_dir(args)
    os.makedirs(data_dir, exist_ok=True)
    device = _init_device(args)

    explorer_base_url = args.openrouter_base_url
    explorer_api_key = args.openrouter_api_key
    ui_collect_base_url = args.ui_collect_base_url or explorer_base_url
    ui_collect_api_key = args.ui_collect_api_key or explorer_api_key

    decider_client = init_decider_client(args.service_ip, args.decider_port, args.decider_base_url, args.decider_api_key)
    explorer_client = init_explorer_client(explorer_base_url, explorer_api_key)
    use_qwen3 = args.use_qwen3 == "on"
    allow_hierarchy_text_decider = args.allow_hierarchy_text_decider == "on"
    enable_ui_semantic_collect = args.enable_ui_semantic_collect == "on"
    ui_collect_async = args.ui_collect_async == "on"
    ui_collect_drain_on_exit = args.ui_collect_drain_on_exit == "on"
    logging.info("Explorer provider base_url=%s", explorer_base_url)
    logging.info("UI collect provider base_url=%s", ui_collect_base_url)

    logging.info("Starting app: %s", args.app_name)
    device.start_app(args.app_name)
    time.sleep(1.5)

    actions: List[Dict[str, Any]] = []
    reacts: List[Dict[str, Any]] = []
    step_counter = [0]
    path_counter = [0]
    page_counter = [1]

    steps_dir = os.path.join(data_dir, "steps")
    paths_dir = os.path.join(data_dir, "paths")
    ui_pages_dir = os.path.join(data_dir, "ui-pages")
    index_path = os.path.join(ui_pages_dir, "pages_index.json")
    os.makedirs(steps_dir, exist_ok=True)
    os.makedirs(paths_dir, exist_ok=True)
    os.makedirs(ui_pages_dir, exist_ok=True)

    page_registry: Dict[str, Dict[str, Any]] = _load_ui_page_index(index_path)
    page_counter[0] = _next_page_id(page_registry)
    queue_lock = threading.Lock()
    index_lock = threading.Lock()
    collect_queue: Optional["queue.Queue[Dict[str, Any]]"] = None
    collect_stop_event: Optional[threading.Event] = None
    collect_threads: List[threading.Thread] = []

    if enable_ui_semantic_collect and ui_collect_async:
        collect_queue = queue.Queue(maxsize=args.ui_collect_queue_size)
        collect_stop_event = threading.Event()
        worker_kwargs = {
            "collect_queue": collect_queue,
            "stop_event": collect_stop_event,
            "page_registry": page_registry,
            "queue_lock": queue_lock,
            "index_lock": index_lock,
            "index_path": index_path,
            "ui_collect_use_vlm": args.ui_collect_use_vlm == "on",
            "ui_collect_vlm_text_only": args.ui_collect_vlm_text_only == "on",
            "ui_collect_model": args.ui_collect_vlm_model,
            "ui_collect_max_items": args.ui_collect_max_items,
            "ui_collect_max_vlm_calls": args.ui_collect_max_vlm_calls,
            "ui_collect_min_area": args.ui_collect_min_area,
            "ui_collect_base_url": ui_collect_base_url,
            "ui_collect_api_key": ui_collect_api_key,
        }
        num_workers = max(1, args.ui_collect_num_workers)
        for _ in range(num_workers):
            thread = threading.Thread(target=ui_collect_worker, kwargs=worker_kwargs, daemon=True)
            thread.start()
            collect_threads.append(thread)
        logging.info("Started %s ui_collect_worker thread(s).", num_workers)

    try:
        decider_model = args.decider_model or DECIDER_MODEL_PLACEHOLDER
        visited_tasks: Dict[str, set] = {}
        explorer_cache = ExplorerCache(ttl_sec=300.0)
        screen_cache = ScreenStateCache(staleness_sec=0.3)

        explore_dfs(
            app_name=args.app_name,
            depth_limit=args.depth,
            breadth=args.breadth,
            current_depth=0,
            decider_client=decider_client,
            decider_model=decider_model,
            explorer_client=explorer_client,
            explorer_model=args.explorer_model,
            device=device,
            device_type=args.device,
            use_qwen3=use_qwen3,
            allow_hierarchy_text_decider=allow_hierarchy_text_decider,
            data_dir=data_dir,
            actions=actions,
            reacts=reacts,
            step_counter=step_counter,
            path_counter=path_counter,
            page_counter=page_counter,
            steps_dir=steps_dir,
            paths_dir=paths_dir,
            enable_ui_semantic_collect=enable_ui_semantic_collect and ui_collect_async,
            ui_pages_dir=ui_pages_dir,
            page_registry=page_registry,
            collect_queue=collect_queue,
            queue_lock=queue_lock,
            index_lock=index_lock,
            index_path=index_path,
            ui_collect_async=ui_collect_async,
            ui_collect_queue_size=args.ui_collect_queue_size,
            visited_tasks=visited_tasks,
            explorer_cache=explorer_cache,
            screen_cache=screen_cache,
            popup_dismiss_max_attempts=args.popup_dismiss_max_attempts,
        )
    finally:
        if enable_ui_semantic_collect and ui_collect_async and collect_queue is not None:
            if ui_collect_drain_on_exit:
                start_wait = time.time()
                while collect_queue.unfinished_tasks > 0:
                    if args.ui_collect_drain_timeout_sec and (time.time() - start_wait) >= args.ui_collect_drain_timeout_sec:
                        logging.warning("ui_collect queue drain timeout reached, marking pending tasks as skipped")
                        break
                    time.sleep(0.2)
            if collect_queue.unfinished_tasks > 0:
                _mark_unfinished_collect_tasks(
                    page_registry=page_registry,
                    queue_lock=queue_lock,
                    index_lock=index_lock,
                    index_path=index_path,
                )
            if collect_stop_event is not None:
                collect_stop_event.set()
            for _ in collect_threads:
                try:
                    collect_queue.put_nowait({"type": "stop"})
                except Exception:
                    pass
            for thread in collect_threads:
                thread.join(timeout=2.0)
            collect_threads.clear()
        logging.info("Auto-search finished. data_dir=%s", data_dir)


if __name__ == "__main__":
    main()
