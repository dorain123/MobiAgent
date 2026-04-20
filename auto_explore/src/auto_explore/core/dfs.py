import difflib
import logging
import os
import queue
import shutil
import threading
import time
from typing import Dict, List, Optional

from openai import OpenAI

from auto_explore.adapters.device import convert_qwen3_coordinates_to_absolute, get_screenshot
from auto_explore.core import settings
from auto_explore.core.artifacts import (
    _ANNOTATION_EXECUTOR,
    _compute_task_description,
    _persist_outputs_safe,
    _persist_step_output_safe,
    copy_step_artifacts_to_path,
    save_hierarchy,
    save_raw_screenshot,
)
from auto_explore.core.decider import WaitActionSkip, execute_decider_one_step
from auto_explore.core.explorer import (
    ExplorerCache,
    ScreenStateCache,
    _capture_screen,
    _compute_adaptive_similarity_threshold,
    _deduplicate_candidates,
    call_explorer_model,
    get_hierarchy_text,
)
from auto_explore.core.fingerprints import (
    _compute_fingerprints_concurrent,
    _detect_countdown,
    _hierarchy_fingerprint,
    _normalize_hierarchy_text,
    _triple_verify,
)
from auto_explore.core.navigation import (
    _find_dismissible_element,
    _get_current_screen_size,
    _is_app_in_foreground,
    navigate_back,
    perform_backtrack_action,
    replay_action_record,
)
from auto_explore.core.prompting import append_done_to_path
from auto_explore.core.ui_collect import enqueue_ui_collect_if_new


