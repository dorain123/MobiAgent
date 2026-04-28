import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Sequence

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
    submit_artifact_task,
)
from auto_explore.core.explorer import get_hierarchy_text
from auto_explore.core.fingerprints import _bbox_iou, _extract_bounds_from_hierarchy_text
from auto_explore.core.navigation import (
    _choose_tab_snapshot_for_action,
    _extract_selected_tab_snapshots,
    _get_current_screen_size,
    _task_mentions_swipe,
)
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

_DECIDER_ALLOWED_ACTIONS = {"click", "click_input", "input", "swipe", "wait", "done"}
_SEARCH_KEYWORDS = ("search", "\u641c\u7d22", "\u641c\u7d22\u6846", "\u67e5\u627e")
_INPUT_KEYWORDS = ("input", "\u8f93\u5165", "\u8f93\u5165\u6846", "edittext", "editable")
_NON_TARGET_KEYWORDS = (
    "close",
    "dismiss",
    "cancel",
    "back",
    "\u5173\u95ed",
    "\u53d6\u6d88",
    "\u8fd4\u56de",
    "\u8bbe\u7f6e",
)


class DeciderTargetMismatch(RuntimeError):
    """Raised when the decider action clearly points at a different UI target."""


class InputFailed(RuntimeError):
    """Raised when text input cannot be verified after retries."""


def _raise_decider_schema_error(kind: str, detail: str) -> None:
    message = f"Decider invalid {kind}: {detail}"
    logging.error(message)
    raise ValueError(message)


