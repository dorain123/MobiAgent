import concurrent.futures
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, List, Optional

from PIL import Image

from auto_explore.core.settings import _FP_EXECUTOR

try:
    from utils.parse_xml import extract_all_bounds, parse_bounds
except Exception:
    extract_all_bounds = None
    parse_bounds = None


def _normalize_hierarchy_text(text: str) -> str:
    text = re.sub(r"\d+", "#", text)
    return "".join(text.split())


def _hierarchy_fingerprint(hierarchy_text: str) -> str:
    if not hierarchy_text:
        return ""
    normalized = _normalize_hierarchy_text(hierarchy_text)
    if not normalized:
        return ""
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


_STABLE_RESOURCE_ID_KEYWORDS = (
    "title",
    "tab",
    "nav",
    "toolbar",
    "header",
    "bottom",
    "action_bar",
    "actionbar",
    "topbar",
)
_STABLE_CLASS_KEYWORDS = (
    "Toolbar",
    "ActionBar",
    "TabLayout",
    "BottomNavigationView",
    "NavigationView",
    "TabBar",
)


def _stable_text_fingerprint(hierarchy_text: str) -> str:
    """只对导航栏/标题/底部Tab等固定UI元素的文字计算指纹，忽略内容区动态文字。"""
    if not hierarchy_text:
        return ""
    stable_texts: List[str] = []
    try:
        if hierarchy_text.lstrip().startswith("<"):
            root = ET.fromstring(hierarchy_text)
            for node in root.iter():
                res_id = node.attrib.get("resource-id", "").lower()
                cls = node.attrib.get("class", "")
                text = node.attrib.get("text", "").strip()
                if not text:
                    continue
                if any(kw in res_id for kw in _STABLE_RESOURCE_ID_KEYWORDS) or any(
                    kw in cls for kw in _STABLE_CLASS_KEYWORDS
                ):
                    stable_texts.append(text)
        else:
            obj = json.loads(hierarchy_text)

            def _walk_stable(node: Any) -> None:
                if isinstance(node, dict):
                    attrs = node.get("attributes") if isinstance(node.get("attributes"), dict) else node
                    res_id = str(attrs.get("resource-id", attrs.get("id", ""))).lower()
                    cls = str(attrs.get("className", attrs.get("class", "")))
                    text = str(attrs.get("text", attrs.get("content", ""))).strip()
                    if text and (
                        any(kw in res_id for kw in _STABLE_RESOURCE_ID_KEYWORDS)
                        or any(kw in cls for kw in _STABLE_CLASS_KEYWORDS)
                    ):
                        stable_texts.append(text)
                    for v in node.values():
                        _walk_stable(v)
                elif isinstance(node, list):
                    for item in node:
                        _walk_stable(item)

            _walk_stable(obj)
    except Exception:
        pass

    if not stable_texts:
        return _hierarchy_fingerprint(hierarchy_text)

    joined = "|".join(sorted(stable_texts))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def _now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _hamming_distance_hex(a: str, b: str) -> int:
    if not a or not b or len(a) != len(b):
        return 64
    try:
        x = int(a, 16) ^ int(b, 16)
    except Exception:
        return 64
    return x.bit_count()


def _compute_dhash_hex(image_path: str, hash_size: int = 8) -> str:
    img = Image.open(image_path).convert("L")
    img = img.resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
    pixels = list(img.getdata())
    bits: List[int] = []
    row_stride = hash_size + 1
    for y in range(hash_size):
        row = pixels[y * row_stride : (y + 1) * row_stride]
        for x in range(hash_size):
            bits.append(1 if row[x] > row[x + 1] else 0)
    value = 0
    for bit in bits:
        value = (value << 1) | bit
    return f"{value:016x}"


def _collect_struct_tokens_from_xml(hierarchy_text: str) -> List[str]:
    tokens: List[str] = []
    try:
        root = ET.fromstring(hierarchy_text)
    except Exception:
        return tokens
    for node in root.iter():
        clickable = (node.attrib.get("clickable") or "").lower() == "true"
        long_clickable = (node.attrib.get("long-clickable") or "").lower() == "true"
        if not clickable and not long_clickable:
            continue
        cls = node.attrib.get("class", "")
        res_id = node.attrib.get("resource-id", "")
        bounds = node.attrib.get("bounds", "")
        tokens.append(f"{cls}|{res_id}|{bounds}")
    return tokens


