from __future__ import annotations

import argparse
import base64
import collections
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _src_root() -> Path:
    return _repo_root() / "auto_explore" / "src"


if str(_src_root()) not in sys.path:
    sys.path.insert(0, str(_src_root()))

from auto_explore.core.dfs import init_explorer_client
from auto_explore.core.explorer import _build_response_diagnostics, _parse_explorer_response_content
from auto_explore.core.prompting import build_explorer_prompt


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone Explorer output probe for a saved screenshot + hierarchy")
    parser.add_argument("--image", type=str, default="", help="Path to screenshot image")
    parser.add_argument("--hierarchy", type=str, default="", help="Path to hierarchy xml/json")
    parser.add_argument(
        "--step-dir",
        type=str,
        default="",
        help="Directory containing one screenshot (.jpg) and one hierarchy (.xml or .json)",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=os.getenv("AUTO_EXPLORE_EXPLORER_BASE_URL", "https://openrouter.ai/api/v1"),
        help="Explorer provider base URL",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default="",
        help="Explorer API key, defaults to OPENROUTER_API_KEY",
    )
    parser.add_argument("--model", type=str, default="Qwen3.5-35B-A3B", help="Explorer model name")
    parser.add_argument("--depth", type=int, default=1, help="Depth passed into build_explorer_prompt")
    parser.add_argument("--breadth", type=int, default=3, help="Breadth passed into build_explorer_prompt")
    parser.add_argument(
        "--history-task",
        action="append",
        default=[],
        help="Prior action source_task, can be repeated to simulate action history",
    )
    parser.add_argument(
        "--history-type",
        type=str,
        default="click",
        help="Action type used for each --history-task entry",
    )
    parser.add_argument("--max-tokens", type=int, default=1024, help="max_tokens sent to Explorer")
    parser.add_argument("--temperature", type=float, default=0.0, help="temperature sent to Explorer")
    parser.add_argument("--timeout", type=float, default=45, help="timeout sent to Explorer")
    parser.add_argument("--repeat", type=int, default=1, help="Number of repeated Explorer calls to run")
    parser.add_argument(
        "--disable-thinking",
        action="store_true",
        help="Send chat_template_kwargs.enable_thinking=false for Qwen reasoning models",
    )
    parser.add_argument(
        "--response-format",
        type=str,
        choices=["none", "json_object"],
        default="none",
        help="Optional OpenAI-compatible response_format to request from Explorer",
    )
    parser.add_argument("--print-prompt", action="store_true", help="Print the built Explorer prompt before sending")
    parser.add_argument("--save-raw", type=str, default="", help="Optional file path to save raw Explorer output")
    return parser


def _pick_single_file(step_dir: Path, patterns: list[str], label: str) -> Path:
    matches: list[Path] = []
    for pattern in patterns:
        matches.extend(sorted(step_dir.glob(pattern)))
    files = [path for path in matches if path.is_file()]
    if not files:
        raise FileNotFoundError(f"No {label} found in {step_dir}")
    if len(files) > 1:
        names = ", ".join(path.name for path in files[:5])
        raise ValueError(f"Multiple {label} files found in {step_dir}: {names}")
    return files[0]


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    if args.step_dir:
        step_dir = Path(args.step_dir)
        if not step_dir.is_dir():
            raise FileNotFoundError(f"step-dir not found: {step_dir}")
        preferred_hierarchies = [
            path
            for path in sorted(step_dir.glob("*"))
            if path.is_file()
            and path.suffix.lower() in {".xml", ".json"}
            and path.stem.isdigit()
        ]
        if len(preferred_hierarchies) == 1:
            hierarchy_path = preferred_hierarchies[0]
        elif len(preferred_hierarchies) > 1:
            names = ", ".join(path.name for path in preferred_hierarchies[:5])
            raise ValueError(f"Multiple preferred hierarchy files found in {step_dir}: {names}")
        else:
            hierarchy_path = _pick_single_file(step_dir, ["*.xml", "*.json"], "hierarchy")
        hierarchy_stem = hierarchy_path.stem

        exact_image_candidates = [
            path
            for suffix in (".jpg", ".jpeg", ".png")
            for path in [step_dir / f"{hierarchy_stem}{suffix}"]
            if path.is_file()
        ]
        if len(exact_image_candidates) == 1:
            return exact_image_candidates[0], hierarchy_path
        if len(exact_image_candidates) > 1:
            names = ", ".join(path.name for path in exact_image_candidates)
            raise ValueError(f"Multiple exact-match image files found in {step_dir}: {names}")

        preferred_images = [
            path
            for path in sorted(step_dir.glob("*"))
            if path.is_file()
            and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
            and "_" not in path.stem
        ]
        if len(preferred_images) == 1:
            return preferred_images[0], hierarchy_path
        if len(preferred_images) > 1:
            names = ", ".join(path.name for path in preferred_images[:5])
            raise ValueError(f"Multiple preferred image files found in {step_dir}: {names}")

        image_path = _pick_single_file(step_dir, ["*.jpg", "*.jpeg", "*.png"], "image")
        return image_path, hierarchy_path

    if not args.image or not args.hierarchy:
        raise ValueError("Please provide --step-dir or both --image and --hierarchy")
    return Path(args.image), Path(args.hierarchy)