def explore_dfs(
    *,
    app_name: str,
    depth_limit: int,
    breadth: int,
    current_depth: int,
    decider_client: OpenAI,
    decider_model: str,
    explorer_client: OpenAI,
    explorer_model: str,
    device,
    device_type: str,
    use_qwen3: bool,
    allow_hierarchy_text_decider: bool,
    data_dir: str,
    actions: List[Dict[str, object]],
    reacts: List[Dict[str, object]],
    step_counter: List[int],
    path_counter: List[int],
    page_counter: List[int],
    steps_dir: str,
    paths_dir: str,
    enable_ui_semantic_collect: bool,
    ui_pages_dir: str,
    page_registry: Dict[str, Dict[str, object]],
    collect_queue: Optional["queue.Queue[Dict[str, object]]"],
    queue_lock: Optional[threading.Lock],
    index_lock: Optional[threading.Lock],
    index_path: Optional[str],
    ui_collect_async: bool,
    ui_collect_queue_size: int,
    path_actions: Optional[List[Dict[str, object]]] = None,
    path_reacts: Optional[List[Dict[str, object]]] = None,
    visited_tasks: Optional[Dict[str, set]] = None,
    explorer_cache: Optional["ExplorerCache"] = None,
    screen_cache: Optional["ScreenStateCache"] = None,
    popup_dismiss_max_attempts: int = 2,
) -> None:
    """DFS探索：每层挑选H个候选，逐个执行并回溯。"""
    if current_depth >= depth_limit:
        return

    screenshot_b64, hierarchy_text = _capture_screen(device, device_type, screen_cache)

    _do_collect = (
        enable_ui_semantic_collect
        and collect_queue is not None
        and queue_lock is not None
        and index_lock is not None
        and bool(index_path)
    )

    if _do_collect:
        assert collect_queue is not None and queue_lock is not None and index_lock is not None and index_path
        enqueue_ui_collect_if_new(
            app_name=app_name,
            device_type=device_type,
            current_depth=current_depth,
            hierarchy_text=hierarchy_text,
            ui_pages_dir=ui_pages_dir,
            page_registry=page_registry,
            page_counter=page_counter,
            collect_queue=collect_queue,
            queue_lock=queue_lock,
            index_lock=index_lock,
            index_path=index_path,
            ui_collect_queue_size=ui_collect_queue_size,
        )

    for rule_attempt in range(popup_dismiss_max_attempts):
        dismiss_bounds = _find_dismissible_element(hierarchy_text)
        if dismiss_bounds is None:
            break
        x = (dismiss_bounds[0] + dismiss_bounds[2]) // 2
        y = (dismiss_bounds[1] + dismiss_bounds[3]) // 2
        device.click(x, y)
        time.sleep(settings.DEVICE_WAIT_TIME)
        screenshot_b64, hierarchy_text = _capture_screen(device, device_type, screen_cache)
        logging.info("\033[93m[RuleDismiss] clicked (%d,%d) attempt %d\033[0m", x, y, rule_attempt + 1)

    action_history = list(path_actions) if path_actions else list(actions)

    if visited_tasks is None:
        visited_tasks = {}
    current_page_fp, struct_fp, _ = _compute_fingerprints_concurrent(hierarchy_text)
    already_explored_list = list(visited_tasks.get(current_page_fp, set()))

    cached_candidates = None
    if explorer_cache is not None:
        cached_candidates = explorer_cache.get(struct_fp, current_depth, breadth, already_explored_list)
    popup_info: Optional[Dict[str, object]] = None
    if cached_candidates is not None:
        candidates = [
            c for c in cached_candidates if c.get("single_step_task", "") not in visited_tasks.get(current_page_fp, set())
        ]
        logging.info(f"Depth={current_depth}, Explorer cache hit, {len(candidates)} candidates after filter")
    else:
        candidates, popup_info = call_explorer_model(
            explorer_client,
            explorer_model,
            screenshot_b64,
            hierarchy_text,
            current_depth,
            breadth,
            action_history,
            already_explored=already_explored_list,
        )
        if explorer_cache is not None:
            explorer_cache.put(struct_fp, current_depth, breadth, already_explored_list, candidates)

    for popup_attempt in range(popup_dismiss_max_attempts):
        if not (popup_info and popup_info.get("detected")):
            break
        close_pt = popup_info.get("close_point")
        if close_pt:
            size = _get_current_screen_size(device_type)
            if size:
                img_w, img_h = size
                if use_qwen3:
                    abs_pt = convert_qwen3_coordinates_to_absolute(close_pt, img_w, img_h, is_bbox=False)
                else:
                    abs_pt = close_pt
                device.click(int(abs_pt[0]), int(abs_pt[1]))
                logging.info(
                    f"\033[93m[PopupDismiss] clicked close at {abs_pt} (attempt {popup_attempt + 1})\033[0m"
                )
            else:
                navigate_back(device, device_type)
                logging.info(
                    f"\033[93m[PopupDismiss] no screen size, pressed back (attempt {popup_attempt + 1})\033[0m"
                )
        else:
            h1 = hierarchy_text
            time.sleep(1.0)
            if screen_cache is not None:
                _, h2 = screen_cache.capture(device, device_type, force=True)
            else:
                h2 = get_hierarchy_text(device)
            remaining = _detect_countdown(h1, h2)
            if remaining is not None:
                logging.info(
                    "\033[93m[PopupDismiss] Countdown ad detected, waiting %ds... (attempt %d)\033[0m",
                    remaining,
                    popup_attempt + 1,
                )
                time.sleep(remaining + 0.5)
            else:
                navigate_back(device, device_type)
                logging.info(
                    "\033[93m[PopupDismiss] no close_point, no countdown → pressed back (attempt %d)\033[0m",
                    popup_attempt + 1,
                )
        time.sleep(settings.DEVICE_WAIT_TIME)
        screenshot_b64, hierarchy_text = _capture_screen(device, device_type, screen_cache)
        candidates, popup_info = call_explorer_model(
            explorer_client,
            explorer_model,
            screenshot_b64,
            hierarchy_text,
            current_depth,
            breadth,
            action_history,
            already_explored=already_explored_list,
        )

    candidates = _deduplicate_candidates(candidates, already_explored=already_explored_list)
    base_hierarchy_text = hierarchy_text

    logging.info(f"Depth={current_depth}, got {len(candidates)} candidates")
    for cand in candidates:
        color = "\033[96m"
        reset = "\033[0m"
        logging.info(
            f"{color}[Depth {current_depth}] candidate rank={cand.get('rank')} "
            f"task={cand.get('single_step_task')} reason={cand.get('reason')}{reset}"
        )

    cand_idx = 0
    while cand_idx < len(candidates):
        if cand_idx > 0:
            current_hierarchy_text = get_hierarchy_text(device)
            similarity = difflib.SequenceMatcher(
                None,
                _normalize_hierarchy_text(base_hierarchy_text),
                _normalize_hierarchy_text(current_hierarchy_text),
            ).ratio()
            page_change_threshold = _compute_adaptive_similarity_threshold(base_hierarchy_text)
            if similarity < page_change_threshold:
                remaining = max(breadth - cand_idx, 0)
                if remaining == 0:
                    break
                color = "\033[93m"
                reset = "\033[0m"
                logging.info(
                    f"{color}Page changed (similarity={similarity:.3f} < threshold={page_change_threshold:.3f}). "
                    f"Regenerating {remaining} candidates.{reset}"
                )
                screenshot_b64 = get_screenshot(device, device_type)
                base_hierarchy_text = current_hierarchy_text
                if _do_collect:
                    assert collect_queue is not None and queue_lock is not None and index_lock is not None and index_path
                    enqueue_ui_collect_if_new(
                        app_name=app_name,
                        device_type=device_type,
                        current_depth=current_depth,
                        hierarchy_text=current_hierarchy_text,
                        ui_pages_dir=ui_pages_dir,
                        page_registry=page_registry,
                        page_counter=page_counter,
                        collect_queue=collect_queue,
                        queue_lock=queue_lock,
                        index_lock=index_lock,
                        index_path=index_path,
                        ui_collect_queue_size=ui_collect_queue_size,
                    )
                new_page_fp = _hierarchy_fingerprint(base_hierarchy_text)
                new_already_explored = list(visited_tasks.get(new_page_fp, set()))
                new_candidates, _ = call_explorer_model(
                    explorer_client,
                    explorer_model,
                    screenshot_b64,
                    base_hierarchy_text,
                    current_depth,
                    remaining,
                    list(path_actions) if path_actions else list(actions),
                    already_explored=new_already_explored,
                )
                new_candidates = _deduplicate_candidates(new_candidates, already_explored=new_already_explored)
                candidates = candidates[:cand_idx] + new_candidates

        cand = candidates[cand_idx]
        task = cand["single_step_task"]
        step_counter[0] += 1
        step_idx = step_counter[0]
        step_output_dir = os.path.join(steps_dir, f"step_{step_idx:04d}")
        os.makedirs(step_output_dir, exist_ok=True)

        current_path_actions = list(path_actions) if path_actions else []
        current_path_reacts = list(path_reacts) if path_reacts else []

        cyan = "\033[96m"
        reset = "\033[0m"
        logging.info(
            f"{cyan}[Depth {current_depth}] execute rank={cand.get('rank')} task={task} "
            f"reason={cand.get('reason')}{reset}"
        )

        action_record = None
        pre_hierarchy_text = get_hierarchy_text(device)
        get_screenshot(device, device_type)
        pre_screenshot_path = "screenshot-Android.jpg" if device_type == "Android" else "screenshot-Harmony.jpg"
        _, pre_struct_fp, pre_dhash = _compute_fingerprints_concurrent(pre_hierarchy_text, screenshot_path=pre_screenshot_path)
        try:
            step_result = execute_decider_one_step(
                decider_client=decider_client,
                decider_model=decider_model,
                device=device,
                device_type=device_type,
                app_name=app_name,
                step_task=task,
                use_qwen3=use_qwen3,
                allow_hierarchy_text_decider=allow_hierarchy_text_decider,
                output_dir=step_output_dir,
                step_index=step_idx,
            )

            action_record = step_result["action_record"]
            react_item = step_result["react_item"]
            post_hierarchy_text = step_result.get("post_hierarchy_text", "")
            if _do_collect:
                assert collect_queue is not None and queue_lock is not None and index_lock is not None and index_path
                _ = get_screenshot(device, device_type)
                enqueue_ui_collect_if_new(
                    app_name=app_name,
                    device_type=device_type,
                    current_depth=current_depth + 1,
                    hierarchy_text=post_hierarchy_text,
                    ui_pages_dir=ui_pages_dir,
                    page_registry=page_registry,
                    page_counter=page_counter,
                    collect_queue=collect_queue,
                    queue_lock=queue_lock,
                    index_lock=index_lock,
                    index_path=index_path,
                    ui_collect_queue_size=ui_collect_queue_size,
                )

            actions.append(action_record)
            reacts.append(react_item)
            current_path_actions.append(action_record)
            current_path_reacts.append(react_item)

            if current_page_fp not in visited_tasks:
                visited_tasks[current_page_fp] = set()
            visited_tasks[current_page_fp].add(task)

            if screen_cache is not None:
                screen_cache.invalidate()

            _ANNOTATION_EXECUTOR.submit(
                _persist_step_output_safe,
                step_output_dir,
                app_name,
                dict(action_record),
                dict(react_item),
            )

            if current_depth + 1 >= depth_limit:
                path_counter[0] += 1
                path_id = path_counter[0]
                path_output_dir = os.path.join(paths_dir, f"path_{path_id:04d}")
                step_indices = [item.get("action_index") for item in current_path_actions if item.get("action_index")]
                copy_step_artifacts_to_path(steps_dir, path_output_dir, step_indices)
                path_task_description = _compute_task_description(actions=current_path_actions, app_name=app_name)
                full_path_actions, full_path_reacts = append_done_to_path(
                    current_path_actions,
                    current_path_reacts,
                    path_task_description,
                )
                done_index = len(full_path_actions)
                try:
                    get_screenshot(device, device_type)
                    save_raw_screenshot(path_output_dir, done_index, device_type)
                    save_hierarchy(device, device_type, path_output_dir, done_index)
                except Exception as e:
                    logging.warning(f"Failed to save done artifacts for path {path_id:04d}: {e}")
                _ANNOTATION_EXECUTOR.submit(
                    _persist_outputs_safe,
                    path_output_dir,
                    app_name,
                    list(full_path_actions),
                    list(full_path_reacts),
                )
            else:
                explore_dfs(
                    app_name=app_name,
                    depth_limit=depth_limit,
                    breadth=breadth,
                    current_depth=current_depth + 1,
                    decider_client=decider_client,
                    decider_model=decider_model,
                    explorer_client=explorer_client,
                    explorer_model=explorer_model,
                    device=device,
                    device_type=device_type,
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
                    enable_ui_semantic_collect=enable_ui_semantic_collect,
                    ui_pages_dir=ui_pages_dir,
                    page_registry=page_registry,
                    collect_queue=collect_queue,
                    queue_lock=queue_lock,
                    index_lock=index_lock,
                    index_path=index_path,
                    ui_collect_async=ui_collect_async,
                    ui_collect_queue_size=ui_collect_queue_size,
                    path_actions=current_path_actions,
                    path_reacts=current_path_reacts,
                    visited_tasks=visited_tasks,
                    explorer_cache=explorer_cache,
                    screen_cache=screen_cache,
                    popup_dismiss_max_attempts=popup_dismiss_max_attempts,
                )

        except WaitActionSkip:
            step_counter[0] -= 1
            try:
                shutil.rmtree(step_output_dir)
            except Exception:
                pass
            logging.info(
                "\033[93m[Depth %d] Wait action — candidate '%s' skipped, not recorded.\033[0m",
                current_depth,
                task,
            )
            cand_idx += 1
            continue
        except Exception as e:
            logging.error(f"Failed to execute candidate at depth {current_depth}: {e}")

        app_was_restarted = False
        perform_backtrack_action(device, device_type, action_record)

        post_back_hierarchy = get_hierarchy_text(device)
        if not _is_app_in_foreground(device, device_type, app_name, post_back_hierarchy):
            logging.warning("\033[91mBacktrack left app unexpectedly. Restarting app: %s\033[0m", app_name)
            device.start_app(app_name)
            time.sleep(settings.DEVICE_WAIT_TIME * 2)
            post_back_hierarchy = get_hierarchy_text(device)
            app_was_restarted = True

        post_back_screenshot_path = "screenshot-Android.jpg" if device_type == "Android" else "screenshot-Harmony.jpg"
        get_screenshot(device, device_type)

        if app_was_restarted:
            verified = _is_app_in_foreground(device, device_type, app_name, post_back_hierarchy)
            if verified:
                logging.info("\033[92mBacktrack verified via app restart (dynamic content expected to differ).\033[0m")
            else:
                logging.warning("\033[91mApp did not return to foreground after restart.\033[0m")
        else:
            verified = _triple_verify(
                pre_hierarchy_text,
                pre_struct_fp,
                pre_dhash,
                post_back_hierarchy,
                post_back_screenshot_path,
            )

        if not verified:
            logging.warning("\033[93mBacktrack verification failed. Attempting full path replay.\033[0m")
            recovered = False
            for attempt in range(2):
                device.start_app(app_name)
                time.sleep(settings.DEVICE_WAIT_TIME * 2)
                replay_ok = True
                for replay_act in current_path_actions[:-1]:
                    if not replay_action_record(device, replay_act):
                        replay_ok = False
                        break
                    time.sleep(settings.DEVICE_WAIT_TIME)
                if not replay_ok:
                    logging.warning("\033[93mPath replay action failed on attempt %d.\033[0m", attempt + 1)
                    continue
                replay_hierarchy = get_hierarchy_text(device)
                get_screenshot(device, device_type)
                if not current_path_actions[:-1]:
                    replay_recovered = _is_app_in_foreground(device, device_type, app_name, replay_hierarchy)
                    if replay_recovered:
                        recovered = True
                        logging.info("\033[92mFull path replay recovery succeeded (first step, app in foreground).\033[0m")
                        break
                    continue
                if _triple_verify(pre_hierarchy_text, pre_struct_fp, pre_dhash, replay_hierarchy, post_back_screenshot_path):
                    recovered = True
                    logging.info("\033[92mFull path replay recovery succeeded on attempt %d.\033[0m", attempt + 1)
                    break
            if not recovered:
                logging.error(
                    "\033[91mBacktrack recovery failed after full path replay. Skipping remaining candidates at this depth.\033[0m"
                )
                break

        cand_idx += 1


def init_decider_client(service_ip: str, decider_port: int, base_url: str = "", api_key: str = "") -> OpenAI:
    if base_url:
        return OpenAI(api_key=api_key or "0", base_url=base_url)
    return OpenAI(api_key="0", base_url=f"http://{service_ip}:{decider_port}/v1")


def init_explorer_client(base_url: str, api_key: str) -> OpenAI:
    logging.info(f"Initializing explorer client with base_url={base_url}")
    logging.info(f"API Key is set: {bool(api_key)}")
    return OpenAI(api_key=api_key, base_url=base_url)


__all__ = ["explore_dfs", "init_decider_client", "init_explorer_client"]
