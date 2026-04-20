import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from openai import OpenAI
from PIL import Image

from auto_explore.adapters.device import (
    call_model_with_validation_retry,
    compute_swipe_positions,
    convert_qwen3_coordinates_to_absolute,
    get_screenshot,
    validate_decider_response,
)
from auto_explore.core import settings
from auto_explore.core.artifacts import (
    _run_annotation_safe,
    save_hierarchy,
    save_raw_screenshot,
)
from auto_explore.core.explorer import get_hierarchy_text
from auto_explore.core.fingerprints import _bbox_iou, _extract_bounds_from_hierarchy_text
from auto_explore.core.navigation import _get_current_screen_size, _task_mentions_swipe
from auto_explore.core.prompting import build_auto_decider_messages

try:
    from utils.parse_xml import parse_bounds
except Exception:
    parse_bounds = None


BBOX_REFINE_AREA_RATIO_MAX = settings.BBOX_REFINE_AREA_RATIO_MAX
BBOX_REFINE_AREA_RATIO_MIN = settings.BBOX_REFINE_AREA_RATIO_MIN
BBOX_REFINE_CENTER_DIST_RATIO = settings.BBOX_REFINE_CENTER_DIST_RATIO
BBOX_REFINE_IOU_THRESHOLD = settings.BBOX_REFINE_IOU_THRESHOLD
DEVICE_WAIT_TIME = settings.DEVICE_WAIT_TIME
WaitActionSkip = settings.WaitActionSkip
_ANNOTATION_EXECUTOR = settings._ANNOTATION_EXECUTOR


def _convert_bbox_to_qwen3_relative(bbox: List[int], img_w: int, img_h: int) -> List[int]:
    if img_w <= 0 or img_h <= 0:
        return bbox
    x1, y1, x2, y2 = bbox
    return [
        int(round(x1 / img_w * 1000)),
        int(round(y1 / img_h * 1000)),
        int(round(x2 / img_w * 1000)),
        int(round(y2 / img_h * 1000)),
    ]


def _extract_click_target_text(task_text: str) -> Optional[str]:
    if not task_text or "点击" not in task_text:
        return None
    match = re.search(
        r"点击.*?[\"\u201c\u201d\u300c\u300d]([^\"\u201c\u201d\u300c\u300d]+)[\"\u201c\u201d\u300c\u300d]",
        task_text,
    )
    if match:
        return match.group(1).strip()
    return None


def _extract_text_bounds_from_hierarchy_text(hierarchy_text: str, target_text: str) -> List[List[int]]:
    if not hierarchy_text or not target_text:
        return []
    target = target_text.strip()
    if not target:
        return []

    if hierarchy_text.lstrip().startswith("<"):
        try:
            import xml.etree.ElementTree as ET

            root = ET.fromstring(hierarchy_text)
        except Exception:
            return []

        bounds_list: List[List[int]] = []
        for node in root.iter():
            node_text = node.get("text") or ""
            node_desc = node.get("content-desc") or node.get("contentDescription") or ""
            if target in node_text or target in node_desc:
                bounds_str = node.get("bounds")
                bounds = parse_bounds(bounds_str) if parse_bounds else None
                if bounds:
                    bounds_list.append(bounds)
        return bounds_list

    try:
        obj = json.loads(hierarchy_text)
    except Exception:
        return []

    bounds_list: List[List[int]] = []
    text_keys = {"text", "label", "name", "title", "contentDescription", "content-desc", "desc"}

    def _coerce_bounds(value: Any) -> Optional[List[int]]:
        if isinstance(value, (list, tuple)) and len(value) == 4:
            try:
                return [int(v) for v in value]
            except Exception:
                return None
        if isinstance(value, dict):
            keys = {"left", "top", "right", "bottom"}
            if keys.issubset(value.keys()):
                try:
                    return [int(value["left"]), int(value["top"]), int(value["right"]), int(value["bottom"])]
                except Exception:
                    return None
        if isinstance(value, str) and parse_bounds:
            return parse_bounds(value)
        return None

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            matched = False
            for key, val in node.items():
                if key in text_keys and isinstance(val, str) and target in val:
                    matched = True
                    break
            if matched:
                bounds = _coerce_bounds(node.get("bounds"))
                if bounds:
                    bounds_list.append(bounds)
            for val in node.values():
                _walk(val)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(obj)
    return bounds_list