def _build_action_history(args: argparse.Namespace) -> list[dict[str, Any]]:
    return [{"source_task": task, "type": args.history_type} for task in args.history_task]


def _classify_candidate(candidate: Any) -> str:
    if not isinstance(candidate, dict):
        return "invalid_candidate_type"
    task = candidate.get("single_step_task")
    if not isinstance(task, str) or not task.strip():
        return "missing_single_step_task"
    stripped = task.strip()
    if len(stripped) <= 2:
        return "likely_incomplete_task"
    return "ok"


def _classify_parsed_output(parsed: Any) -> str:
    if not isinstance(parsed, dict):
        return "invalid_root"
    candidates = parsed.get("candidates")
    if not isinstance(candidates, list):
        return "invalid_candidates"
    if not candidates:
        return "empty_candidates"
    candidate_statuses = [_classify_candidate(candidate) for candidate in candidates]
    if all(status == "ok" for status in candidate_statuses):
        return "ok"
    if any(status == "likely_incomplete_task" for status in candidate_statuses):
        return "content_incomplete"
    return "content_anomaly"


def main() -> None:
    args = create_parser().parse_args()
    if not args.api_key:
        raise ValueError("Please provide --api-key or set OPENROUTER_API_KEY")
    if args.repeat < 1:
        raise ValueError("--repeat must be >= 1")

    image_path, hierarchy_path = _resolve_paths(args)
    screenshot_b64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    hierarchy_text = hierarchy_path.read_text(encoding="utf-8")
    action_history = _build_action_history(args)

    prompt = build_explorer_prompt(
        depth=args.depth,
        breadth=args.breadth,
        hierarchy_text=hierarchy_text,
        action_history=action_history,
    )
    if args.print_prompt:
        print("----- PROMPT START -----")
        print(prompt)
        print("----- PROMPT END -----")

    client = init_explorer_client(args.base_url, args.api_key)
    print(f"image_path = {image_path}")
    print(f"hierarchy_path = {hierarchy_path}")
    print(f"repeat = {args.repeat}")
    print(f"temperature = {args.temperature}")
    print(f"response_format = {args.response_format}")

    counts: collections.Counter[str] = collections.Counter()
    saved_raw_paths: list[str] = []
    latencies: list[float] = []

    for run_idx in range(1, args.repeat + 1):
        request_kwargs: dict[str, Any] = {
            "model": args.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"}},
                    ],
                }
            ],
            "timeout": args.timeout,
            "max_tokens": args.max_tokens,
            "temperature": args.temperature,
        }
        if args.response_format == "json_object":
            request_kwargs["response_format"] = {"type": "json_object"}
        if args.disable_thinking:
            request_kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

        request_started = time.perf_counter()
        response = client.chat.completions.create(**request_kwargs)
        elapsed_sec = time.perf_counter() - request_started
        latencies.append(elapsed_sec)

        choice = response.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        content = choice.message.content or ""
        diagnostics = _build_response_diagnostics(content, finish_reason=finish_reason)

        print(f"===== RUN {run_idx}/{args.repeat} =====")
        print(f"elapsed_sec = {elapsed_sec:.3f}")
        print(f"finish_reason = {finish_reason}")
        print(f"content_length = {len(content)}")
        print("diagnostics =")
        print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
        print("----- RAW OUTPUT START -----")
        print(content)
        print("----- RAW OUTPUT END -----")

        if args.save_raw:
            target = Path(args.save_raw)
            if args.repeat > 1:
                target = target.with_name(f"{target.stem}_run_{run_idx:03d}{target.suffix or '.txt'}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            saved_raw_paths.append(str(target))
            print(f"saved_raw = {target}")

        try:
            parsed = _parse_explorer_response_content(content, finish_reason=finish_reason, attempt=1)
            classification = _classify_parsed_output(parsed)
            counts[classification] += 1
            print(f"classification = {classification}")
            print("----- PARSED JSON START -----")
            print(json.dumps(parsed, ensure_ascii=False, indent=2))
            print("----- PARSED JSON END -----")
        except Exception as exc:
            classification = "parse_error"
            counts[classification] += 1
            print(f"classification = {classification}")
            print(f"parse_error = {exc}")

    if args.repeat > 1:
        print("===== SUMMARY =====")
        print(json.dumps(dict(sorted(counts.items())), ensure_ascii=False, indent=2))
        sorted_latencies = sorted(latencies)
        latency_summary = {
            "count": len(sorted_latencies),
            "avg_sec": sum(sorted_latencies) / len(sorted_latencies),
            "min_sec": sorted_latencies[0],
            "p50_sec": sorted_latencies[len(sorted_latencies) // 2],
            "p90_sec": sorted_latencies[max(0, int(len(sorted_latencies) * 0.9) - 1)],
            "max_sec": sorted_latencies[-1],
        }
        print("latency_summary =")
        print(json.dumps(latency_summary, ensure_ascii=False, indent=2))
        if saved_raw_paths:
            print("saved_raw_files =")
            print(json.dumps(saved_raw_paths, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
