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
from auto_explore.core.artifacts import flush_artifact_tasks
from auto_explore.core.dfs import explore_dfs, init_decider_client, init_explorer_client
from auto_explore.core.explorer import ExplorerCache, ScreenStateCache
from auto_explore.core.runtime import (
    FeatureFlags,
    MetricsCollector,
    RuntimeContext,
    add_feature_flag_args,
    build_metrics_payload,
    write_metrics_payload,
)
from auto_explore.core.ui_collect import (
    _load_ui_page_index,
    _mark_unfinished_collect_tasks,
    _next_page_id,
    ui_collect_worker,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


DECIDER_MODEL_PLACEHOLDER = settings.DECIDER_MODEL_PLACEHOLDER


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MobiAgent Auto Search (DFS + backtracking)")
    parser.add_argument("--app_name", type=str, required=True, help="Target app name")
    parser.add_argument("--depth", type=int, required=True, help="DFS depth")
    parser.add_argument("--breadth", type=int, required=True, help="DFS breadth")

    parser.add_argument("--device", type=str, default="Android", choices=["Android", "Harmony"], help="Device type")
    parser.add_argument("--adb_endpoint", type=str, default="", help="Android ADB endpoint, for example 127.0.0.1:5555")
    parser.add_argument("--service_ip", type=str, default="localhost", help="Decider service IP")
    parser.add_argument("--decider_port", type=int, default=8000, help="Decider service port")

    parser.add_argument("--decider_api_key", type=str, default=os.getenv("DECIDER_API_KEY", ""), help="Decider API key")
    parser.add_argument("--decider_base_url", type=str, default="", help="Decider base URL")
    parser.add_argument("--decider_model", type=str, default="", help="Decider model name")

    parser.add_argument("--openrouter_base_url", type=str, default="https://openrouter.ai/api/v1", help="Explorer base URL")
    parser.add_argument("--openrouter_api_key", type=str, default=os.getenv("OPENROUTER_API_KEY", ""), help="Explorer API key")
    parser.add_argument("--explorer_model", type=str, default="google/gemini-3-flash-preview", help="Explorer model name")
    parser.add_argument(
        "--explorer_disable_thinking",
        choices=["on", "off"],
        default="off",
        help="Disable Qwen reasoning/thinking mode for Explorer requests",
    )

    parser.add_argument("--use_qwen3", choices=["on", "off"], default="on", help="Whether to use Qwen3 coordinate conversion")
    parser.add_argument(
        "--allow_hierarchy_text_decider",
        choices=["on", "off"],
        default="on",
        help="Allow hierarchy-text direct grounding before decider calls",
    )
    parser.add_argument("--data_dir", type=str, default=None, help="Output directory")
    parser.add_argument("--metrics_output_path", type=str, default="", help="Path to write metrics.json")
    parser.add_argument("--experiment_tag", type=str, default="default", help="Experiment tag written into metrics.json")
    parser.add_argument("--enable_ui_semantic_collect", choices=["on", "off"], default="off", help="Enable UI semantic collection")
    parser.add_argument("--ui_collect_async", choices=["on", "off"], default="on", help="Run UI semantic collection asynchronously")
    parser.add_argument("--ui_collect_queue_size", type=int, default=256, help="UI collection queue size")
    parser.add_argument("--ui_collect_num_workers", type=int, default=1, help="UI collection worker count")
    parser.add_argument("--ui_collect_drain_on_exit", choices=["on", "off"], default="on", help="Drain UI collection queue before exit")
    parser.add_argument("--ui_collect_drain_timeout_sec", type=int, default=180, help="UI collection drain timeout in seconds")
    parser.add_argument("--ui_collect_use_vlm", choices=["on", "off"], default="on", help="Enable VLM for UI collection")
    parser.add_argument("--ui_collect_vlm_text_only", choices=["on", "off"], default="off", help="Use VLM text only in UI collection")
    parser.add_argument("--ui_collect_vlm_model", type=str, default="qwen/qwen3-vl-30b-a3b-instruct", help="UI collection VLM model")
    parser.add_argument("--ui_collect_base_url", type=str, default="", help="UI collection VLM base URL")
    parser.add_argument("--ui_collect_api_key", type=str, default="", help="UI collection VLM API key")
    parser.add_argument("--ui_collect_max_items", type=int, default=32, help="Max UI items collected per page")
    parser.add_argument("--ui_collect_max_vlm_calls", type=int, default=12, help="Max VLM calls for UI collection")
    parser.add_argument("--ui_collect_min_area", type=int, default=16, help="Minimum area for UI collection boxes")
    parser.add_argument("--page_load_wait_sec", type=float, default=1.5, help="Fixed post-action wait before load checks")
    parser.add_argument("--page_load_stable_max_polls", type=int, default=6, help="Max number of page stable polls")
    parser.add_argument("--bbox_iou_threshold", type=float, default=0.3, help="BBox refine IoU threshold")
    parser.add_argument("--bbox_center_dist_ratio", type=float, default=0.08, help="BBox refine center distance ratio")
    parser.add_argument("--bbox_area_ratio_min", type=float, default=0.5, help="BBox refine min area ratio")
    parser.add_argument("--bbox_area_ratio_max", type=float, default=2.0, help="BBox refine max area ratio")
    parser.add_argument("--popup_dismiss_max_attempts", type=int, default=2, help="Max popup dismiss attempts")
    add_feature_flag_args(parser)
    return parser.parse_args(argv)


def _validate_args(args: argparse.Namespace) -> None:
    if args.depth <= 0:
        raise ValueError("depth must be > 0")
    if args.breadth <= 0:
        raise ValueError("breadth must be > 0")
    if not args.openrouter_api_key:
        raise ValueError("Please provide OPENROUTER_API_KEY or --openrouter_api_key")
    if args.ui_collect_vlm_text_only == "on" and args.ui_collect_use_vlm != "on":
        raise ValueError("ui_collect_vlm_text_only=on requires ui_collect_use_vlm=on")
    if args.ui_collect_queue_size <= 0:
        raise ValueError("ui_collect_queue_size must be > 0")
    if args.ui_collect_drain_timeout_sec < 0:
        raise ValueError("ui_collect_drain_timeout_sec must be >= 0")


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


def _resolve_metrics_output_path(args: argparse.Namespace, data_dir: str) -> str:
    if args.metrics_output_path:
        return args.metrics_output_path
    return str(Path(data_dir) / "metrics.json")


def _init_device(args: argparse.Namespace):
    if args.device == "Android":
        return AndroidDevice(adb_endpoint=args.adb_endpoint or None)
    return HarmonyDevice()


def run(args: argparse.Namespace) -> Dict[str, Any]:
    _validate_args(args)
    _apply_runtime_overrides(args)

    data_dir = _resolve_data_dir(args)
    os.makedirs(data_dir, exist_ok=True)
    metrics_output_path = _resolve_metrics_output_path(args, data_dir)
    device = _init_device(args)
    feature_flags = FeatureFlags.from_namespace(args)
    metrics = MetricsCollector()
    runtime = RuntimeContext(features=feature_flags, metrics=metrics)

    explorer_base_url = args.openrouter_base_url
    explorer_api_key = args.openrouter_api_key
    ui_collect_base_url = args.ui_collect_base_url or explorer_base_url
    ui_collect_api_key = args.ui_collect_api_key or explorer_api_key

    decider_client = init_decider_client(args.service_ip, args.decider_port, args.decider_base_url, args.decider_api_key)
    explorer_client = init_explorer_client(explorer_base_url, explorer_api_key)
    explorer_disable_thinking = args.explorer_disable_thinking == "on"
    use_qwen3 = args.use_qwen3 == "on"
    allow_hierarchy_text_decider = args.allow_hierarchy_text_decider == "on"
    enable_ui_semantic_collect = args.enable_ui_semantic_collect == "on"
    ui_collect_async = args.ui_collect_async == "on"
    ui_collect_drain_on_exit = args.ui_collect_drain_on_exit == "on"
    logging.info("Explorer provider base_url=%s", explorer_base_url)
    logging.info("UI collect provider base_url=%s", ui_collect_base_url)
    logging.info("Experiment tag=%s", args.experiment_tag)
    logging.info("Feature flags=%s", feature_flags)

    logging.info("Starting app: %s", args.app_name)
    device.start_app(args.app_name)
    time.sleep(1.5)

    actions: List[Dict[str, Any]] = []
    reacts: List[Dict[str, Any]] = []
    step_counter = [0]
    path_counter = [0]
    partial_path_counter = [0]
    page_counter = [1]

    steps_dir = os.path.join(data_dir, "steps")
    paths_dir = os.path.join(data_dir, "paths")
    partial_paths_dir = os.path.join(data_dir, "partial_paths")
    ui_pages_dir = os.path.join(data_dir, "ui-pages")
    index_path = os.path.join(ui_pages_dir, "pages_index.json")
    os.makedirs(steps_dir, exist_ok=True)
    os.makedirs(paths_dir, exist_ok=True)
    os.makedirs(partial_paths_dir, exist_ok=True)
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
        explorer_cache = ExplorerCache(ttl_sec=300.0, metrics=metrics) if feature_flags.explorer_cache else None
        screen_cache = ScreenStateCache(staleness_sec=0.3, metrics=metrics) if feature_flags.screen_cache else None

        explore_dfs(
            app_name=args.app_name,
            depth_limit=args.depth,
            breadth=args.breadth,
            current_depth=0,
            decider_client=decider_client,
            decider_model=decider_model,
            explorer_client=explorer_client,
            explorer_model=args.explorer_model,
            explorer_disable_thinking=explorer_disable_thinking,
            device=device,
            device_type=args.device,
            use_qwen3=use_qwen3,
            allow_hierarchy_text_decider=allow_hierarchy_text_decider,
            data_dir=data_dir,
            actions=actions,
            reacts=reacts,
            step_counter=step_counter,
            path_counter=path_counter,
            partial_path_counter=partial_path_counter,
            page_counter=page_counter,
            steps_dir=steps_dir,
            paths_dir=paths_dir,
            partial_paths_dir=partial_paths_dir,
            enable_ui_semantic_collect=enable_ui_semantic_collect and ui_collect_async,
            ui_pages_dir=ui_pages_dir,
            page_registry=page_registry,
            collect_queue=collect_queue,
            queue_lock=queue_lock,
            index_lock=index_lock,
            index_path=index_path,
            ui_collect_async=ui_collect_async,
            ui_collect_queue_size=args.ui_collect_queue_size,
            runtime=runtime,
            visited_tasks=visited_tasks,
            explorer_cache=explorer_cache,
            screen_cache=screen_cache,
            popup_dismiss_max_attempts=args.popup_dismiss_max_attempts if feature_flags.popup_auto_dismiss else 0,
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

        flush_artifact_tasks(timeout=60.0)
        metrics.finish_run()
        metrics_payload = build_metrics_payload(
            metrics=metrics,
            experiment_tag=args.experiment_tag,
            data_dir=data_dir,
            feature_flags=feature_flags,
            configured_depth_limit=args.depth,
            configured_breadth=args.breadth,
        )
        write_metrics_payload(metrics_output_path, metrics_payload)
        logging.info("Auto-search finished. data_dir=%s metrics=%s", data_dir, metrics_output_path)

    return metrics_payload


def main() -> None:
    args = parse_args()
    run(args)


if __name__ == "__main__":
    main()