def refine_bbox_with_hierarchy(
    hierarchy_text: str,
    bbox: List[int],
    img_w: int,
    img_h: int,
) -> List[int]:
    bounds_list = _extract_bounds_from_hierarchy_text(hierarchy_text)
    green = "\033[92m"
    red = "\033[91m"
    reset = "\033[0m"
    if not bounds_list:
        logging.info(f"{red}BBox not refined (no hierarchy bounds found).{reset}")
        return bbox

    best_bbox = bbox
    best_iou = 0.0
    bx1, by1, bx2, by2 = bbox
    bcx = (bx1 + bx2) / 2
    bcy = (by1 + by2) / 2
    b_area = max(1, (bx2 - bx1) * (by2 - by1))
    max_center_dist = (img_w**2 + img_h**2) ** 0.5 * settings.BBOX_REFINE_CENTER_DIST_RATIO

    for cand in bounds_list:
        iou = _bbox_iou(bbox, cand)
        if iou > best_iou:
            best_iou = iou
            best_bbox = cand

    if best_iou >= settings.BBOX_REFINE_IOU_THRESHOLD:
        logging.info(f"{green}BBox refined by IoU (iou={best_iou:.3f}) from {bbox} to {best_bbox}.{reset}")
        return best_bbox

    cx1, cy1, cx2, cy2 = best_bbox
    ccx = (cx1 + cx2) / 2
    ccy = (cy1 + cy2) / 2
    center_dist = ((ccx - bcx) ** 2 + (ccy - bcy) ** 2) ** 0.5
    c_area = max(1, (cx2 - cx1) * (cy2 - cy1))
    area_ratio = c_area / b_area if b_area else 1.0
    if center_dist <= max_center_dist and settings.BBOX_REFINE_AREA_RATIO_MIN <= area_ratio <= settings.BBOX_REFINE_AREA_RATIO_MAX:
        logging.info(f"{green}BBox refined by center/area from {bbox} to {best_bbox}.{reset}")
        return best_bbox

    logging.info(f"{red}BBox not refined (no close match).{reset}")
    return bbox


