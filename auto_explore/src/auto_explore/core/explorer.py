import difflib
import hashlib
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from openai import OpenAI

from auto_explore.adapters.device import get_screenshot, robust_json_loads
from auto_explore.core.fingerprints import (
    _collect_struct_tokens_from_json,
    _collect_struct_tokens_from_xml,
)
from auto_explore.core.prompting import build_explorer_prompt
from auto_explore.core.settings import API_TIMEOUT, EXPLORER_MAX_TOKENS, EXPLORER_TEMPERATURE, MAX_RETRIES


def _truncate_for_log(text: str, limit: int = 160) -> str:
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, count=1, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped, count=1)
    return stripped.strip()


def _extract_first_balanced_json_object(text: str) -> Optional[str]:
    start_idx = text.find("{")
    if start_idx == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for idx in range(start_idx, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start_idx : idx + 1]
    return None


def _build_parse_variants(raw_text: str) -> List[str]:
    variants: List[str] = []
    seen: set[str] = set()

    def add(value: Optional[str]) -> None:
        if not value:
            return
        candidate = value.strip()
        if not candidate or candidate in seen:
            return
        seen.add(candidate)
        variants.append(candidate)

    stripped = raw_text.strip()
    fence_stripped = _strip_code_fence(stripped)

    add(stripped)
    add(fence_stripped)

    fenced_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", stripped, flags=re.IGNORECASE)
    if fenced_match:
        add(fenced_match.group(1))

    add(_extract_first_balanced_json_object(stripped))
    if fence_stripped != stripped:
        add(_extract_first_balanced_json_object(fence_stripped))

    return variants


def _compute_json_balance(text: str) -> Dict[str, Any]:
    brace_balance = 0
    bracket_balance = 0
    in_string = False
    escape = False

    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            brace_balance += 1
        elif ch == "}":
            brace_balance -= 1
        elif ch == "[":
            bracket_balance += 1
        elif ch == "]":
            bracket_balance -= 1

    return {
        "brace_balance": brace_balance,
        "bracket_balance": bracket_balance,
        "unterminated_string": in_string,
    }


def _build_response_diagnostics(raw_text: str, *, finish_reason: Optional[str] = None) -> Dict[str, Any]:
    stripped = raw_text.strip()
    balance = _compute_json_balance(stripped)
    has_code_fence = "```" in stripped
    likely_truncated = (
        finish_reason == "length"
        or balance["brace_balance"] > 0
        or balance["bracket_balance"] > 0
        or balance["unterminated_string"]
        or (stripped.startswith("```") and stripped.count("```") == 1)
    )
    return {
        "raw_length": len(raw_text),
        "has_code_fence": has_code_fence,
        "brace_balance": balance["brace_balance"],
        "bracket_balance": balance["bracket_balance"],
        "unterminated_string": balance["unterminated_string"],
        "finish_reason": finish_reason or "",
        "head": _truncate_for_log(stripped[:160]),
        "tail": _truncate_for_log(stripped[-160:]),
        "likely_truncated": likely_truncated,
    }


def _log_response_diagnostics(diagnostics: Dict[str, Any], *, attempt: int) -> None:
    logging.error(
        "Explorer response diagnostics: attempt=%s len=%s fence=%s brace_balance=%s "
        "bracket_balance=%s unterminated_string=%s finish_reason=%s",
        attempt,
        diagnostics["raw_length"],
        diagnostics["has_code_fence"],
        diagnostics["brace_balance"],
        diagnostics["bracket_balance"],
        diagnostics["unterminated_string"],
        diagnostics["finish_reason"] or "unknown",
    )
    logging.error("Explorer response preview head=%s", diagnostics["head"])
    logging.error("Explorer response preview tail=%s", diagnostics["tail"])


