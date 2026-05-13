from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Dict, List, Optional


NUMBERED_IMAGE_PATTERN = re.compile(r"^(?P<index>\d+)\.(jpg|jpeg|png)$", re.IGNORECASE)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_trace_dir(path: Path) -> bool:
    return path.is_dir() and path.joinpath("actions.json").exists() and path.joinpath("react.json").exists()


def _is_trace_name(path: Path, prefix: str) -> bool:
    return path.is_dir() and path.name.startswith(prefix) and _is_trace_dir(path)


def _sorted_trace_dirs(container: Path, *, prefix: str) -> List[Path]:
    if not container.is_dir():
        return []
    return sorted((child.resolve() for child in container.iterdir() if _is_trace_name(child, prefix)), key=lambda item: item.name)


def _dedupe_paths(paths: Iterable[Path]) -> List[Path]:
    seen = set()
    unique: List[Path] = []
    for path in paths:
        resolved = path.resolve()
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(resolved)
    return unique


def _candidate_trace_containers(root: Path, level: str) -> List[Path]:
    if level not in {"paths", "steps"}:
        raise ValueError(f"Unsupported target_level: {level}")
    containers: List[Path] = []
    if root.name == level:
        containers.append(root)
    else:
        containers.append(root / level)
        containers.append(root / "data" / level)
    return _dedupe_paths(containers)


def _trace_dirs_for_level(root: Path, *, level: str) -> List[Path]:
    prefix = "path_" if level == "paths" else "step_"
    traces: List[Path] = []
    for container in _candidate_trace_containers(root, level):
        traces.extend(_sorted_trace_dirs(container, prefix=prefix))
    return _dedupe_paths(traces)


def collect_trace_dirs(
    input_path: str | Path,
    *,
    target_level: str = "auto",
    max_samples: int = 0,
    allow_auto_step_fallback: bool = True,
) -> List[Path]:
    root = Path(input_path).resolve()
    if not root.exists():
        raise FileNotFoundError(f"Input path does not exist: {root}")

    if _is_trace_dir(root):
        traces = [root]
    elif target_level == "paths":
        traces = _trace_dirs_for_level(root, level="paths")
    elif target_level == "steps":
        traces = _trace_dirs_for_level(root, level="steps")
    elif target_level == "auto":
        traces = _trace_dirs_for_level(root, level="paths")
        if not traces and allow_auto_step_fallback:
            traces = _trace_dirs_for_level(root, level="steps")
    else:
        raise ValueError(f"Unsupported target_level: {target_level}")

    if max_samples > 0:
        traces = traces[:max_samples]
    return traces


def _resolve_run_dir(trace_dir: Path) -> Optional[Path]:
    for candidate in [trace_dir, *trace_dir.parents]:
        if candidate.joinpath("metrics.json").exists():
            return candidate
    return None


