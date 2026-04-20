import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from PIL import Image

from auto_explore.adapters.device import compute_swipe_positions
from auto_explore.core.artifacts import get_current_screenshot_path
from auto_explore.core import settings

try:
    from hmdriver2.proto import KeyCode
except Exception:
    KeyCode = None

try:
    from utils.parse_xml import parse_bounds
except Exception:
    parse_bounds = None


_CLOSE_ID_KWS = (
    "close",
    "dismiss",
    "skip",
    "cancel",
    "iv_close",
    "btn_close",
    "tv_skip",
    "ad_close",
    "popup_close",
    "dialog_close",
    "关闭",
    "跳过",
    "不再提示",
    "我知道了",
)


def navigate_back(device, device_type: str) -> None:
    """执行返回上一层。"""
    try:
        if device_type == "Android":
            device.keyevent("back")
        else:
            if KeyCode is not None:
                device.keyevent(KeyCode.BACK)
            else:
                device.keyevent(2)
        time.sleep(settings.DEVICE_WAIT_TIME)
    except Exception as e:
        logging.warning(f"Back navigation failed: {e}")


def _get_current_screen_size(device_type: str) -> Optional[tuple[int, int]]:
    try:
        path = get_current_screenshot_path(device_type)
        with Image.open(path) as img:
            return img.size
    except Exception as e:
        logging.warning(f"Failed to read current screenshot size: {e}")
        return None


def _reverse_direction(direction: str) -> str:
    mapping = {"UP": "DOWN", "DOWN": "UP", "LEFT": "RIGHT", "RIGHT": "LEFT"}
    return mapping.get(direction.upper(), "DOWN")


def _get_app_package_name(device, app_name: str) -> Optional[str]:
    if not app_name:
        return None
    mapping = getattr(device, "app_package_names", None)
    if isinstance(mapping, dict):
        return mapping.get(app_name)
    return None


def _extract_foreground_from_hierarchy(hierarchy_text: str) -> tuple[str, str]:
    if not hierarchy_text:
        return "", ""

    try:
        obj = json.loads(hierarchy_text) if isinstance(hierarchy_text, str) else hierarchy_text
    except Exception:
        return "", ""

    package_keys = {"bundleName", "bundle_name", "packageName", "package", "appPackage"}
    ability_keys = {"abilityName", "ability_name", "uiAbilityName", "uiAbility", "pageName", "page_name"}

    def _walk(node: Any) -> tuple[str, str]:
        if isinstance(node, dict):
            package = ""
            ability = ""
            for key, val in node.items():
                if key in package_keys and isinstance(val, str) and val.strip() and not package:
                    package = val.strip()
                if key in ability_keys and isinstance(val, str) and val.strip() and not ability:
                    ability = val.strip()
            attrs = node.get("attributes")
            if isinstance(attrs, dict):
                for key, val in attrs.items():
                    if key in package_keys and isinstance(val, str) and val.strip() and not package:
                        package = val.strip()
                    if key in ability_keys and isinstance(val, str) and val.strip() and not ability:
                        ability = val.strip()
            if package and ability:
                return package, ability
            for val in node.values():
                p, a = _walk(val)
                if p and not package:
                    package = p
                if a and not ability:
                    ability = a
                if package and ability:
                    return package, ability
            return package, ability
        if isinstance(node, list):
            package = ""
            ability = ""
            for item in node:
                p, a = _walk(item)
                if p and not package:
                    package = p
                if a and not ability:
                    ability = a
                if package and ability:
                    return package, ability
            return package, ability
        return "", ""

    return _walk(obj)