def _coerce_int_strict(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        _raise_decider_schema_error(field_name, f"boolean is not allowed: {value}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and float(value).is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if re.fullmatch(r"[-+]?\d+", stripped):
            return int(stripped)
    _raise_decider_schema_error(field_name, f"expected integer, got {value!r}")


def _sanitize_int_sequence(
    values: Any,
    expected_len: int,
    field_name: str,
) -> List[int]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        _raise_decider_schema_error(field_name, f"expected length-{expected_len} list, got {type(values).__name__}")
    if len(values) != expected_len:
        _raise_decider_schema_error(field_name, f"expected length {expected_len}, got {len(values)}")
    return [_coerce_int_strict(value, field_name) for value in values]


def sanitize_decider_response(response: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(response, dict):
        _raise_decider_schema_error("action schema", f"response must be dict, got {type(response).__name__}")

    action = response.get("action")
    if action not in _DECIDER_ALLOWED_ACTIONS:
        _raise_decider_schema_error("action schema", f"unsupported action {action!r}")

    parameters = response.get("parameters")
    if not isinstance(parameters, dict):
        _raise_decider_schema_error("action schema", "'parameters' must be a dict")

    sanitized = dict(response)
    sanitized_params = dict(parameters)

    if action in {"click", "click_input"}:
        sanitized_params["bbox"] = _sanitize_int_sequence(parameters.get("bbox"), 4, "bbox")

    if action == "click_input":
        text = parameters.get("text")
        if not isinstance(text, str) or not text.strip():
            _raise_decider_schema_error("action schema", "click_input requires non-empty 'text'")
        sanitized_params["text"] = text

    if action == "input":
        text = parameters.get("text")
        if not isinstance(text, str) or not text.strip():
            _raise_decider_schema_error("action schema", "input requires non-empty 'text'")
        sanitized_params["text"] = text

    if action == "swipe":
        has_start = "start_coords" in parameters
        has_end = "end_coords" in parameters
        if has_start or has_end:
            if not (has_start and has_end):
                _raise_decider_schema_error(
                    "coords",
                    "swipe requires both 'start_coords' and 'end_coords' when explicit coordinates are provided",
                )
            sanitized_params["start_coords"] = _sanitize_int_sequence(parameters.get("start_coords"), 2, "coords")
            sanitized_params["end_coords"] = _sanitize_int_sequence(parameters.get("end_coords"), 2, "coords")

    sanitized["parameters"] = sanitized_params
    return sanitized


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


def _bbox_valid_shape(bbox: List[int]) -> bool:
    if len(bbox) != 4:
        return False
    x1, y1, x2, y2 = bbox
    return x2 > x1 and y2 > y1


def _bbox_inside_screen(bbox: List[int], img_w: int, img_h: int) -> bool:
    if not _bbox_valid_shape(bbox):
        return False
    x1, y1, x2, y2 = bbox
    return 0 <= x1 < img_w and 0 < x2 <= img_w and 0 <= y1 < img_h and 0 < y2 <= img_h


def _clip_bbox_to_screen(bbox: List[int], img_w: int, img_h: int) -> Optional[List[int]]:
    if len(bbox) != 4:
        return None
    x1, y1, x2, y2 = bbox
    clipped = [
        max(0, min(img_w, int(x1))),
        max(0, min(img_h, int(y1))),
        max(0, min(img_w, int(x2))),
        max(0, min(img_h, int(y2))),
    ]
    return clipped if _bbox_valid_shape(clipped) else None


def _dedupe_bboxes(candidates: List[List[int]]) -> List[List[int]]:
    seen = set()
    result: List[List[int]] = []
    for bbox in candidates:
        key = tuple(int(v) for v in bbox)
        if key in seen:
            continue
        seen.add(key)
        result.append(list(key))
    return result


def _candidate_absolute_bboxes(raw_bbox: List[int], img_w: int, img_h: int, use_qwen3: bool) -> List[List[int]]:
    """Generate plausible absolute-coordinate bboxes from model output."""
    candidates: List[List[int]] = []
    if use_qwen3 and all(0 <= value <= 1000 for value in raw_bbox):
        candidates.append(convert_qwen3_coordinates_to_absolute(raw_bbox, img_w, img_h, is_bbox=True))

    candidates.append(raw_bbox)

    # Some VLM endpoints return coordinates in a 2x screenshot space. This keeps
    # such boxes usable instead of clicking far outside the device screen.
    if raw_bbox[2] > img_w or raw_bbox[3] > img_h:
        half_scaled = [int(round(value * 0.5)) for value in raw_bbox]
        candidates.append(half_scaled)

    clipped_candidates = []
    for candidate in candidates:
        clipped = _clip_bbox_to_screen(candidate, img_w, img_h)
        if clipped is not None:
            clipped_candidates.append(clipped)
    candidates.extend(clipped_candidates)
    return _dedupe_bboxes(candidates)


def _bbox_center_in_any(bbox: List[int], bounds_list: List[List[int]], *, pad: int = 12) -> bool:
    if not bbox or not bounds_list:
        return False
    x1, y1, x2, y2 = bbox
    cx = (x1 + x2) // 2
    cy = (y1 + y2) // 2
    for bx1, by1, bx2, by2 in bounds_list:
        if bx1 - pad <= cx <= bx2 + pad and by1 - pad <= cy <= by2 + pad:
            return True
    return False


def _task_requires_search_input(task_text: str) -> bool:
    text = str(task_text or "").lower()
    has_search = any(keyword in text for keyword in _SEARCH_KEYWORDS)
    has_input = any(keyword in text for keyword in _INPUT_KEYWORDS)
    return has_search and has_input


def _task_allows_text_input(task_text: str) -> bool:
    text = str(task_text or "").lower()
    return any(keyword in text for keyword in _INPUT_KEYWORDS)


def _attrs_look_like_search_or_edit(attrs: Dict[str, Any]) -> bool:
    text = str(attrs.get("text", "") or attrs.get("content", "") or "").lower()
    desc = str(attrs.get("content-desc", "") or attrs.get("contentDescription", "") or "").lower()
    res_id = str(attrs.get("resource-id", "") or attrs.get("id", "") or "").lower()
    cls = str(attrs.get("class", "") or attrs.get("className", "") or "").lower()
    hint = str(attrs.get("hint", "") or attrs.get("placeholder", "") or "").lower()
    editable = str(attrs.get("editable", "false")).lower() in {"true", "1"}
    combined = "|".join([text, desc, res_id, cls, hint])
    return editable or "edittext" in cls or any(keyword in combined for keyword in _SEARCH_KEYWORDS)


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


def _extract_search_or_edit_bounds(hierarchy_text: str) -> List[List[int]]:
    if not hierarchy_text:
        return []
    bounds_list: List[List[int]] = []
    if hierarchy_text.lstrip().startswith("<"):
        try:
            import xml.etree.ElementTree as ET

            root = ET.fromstring(hierarchy_text)
        except Exception:
            return []
        for node in root.iter():
            if _attrs_look_like_search_or_edit(dict(node.attrib)):
                bounds = _coerce_bounds(node.get("bounds"))
                if bounds:
                    bounds_list.append(bounds)
        return bounds_list

    try:
        obj = json.loads(hierarchy_text)
    except Exception:
        return []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            attrs = node.get("attributes") if isinstance(node.get("attributes"), dict) else node
            if isinstance(attrs, dict) and _attrs_look_like_search_or_edit(attrs):
                bounds = _coerce_bounds(attrs.get("bounds") or attrs.get("rect"))
                if bounds:
                    bounds_list.append(bounds)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(obj)
    return bounds_list


def _contains_obvious_non_target(target_element: str, task_text: str) -> bool:
    target_lower = str(target_element or "").lower()
    task_lower = str(task_text or "").lower()
    if not target_lower:
        return False
    return any(keyword in target_lower for keyword in _NON_TARGET_KEYWORDS) and not any(
        keyword in task_lower for keyword in _NON_TARGET_KEYWORDS
    )


def _validate_decider_target_alignment(
    *,
    hierarchy_text: str,
    step_task: str,
    action: str,
    bbox: List[int],
    target_element: str,
) -> None:
    if action not in {"click", "click_input"}:
        return

    if _task_requires_search_input(step_task):
        search_bounds = _extract_search_or_edit_bounds(hierarchy_text)
        if not search_bounds:
            raise DeciderTargetMismatch("decider_target_mismatch: search/input target is not visible")
        if not _bbox_center_in_any(bbox, search_bounds, pad=24):
            raise DeciderTargetMismatch(
                f"decider_target_mismatch: bbox {bbox} is outside visible search/input controls"
            )

    task_target = _extract_click_target_text(step_task)
    if task_target:
        target_bounds = _extract_text_bounds_from_hierarchy_text(hierarchy_text, task_target)
        if target_bounds and not _bbox_center_in_any(bbox, target_bounds, pad=24):
            raise DeciderTargetMismatch(
                f"decider_target_mismatch: bbox {bbox} does not match task target {task_target!r}"
            )
        if _contains_obvious_non_target(target_element, step_task) and task_target not in str(target_element):
            raise DeciderTargetMismatch(
                f"decider_target_mismatch: decider target {target_element!r} conflicts with task target {task_target!r}"
            )


def _hierarchy_has_focus_or_editable(hierarchy_text: str) -> bool:
    text = str(hierarchy_text or "").lower()
    return (
        'focused="true"' in text
        or '"focused": true' in text
        or '"focused":"true"' in text
        or 'editable="true"' in text
        or '"editable": true' in text
        or "edittext" in text
    )


def _input_text_visible(hierarchy_text: str, text: str) -> bool:
    normalized_text = str(text or "").strip()
    return bool(normalized_text) and normalized_text in str(hierarchy_text or "")


def _perform_text_input(
    *,
    device,
    device_type: str,
    hierarchy_before: str,
    bbox: Optional[List[int]],
    text: str,
) -> str:
    if not text.strip():
        raise InputFailed("input_failed: empty text")

    for attempt in range(2):
        if bbox:
            x1, y1, x2, y2 = bbox
            device.click((x1 + x2) // 2, (y1 + y2) // 2)
            time.sleep(0.4)
            focused_hierarchy = get_hierarchy_text(device)
            if not _hierarchy_has_focus_or_editable(focused_hierarchy) and attempt == 0:
                logging.info("Input target not focused after click; retrying focus once.")
                continue
        else:
            focused_hierarchy = hierarchy_before

        device.input(text)
        time.sleep(settings.DEVICE_WAIT_TIME)
        post_hierarchy = get_hierarchy_text(device)
        if _input_text_visible(post_hierarchy, text):
            logging.info("\033[92mInput verified: text appeared in hierarchy.\033[0m")
            return post_hierarchy
        logging.warning("Input verification failed on attempt %d; retrying.", attempt + 1)

    raise InputFailed(f"input_failed: text {text!r} did not appear after input")


def _task_looks_like_tab_switch(task_text: str) -> bool:
    text = str(task_text or "").lower()
    keywords = (
        "tab",
        "navigation",
        "nav",
        "bottom",
        "top",
        "switch",
        "icon",
        "\u5bfc\u822a",
        "\u5bfc\u822a\u680f",
        "\u6807\u7b7e",
        "\u5207\u6362",
        "\u56fe\u6807",
        "\u5e95\u90e8",
        "\u9876\u90e8",
    )
    return any(keyword in text for keyword in keywords)


def _attach_pre_selected_tab_snapshot(
    action_record: Dict[str, Any],
    hierarchy_text: str,
) -> None:
    if action_record.get("type") != "click" or not _task_looks_like_tab_switch(str(action_record.get("source_task", ""))):
        return
    snapshot = _choose_tab_snapshot_for_action(_extract_selected_tab_snapshots(hierarchy_text), action_record)
    if snapshot:
        action_record["pre_selected_tab"] = snapshot
        logging.info(
            "Recorded pre-selected tab snapshot for recovery: label=%s center=(%s,%s)",
            snapshot.get("text") or snapshot.get("content_desc") or "",
            snapshot.get("center_x"),
            snapshot.get("center_y"),
        )


def resolve_action_bbox(
    hierarchy_text: str,
    raw_bbox: List[int],
    img_w: int,
    img_h: int,
    *,
    use_qwen3: bool,
    target_element: str = "",
) -> List[int]:
    target_bounds = _extract_text_bounds_from_hierarchy_text(hierarchy_text, target_element)
    if target_bounds:
        best = min(target_bounds, key=lambda b: max(1, (b[2] - b[0]) * (b[3] - b[1])))
        logging.info("\033[92mBBox resolved by target text '%s': %s.\033[0m", target_element, best)
        return best

    candidates = _candidate_absolute_bboxes(raw_bbox, img_w, img_h, use_qwen3)
    if not candidates:
        raise ValueError(f"Invalid bbox from decider: {raw_bbox}")

    for candidate in candidates:
        refined = refine_bbox_with_hierarchy(hierarchy_text, candidate, img_w, img_h)
        if _bbox_inside_screen(refined, img_w, img_h):
            if refined != candidate:
                logging.info("\033[92mBBox resolved via candidate %s -> %s.\033[0m", candidate, refined)
            return refined

    fallback = _clip_bbox_to_screen(candidates[0], img_w, img_h)
    if fallback is None:
        raise ValueError(f"Unable to resolve bbox into screen: {raw_bbox}")
    logging.warning("\033[93mBBox clipped to screen from %s to %s.\033[0m", candidates[0], fallback)
    return fallback


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


def _extract_click_target_text(task_text: str) -> Optional[str]:
    if not task_text:
        return None

    click_tokens = ("\u70b9\u51fb", "click", "éç‘°åš®")
    if not any(token in task_text for token in click_tokens):
        return None

    match = re.search(
        r"[\"\u201c\u201d\u300c\u300d]([^\"\u201c\u201d\u300c\u300d]+)[\"\u201c\u201d\u300c\u300d]",
        task_text,
    )
    if match:
        before = task_text[: match.start()]
        if "\u8f93\u5165" in before and any(keyword in before.lower() for keyword in _SEARCH_KEYWORDS):
            return None
        return match.group(1).strip()
    return None


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
    runtime,
    history: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Execute one decider step and persist its step-local artifacts."""
    metrics = runtime.metrics
    trace: Dict[str, Any] = {
        "phase": "decider",
        "step_index": step_index,
        "task": step_task,
    }

    t0 = metrics.now()
    trace["T0"] = metrics.relative_time(t0)
    screenshot_b64 = get_screenshot(device, device_type)
    t1 = metrics.now()
    trace["T1"] = metrics.relative_time(t1)

    pre_action_hierarchy_text = get_hierarchy_text(device)
    target_text = _extract_click_target_text(step_task)
    bounds_from_text = (
        _extract_text_bounds_from_hierarchy_text(pre_action_hierarchy_text, target_text) if target_text else []
    )

    decider_resp: Optional[Dict[str, Any]] = None
    if allow_hierarchy_text_decider and runtime.features.hierarchy_text_decider and target_text and bounds_from_text:
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
            "parameters": {"bbox": best_bbox, "target_element": target_text},
            "reasoning": f"Hierarchy text matched the target: {target_text}",
        }
        trace["T2"] = trace["T1"]
        trace["T3"] = trace["T1"]
        trace["T4"] = trace["T1"]
        trace["t3_note"] = "hierarchy_shortcut"
    else:
        red = "\033[91m"
        reset = "\033[0m"
        logging.info(f"{red}Using model output bbox (decider).{reset}")
        messages = build_auto_decider_messages(
            task=f"当前处在{app_name}，请帮我{step_task}",
            history=history or [],
            screenshot_b64=screenshot_b64,
        )

        def _validator(resp: Dict[str, Any]) -> None:
            validate_decider_response(resp, use_e2e=True)
            sanitize_decider_response(resp)

        for attempt in range(3):
            request_start = metrics.now()
            trace["attempt"] = attempt + 1
            trace["T2"] = metrics.relative_time(request_start)
            call_start = time.perf_counter()
            decider_resp = call_model_with_validation_retry(
                decider_client,
                decider_model,
                messages,
                validator_func=_validator,
                max_retries=5,
                max_tokens=256,
                context="Decider",
            )
            call_end = time.perf_counter()
            metrics.record_decider_call(call_end - call_start)
            trace["T3"] = metrics.relative_time(call_end)
            trace["T4"] = metrics.relative_time(call_end)
            trace["t3_note"] = "non_streaming_response"
            try:
                decider_resp = sanitize_decider_response(decider_resp)
            except ValueError:
                if attempt < 2:
                    logging.warning(
                        "Decider returned invalid executable payload (attempt=%s), retrying.",
                        attempt + 1,
                    )
                    time.sleep(0.6)
                    continue
                raise
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

    screenshot_file = save_raw_screenshot(output_dir, step_index, device_type, metrics=metrics)
    save_hierarchy(
        device,
        device_type,
        output_dir,
        step_index,
        async_enabled=runtime.features.async_artifact_io,
        metrics=metrics,
    )

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
    if action == "click_input" and not _task_allows_text_input(step_task):
        logging.warning(
            "\033[93mDecider returned click_input for non-input task; downgrading to click: %s\033[0m",
            step_task,
        )
        action = "click"
        decider_resp["action"] = "click"
        decider_resp.setdefault("parameters", {}).pop("text", None)
    elif action == "input" and not _task_allows_text_input(step_task):
        raise DeciderTargetMismatch(
            f"decider_target_mismatch: input action is not allowed for non-input task {step_task!r}"
        )
    react_item["function"]["name"] = action
    react_item["function"]["parameters"] = decider_resp.get("parameters", {})
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

    t5 = metrics.now()
    trace["T5"] = metrics.relative_time(t5)
    if action == "click":
        bbox = params.get("bbox")
        if bbox:
            bbox = resolve_action_bbox(
                pre_action_hierarchy_text,
                bbox,
                img_w,
                img_h,
                use_qwen3=use_qwen3,
                target_element=str(params.get("target_element", "")),
            )
        _validate_decider_target_alignment(
            hierarchy_text=pre_action_hierarchy_text,
            step_task=step_task,
            action=action,
            bbox=bbox,
            target_element=str(params.get("target_element", "")),
        )
        x1, y1, x2, y2 = bbox
        x, y = (x1 + x2) // 2, (y1 + y2) // 2
        device.click(x, y)
        action_record.update({"position_x": x, "position_y": y, "bounds": [x1, y1, x2, y2]})
        _attach_pre_selected_tab_snapshot(action_record, pre_action_hierarchy_text)
    elif action == "click_input":
        bbox = params.get("bbox")
        text = params.get("text", "")
        if bbox:
            bbox = resolve_action_bbox(
                pre_action_hierarchy_text,
                bbox,
                img_w,
                img_h,
                use_qwen3=use_qwen3,
                target_element=str(params.get("target_element", "")),
            )
        _validate_decider_target_alignment(
            hierarchy_text=pre_action_hierarchy_text,
            step_task=step_task,
            action=action,
            bbox=bbox,
            target_element=str(params.get("target_element", "")),
        )
        x1, y1, x2, y2 = bbox
        x, y = (x1 + x2) // 2, (y1 + y2) // 2
        post_input_hierarchy_text = _perform_text_input(
            device=device,
            device_type=device_type,
            hierarchy_before=pre_action_hierarchy_text,
            bbox=[x1, y1, x2, y2],
            text=text,
        )
        action_record.update({"position_x": x, "position_y": y, "bounds": [x1, y1, x2, y2], "text": text})
    elif action == "input":
        text = params.get("text", "")
        post_input_hierarchy_text = _perform_text_input(
            device=device,
            device_type=device_type,
            hierarchy_before=pre_action_hierarchy_text,
            bbox=None,
            text=text,
        )
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
        logging.info("Decider returned 'wait' - sleeping 2s and skipping step.")
        time.sleep(2.0)
        trace["T6"] = metrics.relative_time(metrics.now())
        trace["action"] = action
        metrics.record_timing_trace(trace)
        raise WaitActionSkip("wait action - step not recorded")
    elif action == "done":
        action_record["status"] = params.get("status", "success")
    else:
        raise ValueError(f"Unsupported action from decider: {action}")

    t6 = metrics.now()
    trace["T6"] = metrics.relative_time(t6)
    trace["action"] = action

    if runtime.features.async_artifact_io:
        submit_artifact_task(
            _run_annotation_safe,
            action,
            action_record,
            screenshot_file,
            output_dir,
            step_index,
            metrics,
        )
    else:
        _run_annotation_safe(action, action_record, screenshot_file, output_dir, step_index, metrics)

    time.sleep(settings.DEVICE_WAIT_TIME)
    post_hierarchy_text = locals().get("post_input_hierarchy_text") or get_hierarchy_text(device)
    metrics.record_timing_trace(trace)

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
    "DeciderTargetMismatch",
    "InputFailed",
    "WaitActionSkip",
    "_convert_bbox_to_qwen3_relative",
    "_extract_click_target_text",
    "_extract_search_or_edit_bounds",
    "_extract_text_bounds_from_hierarchy_text",
    "_task_requires_search_input",
    "execute_decider_one_step",
    "refine_bbox_with_hierarchy",
    "resolve_action_bbox",
    "sanitize_decider_response",
]
