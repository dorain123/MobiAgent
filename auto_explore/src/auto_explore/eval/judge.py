from __future__ import annotations

import base64
import json
import mimetypes
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List

from openai import OpenAI

from auto_explore.eval.loader import render_trace_for_prompt


JUDGE_MODE_LEGACY_TEXT = "legacy_text"
JUDGE_MODE_PATH_MULTIMODAL = "path_multimodal"

LEGACY_MAIN_SCORE_FIELDS = (
    "trajectory_completeness_score",
    "reasoning_quality_score",
)
LEGACY_SUBSCORE_FIELDS = (
    "goal_coverage",
    "step_coherence",
    "action_reason_alignment",
    "groundedness",
)
PATH_MULTIMODAL_MAIN_SCORE_FIELDS = (
    "trajectory_completeness_score",
    "image_coherence_score",
    "task_operation_match_score",
)
PATH_MULTIMODAL_SUBSCORE_FIELDS = (
    "goal_coverage",
    "visual_action_alignment",
    "click_target_grounding",
    "inter_image_continuity",
    "termination_quality",
)

PROMPT_ROOT = Path(__file__).resolve().parents[3] / "eval"


def _extract_response_text(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", "") if message is not None else ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: List[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    chunks.append(str(item.get("text", "")))
            return "\n".join(chunk for chunk in chunks if chunk)
    return ""


def _extract_json_object_text(text: str) -> str:
    stripped = str(text or "").strip()
    if not stripped:
        return stripped
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("{")
    if start < 0:
        return stripped
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(stripped)):
        ch = stripped[idx]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return stripped[start : idx + 1]
    return stripped[start:]