def _get_foreground_app_state(device, device_type: str, hierarchy_text: str = "") -> Dict[str, str]:
    state = {"package": "", "ability": "", "source": "unknown"}
    driver = getattr(device, "d", None)

    if device_type == "Android" and driver is not None:
        for api_name in ("app_current", "current_app"):
            api = getattr(driver, api_name, None)
            if callable(api):
                try:
                    raw = api()
                    if isinstance(raw, dict):
                        pkg = str(raw.get("package") or raw.get("appPackage") or raw.get("pkg") or "").strip()
                        act = str(raw.get("activity") or raw.get("appActivity") or raw.get("act") or "").strip()
                        if pkg:
                            state.update({"package": pkg, "ability": act, "source": f"android:{api_name}"})
                            return state
                    elif isinstance(raw, str) and raw.strip():
                        state.update({"package": raw.strip(), "source": f"android:{api_name}"})
                        return state
                except Exception:
                    continue

    if device_type == "Harmony" and driver is not None:
        shell = getattr(driver, "shell", None)
        if callable(shell):
            for cmd in ("aa dump --mission-list", "aa dump -l", "aa dump --stack"):
                try:
                    out = shell(cmd)
                    text = str(out)
                    pkg_match = re.search(r"(?:bundleName|bundle_name|packageName)\s*[:=]\s*([\w\.]+)", text)
                    ability_match = re.search(r"(?:abilityName|uiAbilityName|ability|uiAbility)\s*[:=]\s*([\w\.$]+)", text)
                    if pkg_match:
                        state["package"] = pkg_match.group(1)
                    if ability_match:
                        state["ability"] = ability_match.group(1)
                    if state["package"]:
                        state["source"] = f"harmony:shell:{cmd}"
                        return state
                except Exception:
                    continue

    pkg, ability = _extract_foreground_from_hierarchy(hierarchy_text)
    if pkg or ability:
        state.update({"package": pkg, "ability": ability, "source": "hierarchy"})
    return state


def _is_app_in_foreground(device, device_type: str, app_name: Optional[str], hierarchy_text: str) -> bool:
    if not app_name:
        return True
    package_name = _get_app_package_name(device, app_name)
    if not package_name:
        return True

    fg_state = _get_foreground_app_state(device, device_type, hierarchy_text)
    fg_package = fg_state.get("package", "")
    if fg_package:
        in_foreground = package_name == fg_package or package_name in fg_package or fg_package in package_name
        if not in_foreground:
            logging.warning(
                "\033[91mForeground app mismatch: expect=%s actual=%s ability=%s source=%s\033[0m",
                package_name,
                fg_package,
                fg_state.get("ability", ""),
                fg_state.get("source", "unknown"),
            )
        return in_foreground

    if hierarchy_text:
        return package_name in hierarchy_text
    return True


def _find_dismissible_element(hierarchy_text: str) -> Optional[List[int]]:
    """在 hierarchy 中寻找广告/弹窗关闭按钮。"""
    if not hierarchy_text:
        return None

    def _parse_bounds(bounds_str: str) -> Optional[List[int]]:
        if parse_bounds:
            return parse_bounds(bounds_str)
        m = re.findall(r"\d+", bounds_str or "")
        return [int(x) for x in m] if len(m) == 4 else None

    if hierarchy_text.lstrip().startswith("<"):
        try:
            root = ET.fromstring(hierarchy_text)
        except Exception:
            return None
        best: Optional[List[int]] = None
        for node in root.iter():
            clickable = (node.attrib.get("clickable") or "").lower() == "true"
            if not clickable:
                continue
            res_id = (node.attrib.get("resource-id") or "").lower()
            text = (node.attrib.get("text") or "").lower()
            desc = (node.attrib.get("content-desc") or "").lower()
            combined = res_id + "|" + text + "|" + desc
            if any(kw in combined for kw in _CLOSE_ID_KWS):
                bounds = _parse_bounds(node.attrib.get("bounds", ""))
                if bounds:
                    if any(kw in res_id for kw in _CLOSE_ID_KWS):
                        return bounds
                    best = best or bounds
        return best

    try:
        obj = json.loads(hierarchy_text)
    except Exception:
        return None

    result: List[Optional[List[int]]] = [None]

    def _walk(node: Any) -> None:
        if result[0] is not None:
            return
        if isinstance(node, dict):
            attrs = node.get("attributes") if isinstance(node.get("attributes"), dict) else node
            clickable = str(attrs.get("clickable", attrs.get("enabled", "false"))).lower() in {"true", "1"}
            if clickable:
                res_id = str(attrs.get("resource-id", attrs.get("id", ""))).lower()
                text = str(attrs.get("text", attrs.get("label", ""))).lower()
                desc = str(attrs.get("contentDescription", attrs.get("content-desc", ""))).lower()
                combined = res_id + "|" + text + "|" + desc
                if any(kw in combined for kw in _CLOSE_ID_KWS):
                    raw_bounds = attrs.get("bounds") or attrs.get("rect")
                    if isinstance(raw_bounds, (list, tuple)) and len(raw_bounds) == 4:
                        result[0] = [int(v) for v in raw_bounds]
                        return
                    if isinstance(raw_bounds, str):
                        b = _parse_bounds(raw_bounds)
                        if b:
                            result[0] = b
                            return
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(obj)
    return result[0]