def _normalize_reacts(payload: Any) -> List[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("reacts", "items", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
    return []


def _coerce_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def _truncate_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _extract_configured_depth_limit(metrics_payload: Mapping[str, Any]) -> Optional[int]:
    for key in ("configured_depth_limit", "depth_limit", "depth"):
        value = _coerce_int(metrics_payload.get(key))
        if value is not None:
            return value
    return None


def _extract_configured_breadth(metrics_payload: Mapping[str, Any]) -> Optional[int]:
    for key in ("configured_breadth", "breadth"):
        value = _coerce_int(metrics_payload.get(key))
        if value is not None:
            return value
    return None


def _build_step_records(actions: List[Mapping[str, Any]], reacts: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    react_by_index: Dict[int, Mapping[str, Any]] = {}
    for offset, react in enumerate(reacts, start=1):
        action_index = _coerce_int(react.get("action_index")) or offset
        react_by_index[action_index] = react

    records: List[Dict[str, Any]] = []
    for offset, action in enumerate(actions, start=1):
        action_index = _coerce_int(action.get("action_index")) or offset
        react = react_by_index.get(action_index, {})
        function_payload = react.get("function", {}) if isinstance(react, Mapping) else {}
        function_name = ""
        parameters: Mapping[str, Any] = {}
        if isinstance(function_payload, Mapping):
            function_name = str(function_payload.get("name", "")).strip()
            raw_parameters = function_payload.get("parameters", {})
            if isinstance(raw_parameters, Mapping):
                parameters = raw_parameters

        action_type = str(action.get("type") or function_name or "").strip()
        status = str(action.get("status") or parameters.get("status") or "").strip()
        record = {
            "step_index": action_index,
            "action_type": action_type,
            "status": status,
            "reasoning": str(react.get("reasoning", "")).strip() if isinstance(react, Mapping) else "",
            "target_element": str(parameters.get("target_element", "")).strip(),
            "position_x": action.get("position_x"),
            "position_y": action.get("position_y"),
            "bounds": action.get("bounds") if isinstance(action.get("bounds"), list) else parameters.get("bbox"),
        }
        records.append(record)
    return records


def _load_numbered_images(trace_dir: Path, *, require_contiguous: bool = True) -> List[Path]:
    numbered: List[tuple[int, Path]] = []
    for child in trace_dir.iterdir():
        if not child.is_file():
            continue
        match = NUMBERED_IMAGE_PATTERN.match(child.name)
        if not match:
            continue
        numbered.append((int(match.group("index")), child.resolve()))
    if not numbered:
        return []
    numbered.sort(key=lambda item: item[0])
    if not require_contiguous:
        return [path for _, path in numbered]
    ordered: List[Path] = []
    expected = 1
    for index, path in numbered:
        if index != expected:
            raise ValueError(f"Missing numbered screenshot {expected}.jpg before {path.name} in {trace_dir}")
        ordered.append(path)
        expected += 1
    return ordered


def _find_click_point_image(image_path: Path) -> Optional[Path]:
    candidates = [
        image_path.with_name(f"{image_path.stem}_click_point{suffix}")
        for suffix in (image_path.suffix, ".jpg", ".jpeg", ".png")
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return None


def _attach_path_multimodal_fields(sample: Dict[str, Any], trace_dir: Path) -> None:
    if sample["sample_kind"] != "path":
        raise ValueError(
            f"path_multimodal mode only supports path_* samples, but received {sample['sample_id']}"
        )

    image_paths = _load_numbered_images(trace_dir, require_contiguous=True)
    if not image_paths:
        raise ValueError(
            f"path_multimodal sample {sample['sample_id']} has no numbered screenshots; expected 1.jpg, 2.jpg, ..."
        )

    configured_depth_limit = sample.get("configured_depth_limit")
    configured_breadth = sample.get("configured_breadth")
    if configured_depth_limit is None:
        raise ValueError(
            f"path_multimodal sample {sample['sample_id']} is missing configured_depth_limit in metrics.json"
        )
    if configured_breadth is None:
        raise ValueError(
            f"path_multimodal sample {sample['sample_id']} is missing configured_breadth in metrics.json"
        )

    step_records = list(sample.get("steps", []))
    if len(step_records) < len(image_paths):
        raise ValueError(
            f"path_multimodal sample {sample['sample_id']} has {len(image_paths)} screenshots but only {len(step_records)} actions"
        )

    image_step_records: List[Dict[str, Any]] = []
    for image_index, image_path in enumerate(image_paths, start=1):
        step_record = dict(step_records[image_index - 1])
        click_point_path = _find_click_point_image(image_path)
        step_record["image_index"] = image_index
        step_record["image_path"] = str(image_path)
        step_record["click_point_image_path"] = str(click_point_path) if click_point_path is not None else ""
        step_record["has_click_point_image"] = click_point_path is not None
        image_step_records.append(step_record)

    terminal_step_records = [dict(item) for item in step_records[len(image_paths) :]]
    for item in terminal_step_records:
        if item.get("action_type") != "done":
            raise ValueError(
                f"path_multimodal sample {sample['sample_id']} is missing a screenshot for non-terminal step {item.get('step_index')}"
            )

    sample["image_paths"] = [str(path) for path in image_paths]
    sample["image_count"] = len(image_paths)
    sample["image_step_records"] = image_step_records
    sample["terminal_step_records"] = terminal_step_records
    sample["stats"]["image_count"] = len(image_paths)


def load_trace_sample(
    trace_dir: str | Path,
    *,
    judge_mode: str = "legacy_text",
) -> Dict[str, Any]:
    trace_path = Path(trace_dir).resolve()
    if not _is_trace_dir(trace_path):
        raise FileNotFoundError(f"Trace directory is missing actions.json/react.json: {trace_path}")

    actions_payload = _read_json(trace_path / "actions.json")
    reacts_payload = _read_json(trace_path / "react.json")
    if not isinstance(actions_payload, Mapping):
        raise ValueError(f"actions.json must be a JSON object: {trace_path / 'actions.json'}")

    action_items = actions_payload.get("actions", [])
    if not isinstance(action_items, list):
        raise ValueError(f"actions.json field 'actions' must be a list: {trace_path / 'actions.json'}")
    action_records = [item for item in action_items if isinstance(item, Mapping)]
    react_records = _normalize_reacts(reacts_payload)
    step_records = _build_step_records(action_records, react_records)

    run_dir = _resolve_run_dir(trace_path)
    metrics_payload: Dict[str, Any] = {}
    if run_dir is not None:
        metrics_path = run_dir / "metrics.json"
        if metrics_path.exists():
            loaded_metrics = _read_json(metrics_path)
            if isinstance(loaded_metrics, Mapping):
                metrics_payload = dict(loaded_metrics)

    configured_depth_limit = _extract_configured_depth_limit(metrics_payload)
    configured_breadth = _extract_configured_breadth(metrics_payload)
    ends_with_done = bool(step_records and step_records[-1].get("action_type") == "done")
    reasoning_count = sum(1 for item in step_records if item.get("reasoning"))
    sample_kind = "path" if trace_path.name.startswith("path_") else "step"

    sample: Dict[str, Any] = {
        "sample_id": trace_path.name,
        "sample_kind": sample_kind,
        "trace_dir": str(trace_path),
        "run_dir": str(run_dir) if run_dir is not None else "",
        "metrics_path": str(run_dir / "metrics.json") if run_dir is not None and run_dir.joinpath("metrics.json").exists() else "",
        "app_name": str(actions_payload.get("app_name", "")).strip(),
        "task_type": str(actions_payload.get("task_type", "")).strip(),
        "task_description": str(actions_payload.get("task_description", "")).strip(),
        "action_count": len(action_records),
        "reasoning_count": reasoning_count,
        "step_count": len(step_records),
        "steps": step_records,
        "metrics": metrics_payload,
        "configured_depth_limit": configured_depth_limit,
        "configured_breadth": configured_breadth,
        "ends_with_done": ends_with_done,
        "stats": {
            "has_done": any(item.get("action_type") == "done" for item in step_records),
            "ends_with_done": ends_with_done,
            "configured_depth_limit": configured_depth_limit,
            "configured_breadth": configured_breadth,
        },
    }

    image_paths = _load_numbered_images(trace_path, require_contiguous=False) if sample_kind == "path" else []
    sample["image_paths"] = [str(path) for path in image_paths]
    sample["image_count"] = len(image_paths)
    if image_paths:
        sample["stats"]["image_count"] = len(image_paths)

    if judge_mode == "path_multimodal":
        _attach_path_multimodal_fields(sample, trace_path)

    return sample


def render_trace_for_prompt(
    sample: Mapping[str, Any],
    *,
    max_steps: int = 30,
    max_reasoning_chars: int = 320,
) -> str:
    steps = sample.get("steps", [])
    if not isinstance(steps, list) or not steps:
        return ""

    rendered: List[str] = []
    for index, step in enumerate(steps[:max_steps], start=1):
        if not isinstance(step, Mapping):
            continue
        rendered.append(f"[Step {index}]")
        rendered.append(f"step_index: {step.get('step_index', index)}")
        rendered.append(f"action_type: {step.get('action_type', '')}")
        if step.get("status"):
            rendered.append(f"status: {step.get('status')}")
        if step.get("target_element"):
            rendered.append(f"target_element: {step.get('target_element')}")
        if step.get("bounds"):
            rendered.append(f"bounds: {step.get('bounds')}")
        if step.get("position_x") is not None and step.get("position_y") is not None:
            rendered.append(f"position: [{step.get('position_x')}, {step.get('position_y')}]")
        reasoning = _truncate_text(step.get("reasoning"), max_reasoning_chars)
        if reasoning:
            rendered.append(f"reasoning: {reasoning}")
        rendered.append("")

    if len(steps) > max_steps:
        rendered.append(f"... truncated {len(steps) - max_steps} additional step(s)")

    return "\n".join(rendered).strip()