def _json_loads_relaxed(text: str) -> Any:
    cleaned = re.sub(r",\s*([}\]])", r"\1", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    repaired = cleaned
    if repaired.count('"') % 2 == 1:
        repaired += '"'
    repaired += "]" * max(0, repaired.count("[") - repaired.count("]"))
    repaired += "}" * max(0, repaired.count("{") - repaired.count("}"))
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    return json.loads(repaired)


def _regex_score_payload(text: str, *, judge_mode: str) -> Dict[str, Any]:
    score_names = (
        (*PATH_MULTIMODAL_MAIN_SCORE_FIELDS, *PATH_MULTIMODAL_SUBSCORE_FIELDS)
        if judge_mode == JUDGE_MODE_PATH_MULTIMODAL
        else (*LEGACY_MAIN_SCORE_FIELDS, *LEGACY_SUBSCORE_FIELDS)
    )
    extracted: Dict[str, int] = {}
    for name in score_names:
        match = re.search(rf'"{re.escape(name)}"\s*:\s*(-?\d+(?:\.\d+)?)', text)
        if match:
            extracted[name] = _clamp_score(match.group(1), name)

    required = PATH_MULTIMODAL_MAIN_SCORE_FIELDS if judge_mode == JUDGE_MODE_PATH_MULTIMODAL else LEGACY_MAIN_SCORE_FIELDS
    missing_required = [name for name in required if name not in extracted]
    if missing_required:
        raise ValueError(f"Judge response is missing required scores after relaxed parsing: {missing_required}")

    payload: Dict[str, Any] = {name: extracted[name] for name in required}
    payload["subscores"] = {name: extracted[name] for name in score_names if name in extracted and name not in required}

    summary_match = re.search(r'"summary"\s*:\s*"((?:\\.|[^"\\])*)', text, flags=re.DOTALL)
    if summary_match:
        try:
            payload["summary"] = json.loads(f'"{summary_match.group(1)}"')
        except Exception:
            payload["summary"] = summary_match.group(1).strip()
    else:
        payload["summary"] = "Judge returned parseable scores but no complete summary."
    payload["strengths"] = ["Scores recovered from a malformed judge response."]
    payload["issues"] = ["Judge response was not valid JSON; non-score fields may be incomplete."]
    return payload


def _load_judge_payload(raw_text: str, *, judge_mode: str) -> Dict[str, Any]:
    object_text = _extract_json_object_text(raw_text)
    try:
        payload = _json_loads_relaxed(object_text)
    except Exception:
        payload = _regex_score_payload(object_text or raw_text, judge_mode=judge_mode)
    if not isinstance(payload, dict):
        raise ValueError(f"Judge response is not a JSON object: {raw_text}")
    return payload


def _clamp_score(value: Any, field_name: str) -> int:
    try:
        score = int(round(float(value)))
    except Exception as exc:
        raise ValueError(f"Invalid score for {field_name}: {value}") from exc
    return min(10, max(1, score))


def _normalize_list(value: Any, *, fallback: str) -> List[str]:
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        if items:
            return items[:4]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return [fallback]


def _compute_overall_score(payload: Mapping[str, Any]) -> int:
    completeness = _clamp_score(payload.get("trajectory_completeness_score", 1), "trajectory_completeness_score")
    quality = _clamp_score(payload.get("reasoning_quality_score", 1), "reasoning_quality_score")
    weighted = completeness * 0.55 + quality * 0.45
    return int(round(weighted))


@lru_cache(maxsize=None)
def _load_prompt_template(filename: str) -> str:
    return (PROMPT_ROOT / filename).read_text(encoding="utf-8").strip()


def _render_prompt_template(filename: str, replacements: Mapping[str, Any]) -> str:
    content = _load_prompt_template(filename)
    for key, value in replacements.items():
        content = content.replace(f"{{{{{key}}}}}", str(value))
    return content


def _truncate_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _encode_image_as_data_url(path: str | Path) -> str:
    image_path = Path(path)
    suffix = image_path.suffix.lower()
    mime_type = mimetypes.types_map.get(suffix, "") or "image/jpeg"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _normalize_judge_payload(payload: Mapping[str, Any], *, judge_mode: str) -> Dict[str, Any]:
    if judge_mode == JUDGE_MODE_PATH_MULTIMODAL:
        expected_subscores = PATH_MULTIMODAL_SUBSCORE_FIELDS
        normalized_subscores = {}
        raw_subscores = payload.get("subscores", {})
        subscores = raw_subscores if isinstance(raw_subscores, Mapping) else {}
        for field_name in expected_subscores:
            normalized_subscores[field_name] = _clamp_score(subscores.get(field_name, 1), field_name)
        return {
            "trajectory_completeness_score": _clamp_score(
                payload.get("trajectory_completeness_score", 1),
                "trajectory_completeness_score",
            ),
            "image_coherence_score": _clamp_score(
                payload.get("image_coherence_score", 1),
                "image_coherence_score",
            ),
            "task_operation_match_score": _clamp_score(
                payload.get("task_operation_match_score", 1),
                "task_operation_match_score",
            ),
            "subscores": normalized_subscores,
            "strengths": _normalize_list(payload.get("strengths"), fallback="No clear strengths provided."),
            "issues": _normalize_list(payload.get("issues"), fallback="No concrete issues provided."),
            "summary": str(payload.get("summary", "")).strip() or "No summary provided.",
        }

    normalized_subscores = {}
    raw_subscores = payload.get("subscores", {})
    subscores = raw_subscores if isinstance(raw_subscores, Mapping) else {}
    for field_name in LEGACY_SUBSCORE_FIELDS:
        normalized_subscores[field_name] = _clamp_score(subscores.get(field_name, 1), field_name)

    normalized = {
        "trajectory_completeness_score": _clamp_score(
            payload.get("trajectory_completeness_score", 1),
            "trajectory_completeness_score",
        ),
        "reasoning_quality_score": _clamp_score(
            payload.get("reasoning_quality_score", 1),
            "reasoning_quality_score",
        ),
        "subscores": normalized_subscores,
        "strengths": _normalize_list(payload.get("strengths"), fallback="No clear strengths provided."),
        "issues": _normalize_list(payload.get("issues"), fallback="No concrete issues provided."),
        "summary": str(payload.get("summary", "")).strip() or "No summary provided.",
    }
    normalized["overall_score"] = _compute_overall_score(normalized)
    return normalized


def _build_legacy_messages(
    sample: Mapping[str, Any],
    *,
    max_steps: int,
    max_reasoning_chars: int,
) -> List[Dict[str, Any]]:
    trace_text = render_trace_for_prompt(
        dict(sample),
        max_steps=max_steps,
        max_reasoning_chars=max_reasoning_chars,
    )
    replacements = {
        "APP_NAME": sample.get("app_name", ""),
        "TASK_TYPE": sample.get("task_type", ""),
        "SAMPLE_KIND": sample.get("sample_kind", ""),
        "TASK_DESCRIPTION": sample.get("task_description", ""),
        "ACTION_COUNT": sample.get("action_count", 0),
        "REASONING_COUNT": sample.get("reasoning_count", 0),
        "STATS_JSON": json.dumps(sample.get("stats", {}), ensure_ascii=False),
        "TRACE_TEXT": trace_text or "(empty trace)",
    }
    return [
        {
            "role": "system",
            "content": _render_prompt_template("legacy_text_system_prompt.md", replacements),
        },
        {
            "role": "user",
            "content": _render_prompt_template("legacy_text_user_prompt.md", replacements),
        },
    ]


def _render_multimodal_step_context(step: Mapping[str, Any], *, max_reasoning_chars: int) -> str:
    lines = [
        f"[Step {step.get('image_index', step.get('step_index', ''))}]",
        f"step_index: {step.get('step_index', '')}",
        f"action_type: {step.get('action_type', '') or '(empty)'}",
        f"status: {step.get('status', '') or '(empty)'}",
        f"reasoning: {_truncate_text(step.get('reasoning', ''), max_reasoning_chars) or '(empty)'}",
    ]
    if step.get("target_element"):
        lines.append(f"target_element: {step.get('target_element')}")
    if step.get("bounds"):
        lines.append(f"bounds: {step.get('bounds')}")
    if step.get("position_x") is not None and step.get("position_y") is not None:
        lines.append(f"position: [{step.get('position_x')}, {step.get('position_y')}]")
    if step.get("has_click_point_image"):
        lines.append("visual_evidence: original screenshot followed by click-point screenshot.")
    else:
        lines.append("visual_evidence: original screenshot only; click-point screenshot is missing.")
    lines.append("Read this step text first, inspect the original screenshot, then inspect the click-point screenshot if present.")
    return "\n".join(lines)


def _render_terminal_step_context(step: Mapping[str, Any], *, max_reasoning_chars: int) -> str:
    return "\n".join(
        [
            "[Terminal Step Without Screenshot]",
            f"step_index: {step.get('step_index', '')}",
            f"action_type: {step.get('action_type', '') or '(empty)'}",
            f"status: {step.get('status', '') or '(empty)'}",
            f"reasoning: {_truncate_text(step.get('reasoning', ''), max_reasoning_chars) or '(empty)'}",
        ]
    )


def _build_path_multimodal_messages(
    sample: Mapping[str, Any],
    *,
    max_reasoning_chars: int,
) -> List[Dict[str, Any]]:
    image_step_records = sample.get("image_step_records", [])
    if not isinstance(image_step_records, list) or not image_step_records:
        raise ValueError(f"Sample {sample.get('sample_id', '')} has no multimodal step/image pairs")

    replacements = {
        "APP_NAME": sample.get("app_name", ""),
        "TASK_TYPE": sample.get("task_type", ""),
        "SAMPLE_ID": sample.get("sample_id", ""),
        "TASK_DESCRIPTION": sample.get("task_description", ""),
        "ACTION_COUNT": sample.get("action_count", 0),
        "STEP_COUNT": sample.get("step_count", 0),
        "IMAGE_COUNT": sample.get("image_count", 0),
        "ENDS_WITH_DONE": sample.get("ends_with_done", False),
        "CONFIGURED_DEPTH_LIMIT": sample.get("configured_depth_limit", ""),
        "CONFIGURED_BREADTH": sample.get("configured_breadth", ""),
        "STATS_JSON": json.dumps(sample.get("stats", {}), ensure_ascii=False),
    }
    user_text = _render_prompt_template("path_multimodal_user_prompt.md", replacements)
    content: List[Dict[str, Any]] = [{"type": "text", "text": user_text}]

    for step in image_step_records:
        content.append(
            {
                "type": "text",
                "text": _render_multimodal_step_context(step, max_reasoning_chars=max_reasoning_chars),
            }
        )
        content.append({"type": "text", "text": f"[Step {step.get('image_index', '')} Original Screenshot]"})
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": _encode_image_as_data_url(step["image_path"]),
                },
            }
        )
        click_point_image_path = str(step.get("click_point_image_path", "") or "").strip()
        if click_point_image_path:
            content.append({"type": "text", "text": f"[Step {step.get('image_index', '')} Click-Point Screenshot]"})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": _encode_image_as_data_url(click_point_image_path),
                    },
                }
            )

    terminal_step_records = sample.get("terminal_step_records", [])
    if isinstance(terminal_step_records, list):
        for step in terminal_step_records:
            if isinstance(step, Mapping):
                content.append(
                    {
                        "type": "text",
                        "text": _render_terminal_step_context(step, max_reasoning_chars=max_reasoning_chars),
                    }
                )

    return [
        {
            "role": "system",
            "content": _render_prompt_template("path_multimodal_system_prompt.md", replacements),
        },
        {
            "role": "user",
            "content": content,
        },
    ]