def _task_mentions_swipe(task_text: str) -> bool:
    if not task_text:
        return False
    tokens = ["滑动", "上滑", "下滑", "左滑", "右滑", "上划", "下划", "左划", "右划"]
    return any(t in task_text for t in tokens)


def replay_action_record(device, action_record: Dict[str, Any]) -> bool:
    action_type = str(action_record.get("type", "")).lower()
    try:
        if action_type == "click":
            x = action_record.get("position_x")
            y = action_record.get("position_y")
            if x is None or y is None:
                return False
            device.click(int(x), int(y))
            return True
        if action_type == "click_input":
            x = action_record.get("position_x")
            y = action_record.get("position_y")
            text = action_record.get("text", "")
            if x is None or y is None:
                return False
            device.click(int(x), int(y))
            if text:
                device.input(str(text))
            return True
        if action_type == "input":
            text = action_record.get("text", "")
            if text:
                device.input(str(text))
            return True
        if action_type == "swipe":
            sx = action_record.get("press_position_x")
            sy = action_record.get("press_position_y")
            ex = action_record.get("release_position_x")
            ey = action_record.get("release_position_y")
            if None in (sx, sy, ex, ey):
                return False
            device.swipe_with_coords(int(sx), int(sy), int(ex), int(ey))
            return True
        if action_type == "wait":
            time.sleep(1.0)
            return True
    except Exception as e:
        logging.warning(f"Replay action failed (type={action_type}): {e}")
        return False
    return False


def perform_backtrack_action(device, device_type: str, action_record: Optional[Dict[str, Any]]) -> None:
    if not action_record:
        navigate_back(device, device_type)
        return

    action_type = str(action_record.get("type", "")).lower()
    if action_type == "click_input":
        navigate_back(device, device_type)
        navigate_back(device, device_type)
        return

    if action_type == "swipe":
        direction = str(action_record.get("direction", "")).upper()
        reverse_direction = _reverse_direction(direction)
        size = _get_current_screen_size(device_type)
        if size:
            img_w, img_h = size
            sx, sy, ex, ey = compute_swipe_positions(reverse_direction, img_w, img_h)
            try:
                device.swipe_with_coords(sx, sy, ex, ey)
                time.sleep(settings.DEVICE_WAIT_TIME)
                return
            except Exception as e:
                logging.warning(f"Reverse swipe backtrack failed: {e}")

    navigate_back(device, device_type)


__all__ = [
    "_extract_foreground_from_hierarchy",
    "_find_dismissible_element",
    "_get_app_package_name",
    "_get_current_screen_size",
    "_get_foreground_app_state",
    "_is_app_in_foreground",
    "_reverse_direction",
    "_task_mentions_swipe",
    "navigate_back",
    "perform_backtrack_action",
    "replay_action_record",
]