def _collect_struct_tokens_from_json(hierarchy_obj: Any) -> List[str]:
    tokens: List[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            attrs = node.get("attributes") if isinstance(node.get("attributes"), dict) else node
            clickable_val = attrs.get("clickable")
            long_clickable_val = attrs.get("longClickable", attrs.get("long-clickable"))
            clickable = str(clickable_val).lower() in {"1", "true"}
            long_clickable = str(long_clickable_val).lower() in {"1", "true"}
            if clickable or long_clickable:
                cls = str(attrs.get("className", attrs.get("class", attrs.get("type", ""))))
                res_id = str(attrs.get("resource-id", attrs.get("id", attrs.get("key", ""))))
                bounds = str(attrs.get("bounds", attrs.get("rect", "")))
                tokens.append(f"{cls}|{res_id}|{bounds}")
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(hierarchy_obj)
    return tokens


def _compute_hierarchy_struct_fingerprint(hierarchy_text: str) -> str:
    if not hierarchy_text:
        return ""
    tokens: List[str] = []
    if hierarchy_text.lstrip().startswith("<"):
        tokens = _collect_struct_tokens_from_xml(hierarchy_text)
    else:
        try:
            obj = json.loads(hierarchy_text)
        except Exception:
            obj = None
        if obj is not None:
            tokens = _collect_struct_tokens_from_json(obj)
    if not tokens:
        return ""
    joined = "||".join(sorted(set(tokens)))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def _safe_future(fut: "concurrent.futures.Future", default: Any) -> Any:
    try:
        return fut.result()
    except Exception:
        return default


def _compute_fingerprints_concurrent(
    hierarchy_text: str,
    screenshot_path: Optional[str] = None,
) -> tuple:
    fut_fp = _FP_EXECUTOR.submit(_hierarchy_fingerprint, hierarchy_text)
    fut_struct = _FP_EXECUTOR.submit(_compute_hierarchy_struct_fingerprint, hierarchy_text)
    fut_dhash = _FP_EXECUTOR.submit(_compute_dhash_hex, screenshot_path) if screenshot_path else None
    fp = _safe_future(fut_fp, "")
    struct_fp = _safe_future(fut_struct, "")
    dhash_hex = _safe_future(fut_dhash, "") if fut_dhash else ""
    return fp, struct_fp, dhash_hex


def _triple_verify(
    pre_hierarchy: str,
    pre_struct_fp: str,
    pre_dhash: str,
    post_hierarchy: str,
    post_screenshot_path: str,
) -> bool:
    fp_ok = _stable_text_fingerprint(pre_hierarchy) == _stable_text_fingerprint(post_hierarchy)
    _, post_struct_fp_val, post_dhash = _compute_fingerprints_concurrent(
        post_hierarchy, screenshot_path=post_screenshot_path
    )
    struct_ok = pre_struct_fp == post_struct_fp_val
    visual_ok = (_hamming_distance_hex(pre_dhash, post_dhash) <= 3) if pre_dhash and post_dhash else fp_ok
    return (int(fp_ok) + int(struct_ok) + int(visual_ok)) >= 2


def _bbox_iou(a: List[int], b: List[int]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
        return 0.0
    inter_area = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
    a_area = max(0, (ax2 - ax1)) * max(0, (ay2 - ay1))
    b_area = max(0, (bx2 - bx1)) * max(0, (by2 - by1))
    if a_area + b_area - inter_area == 0:
        return 0.0
    return inter_area / (a_area + b_area - inter_area)


def _extract_bounds_from_json(obj: Any) -> List[List[int]]:
    bounds_list: List[List[int]] = []

    def _coerce_bounds(value: Any) -> Optional[List[int]]:
        if isinstance(value, (list, tuple)) and len(value) == 4:
            try:
                return [int(v) for v in value]
            except Exception:
                return None
        if isinstance(value, str) and parse_bounds:
            return parse_bounds(value)
        return None

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key == "bounds":
                    bounds = _coerce_bounds(val)
                    if bounds:
                        bounds_list.append(bounds)
                else:
                    _walk(val)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(obj)
    return bounds_list


def _extract_bounds_from_hierarchy_text(hierarchy_text: str) -> List[List[int]]:
    if not hierarchy_text:
        return []
    if hierarchy_text.lstrip().startswith("<"):
        if extract_all_bounds:
            return extract_all_bounds(hierarchy_text, need_clickable=True)
        return []

    try:
        obj = json.loads(hierarchy_text)
    except Exception:
        return []
    return _extract_bounds_from_json(obj)


def _detect_countdown(text: str) -> bool:
    if not text:
        return False
    patterns = [
        r"\b\d+\s*(秒|s|sec|second)s?\b",
        r"(剩余|倒计时|跳过)\s*\d+",
        r"\b\d{1,2}:\d{2}\b",
    ]
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


__all__ = [
    "_bbox_iou",
    "_collect_struct_tokens_from_json",
    "_collect_struct_tokens_from_xml",
    "_compute_dhash_hex",
    "_compute_fingerprints_concurrent",
    "_compute_hierarchy_struct_fingerprint",
    "_detect_countdown",
    "_extract_bounds_from_hierarchy_text",
    "_extract_bounds_from_json",
    "_hamming_distance_hex",
    "_hierarchy_fingerprint",
    "_normalize_hierarchy_text",
    "_now_ts",
    "_safe_future",
    "_stable_text_fingerprint",
    "_triple_verify",
]