def build_evaluation_messages(
    sample: Mapping[str, Any],
    *,
    judge_mode: str = JUDGE_MODE_LEGACY_TEXT,
    max_steps: int = 30,
    max_reasoning_chars: int = 320,
) -> List[Dict[str, Any]]:
    if judge_mode == JUDGE_MODE_PATH_MULTIMODAL:
        return _build_path_multimodal_messages(sample, max_reasoning_chars=max_reasoning_chars)
    return _build_legacy_messages(sample, max_steps=max_steps, max_reasoning_chars=max_reasoning_chars)


class LLMTrajectoryJudge:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        judge_mode: str = JUDGE_MODE_LEGACY_TEXT,
        temperature: float = 0.0,
        max_tokens: int = 700,
        enable_thinking: bool = True,
        max_steps: int = 30,
        max_reasoning_chars: int = 320,
    ) -> None:
        self.client = OpenAI(base_url=base_url, api_key=api_key or "EMPTY")
        self.base_url = base_url
        self.model = model
        self.judge_mode = judge_mode
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.enable_thinking = enable_thinking
        self.max_steps = max_steps
        self.max_reasoning_chars = max_reasoning_chars

    def evaluate(self, sample: Mapping[str, Any]) -> Dict[str, Any]:
        messages = build_evaluation_messages(
            sample,
            judge_mode=self.judge_mode,
            max_steps=self.max_steps,
            max_reasoning_chars=self.max_reasoning_chars,
        )
        request_kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": self.enable_thinking}},
        }
        if "qwen" not in self.model.lower():
            request_kwargs["response_format"] = {"type": "json_object"}
        response = self.client.chat.completions.create(**request_kwargs)
        raw_text = _extract_response_text(response).strip()
        payload = _load_judge_payload(raw_text, judge_mode=self.judge_mode)
        normalized = _normalize_judge_payload(payload, judge_mode=self.judge_mode)
        normalized.update(
            {
                "sample_id": sample.get("sample_id", ""),
                "sample_kind": sample.get("sample_kind", ""),
                "trace_dir": sample.get("trace_dir", ""),
                "app_name": sample.get("app_name", ""),
                "task_description": sample.get("task_description", ""),
                "action_count": sample.get("action_count", 0),
                "step_count": sample.get("step_count", 0),
                "image_count": sample.get("image_count", 0),
                "judge_mode": self.judge_mode,
                "evaluated_at": datetime.now().isoformat(timespec="seconds"),
                "judge_model": self.model,
                "judge_base_url": self.base_url,
            }
        )
        return normalized