def execute_decider_one_step(
    decider_client: OpenAI,
    decider_model: str,
    device,
    device_type: str,
    app_name: str,
    step_task: str,
    use_qwen3: bool,
    allow_hierarchy_text_decider: bool,
    output_dir: str,
    step_index: int,
) -> Dict[str, Any]:
    """使用 e2e decider 执行一个单步任务并记录数据。"""
    pre_action_hierarchy_text = get_hierarchy_text(device)
    target_text = _extract_click_target_text(step_task)
    bounds_from_text = (
        _extract_text_bounds_from_hierarchy_text(pre_action_hierarchy_text, target_text) if target_text else []
    )

    decider_resp: Optional[Dict[str, Any]] = None
    if allow_hierarchy_text_decider and target_text and bounds_from_text:
        green = "\033[92m"
        reset = "\033[0m"
        logging.info(f"{green}Using hierarchy_text_UI bbox for click target: {target_text}{reset}")
        best_bbox = min(bounds_from_text, key=lambda b: max(1, (b[2] - b[0]) * (b[3] - b[1])))
        if use_qwen3:
            size = _get_current_screen_size(device_type)
            if size:
                img_w, img_h = size
                best_bbox = _convert_bbox_to_qwen3_relative(best_bbox, img_w, img_h)
        decider_resp = {
            "action": "click",
            "parameters": {"bbox": best_bbox},
            "reasoning": f"观察到屏幕上存在{target_text}文字，直接点击{target_text}文本按钮",
        }
    else:
        red = "\033[91m"
        reset = "\033[0m"
        logging.info(f"{red}Using model output bbox (decider).{reset}")
        screenshot_b64 = get_screenshot(device, device_type)
        messages = build_auto_decider_messages(
            task=f"当前处在{app_name}，请帮我{step_task}",
            history=[],
            screenshot_b64=screenshot_b64,
        )

        def _validator(resp: Dict[str, Any]) -> None:
            validate_decider_response(resp, use_e2e=True)

        for attempt in range(3):
            decider_resp = call_model_with_validation_retry(
                decider_client,
                decider_model,
                messages,
                validator_func=_validator,
                max_retries=5,
                max_tokens=256,
                context="Decider",
            )
            action = decider_resp.get("action")
            if action == "done":
                if attempt < 2:
                    logging.warning(
                        "Decider returned done in auto-search (attempt=%s), retrying.",
                        attempt + 1,
                    )
                    time.sleep(0.6)
                    continue
                raise RuntimeError("Decider action mismatch: auto-search does not accept done action")
            if _task_mentions_swipe(step_task) and action == "click":
                if attempt < 2:
                    logging.warning(
                        "Decider action mismatch (attempt=%s): task expects swipe but got click. Retrying.",
                        attempt + 1,
                    )
                    time.sleep(0.6)
                    continue
                color = "\033[91m"
                reset = "\033[0m"
                logging.error(
                    f"{color}Decider action mismatch after retries: task expects swipe but got click. "
                    f"Skipping task: {step_task}{reset}"
                )
                raise RuntimeError("Decider action mismatch: swipe task returned click")
            break

    if decider_resp is None:
        raise RuntimeError("Decider response is empty")

    screenshot_file = save_raw_screenshot(output_dir, step_index, device_type)
    save_hierarchy(device, device_type, output_dir, step_index)

    react_item = {
        "action_index": step_index,
        "source_task": step_task,
        "reasoning": decider_resp.get("reasoning", ""),
        "function": {
            "name": decider_resp.get("action", ""),
            "parameters": decider_resp.get("parameters", {}),
        },
    }

    action = decider_resp["action"]
    color = "\033[96m"
    reset = "\033[0m"
    logging.info(f"{color}Decider task: {step_task} -> action: {action}{reset}")
    params = decider_resp.get("parameters", {})

    img = Image.open(screenshot_file)
    img_w, img_h = img.size

    action_record: Dict[str, Any] = {
        "action_index": step_index,
        "source_task": step_task,
        "type": action,
    }

    if action == "click":
        bbox = params.get("bbox")
        if use_qwen3:
            bbox = convert_qwen3_coordinates_to_absolute(bbox, img_w, img_h, is_bbox=True)
        if bbox:
            bbox = refine_bbox_with_hierarchy(pre_action_hierarchy_text, bbox, img_w, img_h)
        x1, y1, x2, y2 = bbox
        x, y = (x1 + x2) // 2, (y1 + y2) // 2
        device.click(x, y)
        action_record.update({"position_x": x, "position_y": y, "bounds": [x1, y1, x2, y2]})
    elif action == "click_input":
        bbox = params.get("bbox")
        text = params.get("text", "")
        if use_qwen3:
            bbox = convert_qwen3_coordinates_to_absolute(bbox, img_w, img_h, is_bbox=True)
        if bbox:
            bbox = refine_bbox_with_hierarchy(pre_action_hierarchy_text, bbox, img_w, img_h)
        x1, y1, x2, y2 = bbox
        x, y = (x1 + x2) // 2, (y1 + y2) // 2
        device.click(x, y)
        device.input(text)
        action_record.update({"position_x": x, "position_y": y, "bounds": [x1, y1, x2, y2], "text": text})
    elif action == "input":
        text = params.get("text", "")
        device.input(text)
        action_record["text"] = text
    elif action == "swipe":
        direction = str(params.get("direction", "UP")).upper()
        start_coords = params.get("start_coords")
        end_coords = params.get("end_coords")
        if start_coords and end_coords:
            if use_qwen3:
                start_coords = convert_qwen3_coordinates_to_absolute(start_coords, img_w, img_h, is_bbox=False)
                end_coords = convert_qwen3_coordinates_to_absolute(end_coords, img_w, img_h, is_bbox=False)
            sx, sy = start_coords
            ex, ey = end_coords
        else:
            sx, sy, ex, ey = compute_swipe_positions(direction, img_w, img_h)

        device.swipe_with_coords(sx, sy, ex, ey)
        action_record.update(
            {
                "direction": direction.lower(),
                "press_position_x": sx,
                "press_position_y": sy,
                "release_position_x": ex,
                "release_position_y": ey,
            }
        )
    elif action == "wait":
        logging.info("Decider returned 'wait' — sleeping 2s and skipping step.")
        time.sleep(2.0)
        raise WaitActionSkip("wait action — step not recorded")
    elif action == "done":
        action_record["status"] = params.get("status", "success")
    else:
        raise ValueError(f"Unsupported action from decider: {action}")

    _ANNOTATION_EXECUTOR.submit(_run_annotation_safe, action, action_record, screenshot_file, output_dir, step_index)

    time.sleep(settings.DEVICE_WAIT_TIME)
    post_hierarchy_text = get_hierarchy_text(device)

    return {
        "decider_response": decider_resp,
        "action_record": action_record,
        "react_item": react_item,
        "screenshot_file": screenshot_file,
        "post_hierarchy_text": post_hierarchy_text,
    }


__all__ = [
    "BBOX_REFINE_AREA_RATIO_MAX",
    "BBOX_REFINE_AREA_RATIO_MIN",
    "BBOX_REFINE_CENTER_DIST_RATIO",
    "BBOX_REFINE_IOU_THRESHOLD",
    "DEVICE_WAIT_TIME",
    "WaitActionSkip",
    "_convert_bbox_to_qwen3_relative",
    "_extract_click_target_text",
    "_extract_text_bounds_from_hierarchy_text",
    "execute_decider_one_step",
    "refine_bbox_with_hierarchy",
]