def _parse_explorer_response_content(content: Any, *, finish_reason: Optional[str], attempt: int) -> Dict[str, Any]:
    raw_text = content if isinstance(content, str) else str(content or "")
    diagnostics = _build_response_diagnostics(raw_text, finish_reason=finish_reason)

    for variant in _build_parse_variants(raw_text):
        try:
            return json.loads(variant)
        except json.JSONDecodeError:
            try:
                parsed = robust_json_loads(variant)
                if isinstance(parsed, dict) and ("candidates" in parsed or "popup" in parsed):
                    return parsed
            except Exception:
                continue

    _log_response_diagnostics(diagnostics, attempt=attempt)
    if diagnostics["likely_truncated"]:
        raise ValueError("疑似截断/格式不完整的 JSON 响应")
    raise ValueError("无法解析 JSON 响应，可能包含额外包装或格式污染")


def get_hierarchy_text(device) -> str:
    try:
        hierarchy = device.dump_hierarchy()
        if isinstance(hierarchy, str):
            return hierarchy
        return json.dumps(hierarchy, ensure_ascii=False)
    except Exception as e:
        logging.warning(f"Failed to dump hierarchy for explorer: {e}")
        return ""


def _compute_adaptive_similarity_threshold(hierarchy_text: str) -> float:
    if not hierarchy_text:
        return 0.9
    if hierarchy_text.lstrip().startswith("<"):
        tokens = _collect_struct_tokens_from_xml(hierarchy_text)
    else:
        try:
            obj = json.loads(hierarchy_text)
            tokens = _collect_struct_tokens_from_json(obj)
        except Exception:
            tokens = []
    element_count = len(tokens)
    return max(0.70, 0.95 - 0.01 * min(element_count, 25))


def _normalize_candidate_target(text: str) -> str:
    target = "".join(str(text or "").lower().split())
    for token in (
        "switchto",
        "navigation",
        "bottom",
        "top",
        "click",
        "tap",
        "open",
        "switch",
        "select",
        "button",
        "tab",
        "nav",
        "icon",
        "\u70b9\u51fb",
        "\u6253\u5f00",
        "\u8fdb\u5165",
        "\u5207\u6362\u5230",
        "\u5207\u6362\u81f3",
        "\u5207\u6362",
        "\u9009\u62e9",
        "\u5e95\u90e8",
        "\u9876\u90e8",
        "\u5bfc\u822a\u680f",
        "\u5bfc\u822a",
        "\u6807\u7b7e\u9875",
        "\u6807\u7b7e",
        "\u56fe\u6807",
        "\u5165\u53e3",
        "\u6309\u94ae",
        "\u9875\u9762",
        "\u9891\u9053",
        "\u680f",
        "\u7684",
    ):
        target = target.replace(token, "")
    return target.strip()


def _extract_candidate_target_key(task: str) -> str:
    task_text = str(task or "").strip()
    if not task_text:
        return ""
    quote_match = re.search(r"[\"\u201c\u201d\u300c\u300d]([^\"\u201c\u201d\u300c\u300d]+)[\"\u201c\u201d\u300c\u300d]", task_text)
    if quote_match:
        return _normalize_candidate_target(quote_match.group(1))
    target_patterns = (
        r"(?:\u70b9\u51fb|\u6253\u5f00|\u8fdb\u5165|\u5207\u6362\u5230|\u5207\u6362\u81f3|\u5207\u6362|\u9009\u62e9)\s*([^,\uff0c\u3002.;\uff1b:\uff1a]+)",
        r"(?:click|tap|open|switch to|switch|select)\s+([^,\uff0c\u3002.;\uff1b:\uff1a]+)",
        r"([^,\uff0c\u3002.;\uff1b:\uff1a]+?)(?:tab|\u6807\u7b7e|\u5bfc\u822a|\u5165\u53e3|\u6309\u94ae|\u56fe\u6807)",
    )
    for pattern in target_patterns:
        match = re.search(pattern, task_text, flags=re.IGNORECASE)
        if match:
            key = _normalize_candidate_target(match.group(1))
            if key:
                return key
    return ""


def _candidate_tasks_are_duplicate(left: str, right: str, sim_threshold: float) -> tuple[bool, float, str, str]:
    left_target = _extract_candidate_target_key(left)
    right_target = _extract_candidate_target_key(right)
    if left_target and right_target:
        target_ratio = difflib.SequenceMatcher(None, left_target, right_target).ratio()
        if left_target != right_target and target_ratio < 0.9:
            return False, target_ratio, left_target, right_target
        return True, target_ratio, left_target, right_target
    ratio = difflib.SequenceMatcher(None, left, right).ratio()
    return ratio > sim_threshold, ratio, left_target, right_target