def summarize_batch_results(
    *,
    input_path: str,
    target_level: str,
    judge_model: str,
    judge_base_url: str,
    judge_mode: str = JUDGE_MODE_LEGACY_TEXT,
    results: Iterable[Mapping[str, Any]],
    interrupted: bool = False,
    planned_sample_count: int | None = None,
) -> Dict[str, Any]:
    result_list = [dict(item) for item in results]
    successful = [item for item in result_list if item.get("status") != "error"]
    planned_count = len(result_list) if planned_sample_count is None else int(planned_sample_count)
    summary = {
        "input_path": input_path,
        "target_level": target_level,
        "judge_model": judge_model,
        "judge_base_url": judge_base_url,
        "judge_mode": judge_mode,
        "evaluated_at": datetime.now().isoformat(timespec="seconds"),
        "interrupted": bool(interrupted),
        "planned_sample_count": planned_count,
        "evaluated_sample_count": len(result_list),
        "remaining_sample_count": max(0, planned_count - len(result_list)),
        "sample_count": len(result_list),
        "success_count": len(successful),
        "error_count": len(result_list) - len(successful),
        "averages": {},
        "samples": result_list,
    }
    if successful:
        fields = PATH_MULTIMODAL_MAIN_SCORE_FIELDS if judge_mode == JUDGE_MODE_PATH_MULTIMODAL else (*LEGACY_MAIN_SCORE_FIELDS, "overall_score")
        for field_name in fields:
            summary["averages"][field_name] = round(mean(float(item[field_name]) for item in successful), 3)
        subscore_fields = PATH_MULTIMODAL_SUBSCORE_FIELDS if judge_mode == JUDGE_MODE_PATH_MULTIMODAL else LEGACY_SUBSCORE_FIELDS
        for field_name in subscore_fields:
            values = [
                float(subscores[field_name])
                for item in successful
                for subscores in [item.get("subscores", {})]
                if isinstance(subscores, Mapping) and field_name in subscores
            ]
            if values:
                summary["averages"][field_name] = round(mean(values), 3)
    return summary
