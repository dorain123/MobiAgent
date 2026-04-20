import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
from typing import Any, Dict, Optional

from auto_explore.core.artifacts import get_current_screenshot_path
from auto_explore.core.fingerprints import (
    _compute_dhash_hex,
    _compute_fingerprints_concurrent,
    _hamming_distance_hex,
    _now_ts,
)


def _save_ui_page_index(
    index_path: str,
    page_registry: Dict[str, Dict[str, Any]],
    index_lock: threading.Lock,
) -> None:
    with index_lock:
        try:
            pages = sorted(
                list(page_registry.values()),
                key=lambda x: int(x.get("page_id", 0) or 0),
            )
            with open(index_path, "w", encoding="utf-8") as f:
                json.dump({"pages": pages}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.warning(f"Failed to save ui page index: {e}")


def _load_ui_page_index(index_path: str) -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(index_path):
        return {}
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            return {}
        pages = payload.get("pages", [])
        if not isinstance(pages, list):
            return {}
        registry: Dict[str, Dict[str, Any]] = {}
        for item in pages:
            if not isinstance(item, dict):
                continue
            fp = str(item.get("fingerprint", "")).strip()
            if not fp:
                continue
            registry[fp] = item
        return registry
    except Exception as e:
        logging.warning(f"Failed to load ui page index: {e}")
        return {}


def _next_page_id(page_registry: Dict[str, Dict[str, Any]]) -> int:
    max_id = 0
    for item in page_registry.values():
        try:
            max_id = max(max_id, int(item.get("page_id", 0) or 0))
        except Exception:
            continue
    return max_id + 1


def _save_collect_snapshot(
    *,
    page_dir: str,
    device_type: str,
    hierarchy_text: str,
) -> Dict[str, str]:
    snapshot_dir = os.path.join(page_dir, "snapshot")
    os.makedirs(snapshot_dir, exist_ok=True)
    screenshot_path = os.path.join(snapshot_dir, "input_screenshot.jpg")
    hierarchy_ext = "xml" if hierarchy_text.lstrip().startswith("<") else "json"
    hierarchy_path = os.path.join(snapshot_dir, f"input_hierarchy.{hierarchy_ext}")
    raw_src = get_current_screenshot_path(device_type)
    shutil.copy2(raw_src, screenshot_path)
    with open(hierarchy_path, "w", encoding="utf-8") as f:
        f.write(hierarchy_text)
    return {
        "screenshot_path": screenshot_path,
        "hierarchy_path": hierarchy_path,
    }


def enqueue_ui_collect_if_new(
    *,
    app_name: str,
    device_type: str,
    current_depth: int,
    hierarchy_text: str,
    ui_pages_dir: str,
    page_registry: Dict[str, Dict[str, Any]],
    page_counter: list[int],
    collect_queue: "queue.Queue[Dict[str, Any]]",
    queue_lock: threading.Lock,
    index_lock: threading.Lock,
    index_path: str,
    ui_collect_queue_size: int,
) -> Optional[str]:
    fp, struct_fp, _ = _compute_fingerprints_concurrent(hierarchy_text)
    if not fp:
        return None

    with queue_lock:
        existing = page_registry.get(fp)
        if existing:
            return existing.get("output_dir")

        temp_page_id = page_counter[0]
        page_counter[0] += 1
        temp_page_dir = os.path.join(ui_pages_dir, f"page_{temp_page_id:04d}")
        os.makedirs(temp_page_dir, exist_ok=True)
        try:
            snapshot = _save_collect_snapshot(
                page_dir=temp_page_dir,
                device_type=device_type,
                hierarchy_text=hierarchy_text,
            )
        except Exception as e:
            page_registry[fp] = {
                "page_id": temp_page_id,
                "fingerprint": fp,
                "output_dir": temp_page_dir,
                "status": "failed",
                "attempt_count": 0,
                "created_at": _now_ts(),
                "last_attempt_at": "",
                "last_error": f"snapshot_save_failed: {e}",
                "snapshot": {},
                "depth": current_depth,
                "dedupe_key": {"raw_fp": fp, "struct_fp": struct_fp, "dhash": ""},
            }
            _save_ui_page_index(index_path, page_registry, index_lock)
            return temp_page_dir

        dhash_hex = ""
        try:
            dhash_hex = _compute_dhash_hex(snapshot["screenshot_path"])
        except Exception:
            dhash_hex = ""

        dedup_target: Optional[Dict[str, Any]] = None
        for meta in page_registry.values():
            other_key = meta.get("dedupe_key") or {}
            other_struct = str(other_key.get("struct_fp", ""))
            other_hash = str(other_key.get("dhash", ""))
            if not other_struct or not other_hash:
                continue
            if struct_fp and struct_fp == other_struct and _hamming_distance_hex(dhash_hex, other_hash) <= 3:
                dedup_target = meta
                break

        if dedup_target is not None:
            page_registry[fp] = {
                "page_id": dedup_target.get("page_id"),
                "fingerprint": fp,
                "output_dir": dedup_target.get("output_dir"),
                "status": "skipped_dedup",
                "attempt_count": 0,
                "created_at": _now_ts(),
                "last_attempt_at": "",
                "last_error": "",
                "snapshot": snapshot,
                "depth": current_depth,
                "dedupe_key": {"raw_fp": fp, "struct_fp": struct_fp, "dhash": dhash_hex},
                "deduped_to_page_id": dedup_target.get("page_id"),
            }
            _save_ui_page_index(index_path, page_registry, index_lock)
            return str(dedup_target.get("output_dir", "")) or temp_page_dir

        page_id = temp_page_id
        page_dir = temp_page_dir

        page_registry[fp] = {
            "page_id": page_id,
            "fingerprint": fp,
            "output_dir": page_dir,
            "status": "queued",
            "attempt_count": 0,
            "created_at": _now_ts(),
            "last_attempt_at": "",
            "last_error": "",
            "snapshot": snapshot,
            "depth": current_depth,
            "dedupe_key": {"raw_fp": fp, "struct_fp": struct_fp, "dhash": dhash_hex},
        }
        _save_ui_page_index(index_path, page_registry, index_lock)

        task = {
            "fingerprint": fp,
            "app_name": app_name,
            "device_type": device_type,
            "page_dir": page_dir,
            "snapshot": snapshot,
        }
        try:
            collect_queue.put_nowait(task)
        except queue.Full:
            page_registry[fp]["status"] = "skipped_queue_full"
            page_registry[fp]["last_error"] = f"queue_full({ui_collect_queue_size})"
            _save_ui_page_index(index_path, page_registry, index_lock)
        return page_dir


def ui_collect_worker(
    *,
    collect_queue: "queue.Queue[Dict[str, Any]]",
    stop_event: threading.Event,
    page_registry: Dict[str, Dict[str, Any]],
    queue_lock: threading.Lock,
    index_lock: threading.Lock,
    index_path: str,
    ui_collect_use_vlm: bool,
    ui_collect_vlm_text_only: bool,
    ui_collect_model: str,
    ui_collect_max_items: int,
    ui_collect_max_vlm_calls: int,
    ui_collect_min_area: int,
    ui_collect_base_url: str,
    ui_collect_api_key: str,
) -> None:
    while not stop_event.is_set():
        try:
            task = collect_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        if task.get("type") == "stop":
            collect_queue.task_done()
            break

        fp = task.get("fingerprint", "")
        with queue_lock:
            meta = page_registry.get(fp)
            if meta is None:
                collect_queue.task_done()
                continue
            meta["status"] = "running"
            meta["attempt_count"] = int(meta.get("attempt_count", 0) or 0) + 1
            meta["last_attempt_at"] = _now_ts()
            meta["last_error"] = ""
            page_registry[fp] = meta
            _save_ui_page_index(index_path, page_registry, index_lock)

        cmd = [
            sys.executable,
            "-m",
            "collect.auto.ui_semantic_boxer",
            "--device",
            task["device_type"],
            "--app_name",
            task["app_name"],
            "--output_dir",
            task["page_dir"],
            "--input_screenshot_path",
            task["snapshot"]["screenshot_path"],
            "--input_hierarchy_path",
            task["snapshot"]["hierarchy_path"],
            "--use_vlm",
            "on" if ui_collect_use_vlm else "off",
            "--vlm_text_only",
            "on" if ui_collect_vlm_text_only else "off",
            "--vlm_model",
            ui_collect_model,
            "--base_url",
            ui_collect_base_url,
            "--api_key",
            ui_collect_api_key,
            "--max_vlm_calls",
            str(ui_collect_max_vlm_calls),
            "--max_items",
            str(ui_collect_max_items),
            "--min_area",
            str(ui_collect_min_area),
        ]

        status = "ok"
        err = ""
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                status = "failed"
                err = (result.stderr or result.stdout or "").strip()[:500]
        except Exception as e:
            status = "failed"
            err = f"collect_exception: {e}"

        with queue_lock:
            meta = page_registry.get(fp, {})
            meta["status"] = status
            meta["last_error"] = err
            page_registry[fp] = meta
            _save_ui_page_index(index_path, page_registry, index_lock)
        collect_queue.task_done()


def _mark_unfinished_collect_tasks(
    *,
    page_registry: Dict[str, Dict[str, Any]],
    queue_lock: threading.Lock,
    index_lock: threading.Lock,
    index_path: str,
) -> None:
    with queue_lock:
        for fp, meta in page_registry.items():
            status = str(meta.get("status", ""))
            if status == "queued":
                meta["status"] = "skipped_shutdown_timeout"
                meta["last_error"] = "shutdown_timeout_before_processed"
                page_registry[fp] = meta
        _save_ui_page_index(index_path, page_registry, index_lock)


__all__ = [
    "_load_ui_page_index",
    "_mark_unfinished_collect_tasks",
    "_next_page_id",
    "_save_collect_snapshot",
    "_save_ui_page_index",
    "enqueue_ui_collect_if_new",
    "ui_collect_worker",
]