def _deduplicate_candidates(
    candidates: List[Dict[str, Any]],
    already_explored: Optional[List[str]] = None,
    sim_threshold: float = 0.75,
) -> List[Dict[str, Any]]:
    kept: List[Dict[str, Any]] = []
    for cand in candidates:
        task = str(cand.get("single_step_task", "")).strip()
        if not task:
            continue
        if already_explored:
            skip = False
            for explored in already_explored:
                is_dup, ratio, task_target, explored_target = _candidate_tasks_are_duplicate(task, explored, 0.8)
                if is_dup:
                    logging.info(
                        "Candidate dedup: skip '%s' (similar to explored '%s', sim=%.2f, target=%s/%s)",
                        task,
                        explored,
                        ratio,
                        task_target,
                        explored_target,
                    )
                    skip = True
                    break
            if skip:
                continue
        duplicate = False
        for prev in kept:
            prev_task = str(prev.get("single_step_task", ""))
            is_dup, ratio, task_target, prev_target = _candidate_tasks_are_duplicate(task, prev_task, sim_threshold)
            if is_dup:
                logging.info(
                    "Candidate dedup: skip '%s' (similar to kept '%s', sim=%.2f, target=%s/%s)",
                    task,
                    prev_task,
                    ratio,
                    task_target,
                    prev_target,
                )
                duplicate = True
                break
        if not duplicate:
            kept.append(cand)
    return kept


class ExplorerCache:
    def __init__(self, ttl_sec: float = 300.0, metrics=None):
        self._cache: Dict[str, Any] = {}
        self._ttl = ttl_sec
        self._metrics = metrics

    def _make_key(
        self,
        struct_fp: str,
        depth: int,
        breadth: int,
        already_explored: Optional[List[str]],
    ) -> str:
        explored_hash = hashlib.md5("|".join(sorted(already_explored or [])).encode()).hexdigest()[:8]
        return f"{struct_fp}|{depth}|{breadth}|{explored_hash}"

    def get(
        self,
        struct_fp: str,
        depth: int,
        breadth: int,
        already_explored: Optional[List[str]],
    ) -> Optional[List[Dict]]:
        key = self._make_key(struct_fp, depth, breadth, already_explored)
        entry = self._cache.get(key)
        hit = bool(entry and (time.time() - entry["ts"]) < self._ttl)
        if self._metrics is not None:
            self._metrics.record_explorer_cache_lookup(hit)
        if hit:
            return list(entry["candidates"])
        return None

    def put(
        self,
        struct_fp: str,
        depth: int,
        breadth: int,
        already_explored: Optional[List[str]],
        candidates: List[Dict],
    ) -> None:
        key = self._make_key(struct_fp, depth, breadth, already_explored)
        self._cache[key] = {"candidates": list(candidates), "ts": time.time()}


class ScreenStateCache:
    def __init__(self, staleness_sec: float = 0.3, metrics=None):
        self._screenshot_b64: Optional[str] = None
        self._hierarchy_text: Optional[str] = None
        self._last_capture: float = 0.0
        self._staleness = staleness_sec
        self._metrics = metrics

    def capture(self, device, device_type: str, force: bool = False):
        now = time.time()
        should_refresh = (
            force
            or self._screenshot_b64 is None
            or (now - self._last_capture) > self._staleness
        )
        if self._metrics is not None:
            self._metrics.record_screen_cache_lookup(not should_refresh)
        if should_refresh:
            self._screenshot_b64 = get_screenshot(device, device_type)
            self._hierarchy_text = get_hierarchy_text(device)
            self._last_capture = now
        return self._screenshot_b64, self._hierarchy_text

    def invalidate(self) -> None:
        self._last_capture = 0.0


def _capture_screen(
    device,
    device_type: str,
    screen_cache: Optional["ScreenStateCache"],
) -> tuple:
    if screen_cache is not None:
        return screen_cache.capture(device, device_type, force=True)
    return get_screenshot(device, device_type), get_hierarchy_text(device)


