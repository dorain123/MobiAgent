from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from auto_explore.eval.judge import (
    JUDGE_MODE_LEGACY_TEXT,
    JUDGE_MODE_PATH_MULTIMODAL,
    LLMTrajectoryJudge,
    summarize_batch_results,
)
from auto_explore.eval.loader import collect_trace_dirs, load_trace_sample


class EvaluationInterrupted(RuntimeError):
    def __init__(self, summary: Dict[str, Any]) -> None:
        super().__init__("Evaluation interrupted; partial summary was written.")
        self.summary = summary


def _default_judge_base_url() -> str:
    explicit = os.getenv("AUTO_EXPLORE_EVAL_BASE_URL", "").strip()
    if explicit:
        return explicit
    if os.getenv("SJTU_API_KEY", "").strip():
        return "https://models.sjtu.edu.cn/api/v1"
    return "https://openrouter.ai/api/v1"


def _default_judge_api_key() -> str:
    return (
        os.getenv("AUTO_EXPLORE_EVAL_API_KEY", "").strip()
        or os.getenv("SJTU_API_KEY", "").strip()
        or os.getenv("OPENROUTER_API_KEY", "").strip()
    )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate auto-search traces with an OpenAI-compatible judge model")
    parser.add_argument("--input_path", type=str, required=True, help="Run directory, paths/step directory, or a single trace directory")
    parser.add_argument(
        "--target_level",
        choices=["auto", "paths", "steps"],
        default="auto",
        help="Which trace level to evaluate when input_path is a run directory",
    )
    parser.add_argument("--max_samples", type=int, default=0, help="Optional cap on the number of traces to evaluate")
    parser.add_argument("--judge_base_url", type=str, default=_default_judge_base_url())
    parser.add_argument(
        "--judge_api_key",
        type=str,
        default=_default_judge_api_key(),
        help="Judge API key; empty is allowed for local OpenAI-compatible services",
    )
    parser.add_argument(
        "--judge_model",
        type=str,
        default=os.getenv("AUTO_EXPLORE_EVAL_MODEL", ""),
        help="Judge model name",
    )
    parser.add_argument("--temperature", type=float, default=0.0, help="Judge sampling temperature")
    parser.add_argument("--max_tokens", type=int, default=700, help="Max tokens per judge call")
    parser.add_argument(
        "--enable_thinking",
        choices=["on", "off"],
        default=os.getenv("AUTO_EXPLORE_EVAL_ENABLE_THINKING", "on"),
        help="Enable Qwen thinking mode for judge calls when supported",
    )
    parser.add_argument("--max_steps", type=int, default=30, help="Max steps included in each prompt")
    parser.add_argument("--max_reasoning_chars", type=int, default=320, help="Max reasoning chars per step in prompt")
    parser.add_argument(
        "--judge_mode",
        choices=[JUDGE_MODE_LEGACY_TEXT, JUDGE_MODE_PATH_MULTIMODAL],
        default=JUDGE_MODE_LEGACY_TEXT,
        help="Judge prompt/schema mode",
    )
    parser.add_argument("--output_path", type=str, default="", help="Summary JSON path")
    parser.add_argument(
        "--continue_on_error",
        choices=["on", "off"],
        default="on",
        help="Continue evaluating later traces when one sample fails",
    )
    return parser.parse_args(argv)


def _resolve_output_path(output_path: str) -> Path:
    if output_path:
        return Path(output_path)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = Path(__file__).resolve().parents[3] / "eval" / "results"
    return root / timestamp / "summary.json"


def _write_summary(
    args: argparse.Namespace,
    *,
    output_path: Path,
    results: List[Dict[str, Any]],
    planned_sample_count: int,
    interrupted: bool,
) -> Dict[str, Any]:
    summary = summarize_batch_results(
        input_path=args.input_path,
        target_level=args.target_level,
        judge_model=args.judge_model,
        judge_base_url=args.judge_base_url,
        judge_mode=args.judge_mode,
        results=results,
        interrupted=interrupted,
        planned_sample_count=planned_sample_count,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["output_path"] = str(output_path)
    return summary


def run(args: argparse.Namespace) -> Dict[str, Any]:
    if not args.judge_model:
        raise ValueError("Please provide --judge_model or set AUTO_EXPLORE_EVAL_MODEL")
    if args.judge_mode == JUDGE_MODE_PATH_MULTIMODAL and args.target_level == "steps":
        raise ValueError("path_multimodal only supports path_* samples. Use --target_level paths or auto.")
    trace_dirs = collect_trace_dirs(
        args.input_path,
        target_level=args.target_level,
        max_samples=args.max_samples,
        allow_auto_step_fallback=args.judge_mode != JUDGE_MODE_PATH_MULTIMODAL,
    )
    if not trace_dirs:
        raise ValueError(f"No traces found under: {args.input_path}")

    judge = LLMTrajectoryJudge(
        base_url=args.judge_base_url,
        api_key=args.judge_api_key,
        model=args.judge_model,
        judge_mode=args.judge_mode,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        enable_thinking=args.enable_thinking == "on",
        max_steps=args.max_steps,
        max_reasoning_chars=args.max_reasoning_chars,
    )

    results: List[Dict[str, Any]] = []
    output_path = _resolve_output_path(args.output_path)
    try:
        for trace_dir in trace_dirs:
            try:
                sample = load_trace_sample(trace_dir, judge_mode=args.judge_mode)
                result = judge.evaluate(sample)
                result["status"] = "ok"
                results.append(result)
            except Exception as exc:
                failure = {
                    "sample_id": Path(trace_dir).name,
                    "trace_dir": str(trace_dir),
                    "status": "error",
                    "error": str(exc),
                }
                results.append(failure)
                if args.continue_on_error != "on":
                    raise
    except KeyboardInterrupt as exc:
        summary = _write_summary(
            args,
            output_path=output_path,
            results=results,
            planned_sample_count=len(trace_dirs),
            interrupted=True,
        )
        raise EvaluationInterrupted(summary) from exc

    return _write_summary(
        args,
        output_path=output_path,
        results=results,
        planned_sample_count=len(trace_dirs),
        interrupted=False,
    )


def main() -> None:
    args = parse_args()
    try:
        summary = run(args)
    except EvaluationInterrupted as exc:
        summary = exc.summary
        print(
            json.dumps(
                {
                    "sample_count": summary["sample_count"],
                    "success_count": summary["success_count"],
                    "error_count": summary["error_count"],
                    "averages": summary["averages"],
                    "interrupted": summary.get("interrupted", True),
                    "planned_sample_count": summary.get("planned_sample_count", 0),
                    "evaluated_sample_count": summary.get("evaluated_sample_count", 0),
                    "remaining_sample_count": summary.get("remaining_sample_count", 0),
                    "output_path": summary["output_path"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(130) from exc
    print(
        json.dumps(
            {
                "sample_count": summary["sample_count"],
                "success_count": summary["success_count"],
                "error_count": summary["error_count"],
                "averages": summary["averages"],
                "output_path": summary["output_path"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
