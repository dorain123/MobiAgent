import difflib
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional

from openai import OpenAI

from auto_explore.adapters.device import get_screenshot, robust_json_loads
from auto_explore.core.fingerprints import (
    _collect_struct_tokens_from_json,
    _collect_struct_tokens_from_xml,
)
from auto_explore.core.prompting import build_explorer_prompt
from auto_explore.core.settings import API_TIMEOUT, EXPLORER_MAX_TOKENS, MAX_RETRIES


def get_hierarchy_text(device) -> str:
    try:
        h = device.dump_hierarchy()
        if isinstance(h, str):
            return h
        return json.dumps(h, ensure_ascii=False)
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
                ratio = difflib.SequenceMatcher(None, task, explored).ratio()
                if ratio > 0.8:
                    logging.info(
                        "Candidate dedup: skip '%s' (similar to explored '%s', sim=%.2f)",
                        task,
                        explored,
                        ratio,
                    )
                    skip = True
                    break
            if skip:
                continue
        duplicate = False
        for prev in kept:
            prev_task = str(prev.get("single_step_task", ""))
            ratio = difflib.SequenceMatcher(None, task, prev_task).ratio()
            if ratio > sim_threshold:
                logging.info(
                    "Candidate dedup: skip '%s' (similar to kept '%s', sim=%.2f)",
                    task,
                    prev_task,
                    ratio,
                )
                duplicate = True
                break
        if not duplicate:
            kept.append(cand)
    return kept


class ExplorerCache:
    def __init__(self, ttl_sec: float = 300.0):
        self._cache: Dict[str, Any] = {}
        self._ttl = ttl_sec

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
        if entry and (time.time() - entry["ts"]) < self._ttl:
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
    def __init__(self, staleness_sec: float = 0.3):
        self._screenshot_b64: Optional[str] = None
        self._hierarchy_text: Optional[str] = None
        self._last_capture: float = 0.0
        self._staleness = staleness_sec

    def capture(self, device, device_type: str, force: bool = False):
        now = time.time()
        if force or self._screenshot_b64 is None or (now - self._last_capture) > self._staleness:
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

    last_err: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        try:
            response = explorer_client.chat.completions.create(
                model=explorer_model,
                messages=messages,
                timeout=API_TIMEOUT,
                max_tokens=EXPLORER_MAX_TOKENS,
                temperature=0.4 + attempt * 0.2,
            )
            content = response.choices[0].message.content
            parsed = robust_json_loads(content)
            candidates = parsed.get("candidates", [])
            if not isinstance(candidates, list):
                raise ValueError("`candidates` must be a list")

            normalized = []
            for i, c in enumerate(candidates[:breadth], 1):
                task = str(c.get("single_step_task", "")).strip()
                if not task:
                    continue
                normalized.append(
                    {
                        "rank": c.get("rank", i),
                        "single_step_task": task,
                        "reason": str(c.get("reason", "")).strip(),
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

            return normalized, popup_info
        except Exception as e:
            last_err = e
            logging.warning(f"Explorer model parse/call failed (attempt={attempt + 1}): {e}")
            time.sleep(1.2)

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