def call_explorer_model(
    explorer_client: OpenAI,
    explorer_model: str,
    screenshot_b64: str,
    hierarchy_text: str,
    depth: int,
    breadth: int,
    action_history: List[Dict[str, Any]],
    already_explored: Optional[List[str]] = None,
    metrics=None,
    trace_meta: Optional[Dict[str, Any]] = None,
    disable_thinking: bool = False,
) -> tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    prompt = build_explorer_prompt(depth, breadth, hierarchy_text, action_history, already_explored=already_explored)

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
                },
            ],
        }
    ]

    timeline: Dict[str, Any] = dict(trace_meta or {})
    if metrics is not None:
        timeline.setdefault("phase", "explorer")
        timeline.setdefault("T0", metrics.relative_time())
        timeline.setdefault("T1", timeline["T0"])

    last_err: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        try:
            call_start = time.perf_counter()
            if metrics is not None:
                timeline["attempt"] = attempt + 1
                timeline["T2"] = metrics.relative_time(call_start)
            request_kwargs: Dict[str, Any] = {
                "model": explorer_model,
                "messages": messages,
                "timeout": API_TIMEOUT,
                "max_tokens": EXPLORER_MAX_TOKENS,
                "temperature": EXPLORER_TEMPERATURE,
            }
            if disable_thinking:
                request_kwargs["extra_body"] = {
                    "chat_template_kwargs": {"enable_thinking": False},
                }
            response = explorer_client.chat.completions.create(**request_kwargs)
            choice = response.choices[0]
            finish_reason = getattr(choice, "finish_reason", None)
            call_end = time.perf_counter()
            if metrics is not None:
                metrics.record_explorer_call(call_end - call_start)
                timeline["T3"] = metrics.relative_time(call_end)
                timeline["T4"] = metrics.relative_time(call_end)
                timeline["T5"] = None
                timeline["T6"] = None
                timeline["t3_note"] = "non_streaming_response"
            if finish_reason and finish_reason != "stop":
                logging.warning("Explorer finish_reason=%s on attempt=%s", finish_reason, attempt + 1)
            content = choice.message.content
            parsed = _parse_explorer_response_content(content, finish_reason=finish_reason, attempt=attempt + 1)
            candidates = parsed.get("candidates", [])
            if not isinstance(candidates, list):
                raise ValueError("`candidates` must be a list")

            normalized = []
            for i, candidate in enumerate(candidates[:breadth], 1):
                task = str(candidate.get("single_step_task", "")).strip()
                if not task:
                    continue
                normalized.append(
                    {
                        "rank": candidate.get("rank", i),
                        "single_step_task": task,
                        "reason": str(candidate.get("reason", "")).strip(),
                    }
                )

            if not normalized:
                raise ValueError("No valid candidates returned")

            popup_info: Optional[Dict[str, Any]] = None
            raw_popup = parsed.get("popup")
            if isinstance(raw_popup, dict) and raw_popup.get("detected"):
                close_pt = raw_popup.get("close_point")
                if isinstance(close_pt, (list, tuple)) and len(close_pt) == 2:
                    popup_info = {"detected": True, "close_point": list(close_pt)}
                else:
                    popup_info = {"detected": True, "close_point": None}

            if metrics is not None:
                timeline["candidate_count"] = len(normalized)
                metrics.record_timing_trace(timeline)
            return normalized, popup_info
        except Exception as e:
            last_err = e
            logging.warning(f"Explorer model parse/call failed (attempt={attempt + 1}): {e}")
            time.sleep(1.2)

    if metrics is not None:
        timeline["error"] = str(last_err)
        metrics.record_timing_trace(timeline)
    raise RuntimeError(f"Explorer model failed after retries: {last_err}")


__all__ = [
    "API_TIMEOUT",
    "EXPLORER_MAX_TOKENS",
    "MAX_RETRIES",
    "ExplorerCache",
    "ScreenStateCache",
    "_capture_screen",
    "_compute_adaptive_similarity_threshold",
    "_deduplicate_candidates",
    "call_explorer_model",
    "get_hierarchy_text",
]
