import json
import os
from pathlib import Path
from unittest import TestCase, mock

from test_support import install_test_stubs, make_workspace_tempdir, remove_workspace_tempdir

install_test_stubs()

from auto_explore.eval import cli as eval_cli
from auto_explore.eval.judge import (
    JUDGE_MODE_LEGACY_TEXT,
    JUDGE_MODE_PATH_MULTIMODAL,
    _load_judge_payload,
    build_evaluation_messages,
    summarize_batch_results,
)
from auto_explore.eval.loader import collect_trace_dirs, load_trace_sample, render_trace_for_prompt


class AutoExploreEvalTests(TestCase):
    def _create_trace(
        self,
        root: Path,
        relative_dir: str,
        *,
        image_count: int = 0,
        include_done: bool = True,
        missing_images: set[int] | None = None,
        missing_click_point_images: set[int] | None = None,
        metrics_root: Path | None = None,
        configured_depth_limit: int | None = None,
        configured_breadth: int | None = None,
    ) -> Path:
        trace_dir = root / relative_dir
        trace_dir.mkdir(parents=True, exist_ok=True)
        click_count = max(1, image_count or 1)

        actions = []
        reacts = []
        for action_index in range(1, click_count + 1):
            actions.append(
                {
                    "action_index": action_index,
                    "type": "click",
                    "position_x": 100 + action_index,
                    "position_y": 200 + action_index,
                    "bounds": [10, 20, 30, 40],
                }
            )
            reacts.append(
                {
                    "action_index": action_index,
                    "reasoning": f"Reason about click step {action_index} before checking the next page.",
                    "function": {
                        "name": "click",
                        "parameters": {
                            "target_element": f"target-{action_index}",
                            "bbox": [10, 20, 30, 40],
                        },
                    },
                }
            )

        if include_done:
            done_index = click_count + 1
            actions.append(
                {
                    "action_index": done_index,
                    "type": "done",
                    "status": "success",
                }
            )
            reacts.append(
                {
                    "action_index": done_index,
                    "reasoning": "The sample ends here after the visible steps.",
                    "function": {"name": "done", "parameters": {"status": "success"}},
                }
            )

        actions_payload = {
            "app_name": "DemoApp",
            "task_type": "auto_search",
            "task_description": "Open DemoApp and search for camera results.",
            "actions": actions,
        }
        trace_dir.joinpath("actions.json").write_text(
            json.dumps(actions_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        trace_dir.joinpath("react.json").write_text(
            json.dumps(reacts, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        missing = missing_images or set()
        missing_click_points = missing_click_point_images or set()
        for image_index in range(1, image_count + 1):
            if image_index in missing:
                continue
            trace_dir.joinpath(f"{image_index}.jpg").write_bytes(b"fake-jpeg-bytes")
            if image_index not in missing_click_points:
                trace_dir.joinpath(f"{image_index}_click_point.jpg").write_bytes(b"fake-click-point-jpeg-bytes")

        if metrics_root is not None:
            metrics_root.mkdir(parents=True, exist_ok=True)
            payload = {
                "run_wall_time_sec": 1.23,
                "step_count": len(actions),
                "configured_depth_limit": configured_depth_limit,
                "configured_breadth": configured_breadth,
                "data_dir": str(metrics_root / "data"),
            }
            metrics_root.joinpath("metrics.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        return trace_dir

    def test_collect_trace_dirs_prefers_paths_in_auto_mode(self):
        tmpdir = make_workspace_tempdir("auto_explore_eval")
        try:
            self._create_trace(tmpdir, "paths/path_0001")
            self._create_trace(tmpdir, "steps/step_0001")
            targets = collect_trace_dirs(tmpdir, target_level="auto")
            self.assertEqual([path.name for path in targets], ["path_0001"])
        finally:
            remove_workspace_tempdir(tmpdir)

    def test_collect_trace_dirs_finds_nested_data_paths(self):
        tmpdir = make_workspace_tempdir("auto_explore_eval")
        try:
            run_root = tmpdir / "run_001"
            self._create_trace(
                tmpdir,
                "run_001/data/paths/path_0001",
                image_count=2,
                metrics_root=run_root,
                configured_depth_limit=2,
                configured_breadth=1,
            )
            targets = collect_trace_dirs(run_root, target_level="paths")
            self.assertEqual([path.name for path in targets], ["path_0001"])
        finally:
            remove_workspace_tempdir(tmpdir)

    def test_load_trace_sample_builds_step_records(self):
        tmpdir = make_workspace_tempdir("auto_explore_eval")
        try:
            trace_dir = self._create_trace(tmpdir, "paths/path_0001")
            sample = load_trace_sample(trace_dir)
            self.assertEqual(sample["sample_id"], "path_0001")
            self.assertEqual(sample["step_count"], 2)
            self.assertTrue(sample["stats"]["has_done"])
            rendered = render_trace_for_prompt(sample)
            self.assertIn("target-1", rendered)
            self.assertIn("done", rendered)
        finally:
            remove_workspace_tempdir(tmpdir)

    def test_path_multimodal_loads_images_and_terminal_done(self):
        tmpdir = make_workspace_tempdir("auto_explore_eval")
        try:
            run_root = tmpdir / "run_001"
            trace_dir = self._create_trace(
                tmpdir,
                "run_001/data/paths/path_0001",
                image_count=3,
                include_done=True,
                metrics_root=run_root,
                configured_depth_limit=3,
                configured_breadth=2,
            )
            sample = load_trace_sample(trace_dir, judge_mode=JUDGE_MODE_PATH_MULTIMODAL)

            self.assertEqual(sample["image_count"], 3)
            self.assertEqual([Path(path).name for path in sample["image_paths"]], ["1.jpg", "2.jpg", "3.jpg"])
            self.assertEqual(len(sample["image_step_records"]), 3)
            self.assertEqual(Path(sample["image_step_records"][0]["click_point_image_path"]).name, "1_click_point.jpg")
            self.assertEqual(len(sample["terminal_step_records"]), 1)
            self.assertEqual(sample["terminal_step_records"][0]["action_type"], "done")
            self.assertEqual(sample["configured_depth_limit"], 3)
            self.assertEqual(sample["configured_breadth"], 2)
        finally:
            remove_workspace_tempdir(tmpdir)

    def test_build_path_multimodal_messages_interleave_text_and_images(self):
        tmpdir = make_workspace_tempdir("auto_explore_eval")
        try:
            run_root = tmpdir / "run_001"
            trace_dir = self._create_trace(
                tmpdir,
                "run_001/data/paths/path_0001",
                image_count=2,
                include_done=True,
                metrics_root=run_root,
                configured_depth_limit=2,
                configured_breadth=1,
            )
            sample = load_trace_sample(trace_dir, judge_mode=JUDGE_MODE_PATH_MULTIMODAL)
            messages = build_evaluation_messages(sample, judge_mode=JUDGE_MODE_PATH_MULTIMODAL)

            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[1]["role"], "user")
            content = messages[1]["content"]
            self.assertEqual(
                [item["type"] for item in content],
                [
                    "text",
                    "text",
                    "text",
                    "image_url",
                    "text",
                    "image_url",
                    "text",
                    "text",
                    "image_url",
                    "text",
                    "image_url",
                    "text",
                ],
            )
            self.assertIn("step_index", content[1]["text"])
            self.assertIn("action_type", content[1]["text"])
            self.assertIn("status", content[1]["text"])
            self.assertIn("reasoning", content[1]["text"])
            self.assertIn("Original Screenshot", content[2]["text"])
            self.assertTrue(content[3]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
            self.assertIn("Click-Point Screenshot", content[4]["text"])
            self.assertTrue(content[5]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        finally:
            remove_workspace_tempdir(tmpdir)

    def test_path_multimodal_marks_missing_click_point_image(self):
        tmpdir = make_workspace_tempdir("auto_explore_eval")
        try:
            run_root = tmpdir / "run_001"
            trace_dir = self._create_trace(
                tmpdir,
                "run_001/data/paths/path_0001",
                image_count=1,
                missing_click_point_images={1},
                metrics_root=run_root,
                configured_depth_limit=1,
                configured_breadth=1,
            )
            sample = load_trace_sample(trace_dir, judge_mode=JUDGE_MODE_PATH_MULTIMODAL)
            messages = build_evaluation_messages(sample, judge_mode=JUDGE_MODE_PATH_MULTIMODAL)
            content = messages[1]["content"]

            self.assertFalse(sample["image_step_records"][0]["has_click_point_image"])
            self.assertIn("click-point screenshot is missing", content[1]["text"])
            self.assertEqual([item["type"] for item in content], ["text", "text", "text", "image_url", "text"])
        finally:
            remove_workspace_tempdir(tmpdir)

    def test_path_multimodal_rejects_step_samples(self):
        tmpdir = make_workspace_tempdir("auto_explore_eval")
        try:
            trace_dir = self._create_trace(tmpdir, "steps/step_0001")
            with self.assertRaisesRegex(ValueError, "path_\\* samples"):
                load_trace_sample(trace_dir, judge_mode=JUDGE_MODE_PATH_MULTIMODAL)
        finally:
            remove_workspace_tempdir(tmpdir)

    def test_path_multimodal_requires_depth_limit(self):
        tmpdir = make_workspace_tempdir("auto_explore_eval")
        try:
            run_root = tmpdir / "run_001"
            trace_dir = self._create_trace(
                tmpdir,
                "run_001/data/paths/path_0001",
                image_count=2,
                metrics_root=run_root,
                configured_depth_limit=None,
                configured_breadth=1,
            )
            with self.assertRaisesRegex(ValueError, "configured_depth_limit"):
                load_trace_sample(trace_dir, judge_mode=JUDGE_MODE_PATH_MULTIMODAL)
        finally:
            remove_workspace_tempdir(tmpdir)

    def test_path_multimodal_rejects_image_numbering_gaps(self):
        tmpdir = make_workspace_tempdir("auto_explore_eval")
        try:
            run_root = tmpdir / "run_001"
            trace_dir = self._create_trace(
                tmpdir,
                "run_001/data/paths/path_0001",
                image_count=3,
                missing_images={2},
                metrics_root=run_root,
                configured_depth_limit=3,
                configured_breadth=1,
            )
            with self.assertRaisesRegex(ValueError, "Missing numbered screenshot 2"):
                load_trace_sample(trace_dir, judge_mode=JUDGE_MODE_PATH_MULTIMODAL)
        finally:
            remove_workspace_tempdir(tmpdir)

    def test_eval_cli_runs_and_writes_legacy_summary(self):
        tmpdir = make_workspace_tempdir("auto_explore_eval")
        try:
            trace_dir = self._create_trace(tmpdir, "paths/path_0001")
            output_path = tmpdir / "summary.json"
            args = eval_cli.parse_args(
                [
                    "--input_path",
                    str(tmpdir),
                    "--target_level",
                    "paths",
                    "--judge_base_url",
                    "http://localhost:8000/v1",
                    "--judge_model",
                    "mock-judge",
                    "--output_path",
                    str(output_path),
                ]
            )

            fake_result = {
                "sample_id": trace_dir.name,
                "sample_kind": "path",
                "trace_dir": str(trace_dir),
                "app_name": "DemoApp",
                "task_description": "Open DemoApp and search for camera results.",
                "trajectory_completeness_score": 8,
                "reasoning_quality_score": 7,
                "overall_score": 8,
                "subscores": {
                    "goal_coverage": 8,
                    "step_coherence": 8,
                    "action_reason_alignment": 7,
                    "groundedness": 7,
                },
                "strengths": ["The path is concise."],
                "issues": ["The sample ends early."],
                "summary": "Usable but short.",
                "step_count": 2,
                "judge_mode": JUDGE_MODE_LEGACY_TEXT,
                "evaluated_at": "2026-04-22T12:00:00",
                "judge_model": "mock-judge",
                "judge_base_url": "http://localhost:8000/v1",
            }

            with mock.patch("auto_explore.eval.cli.LLMTrajectoryJudge") as mocked_judge_cls:
                mocked_judge = mocked_judge_cls.return_value
                mocked_judge.evaluate.return_value = fake_result
                summary = eval_cli.run(args)

            self.assertTrue(output_path.exists())
            saved = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["sample_count"], 1)
            self.assertEqual(saved["success_count"], 1)
            self.assertEqual(saved["averages"]["overall_score"], 8.0)
            mocked_judge.evaluate.assert_called_once()
            self.assertEqual(summary["output_path"], str(output_path))
        finally:
            remove_workspace_tempdir(tmpdir)

    def test_eval_cli_path_multimodal_summary_averages_all_scores(self):
        tmpdir = make_workspace_tempdir("auto_explore_eval")
        try:
            run_root = tmpdir / "run_001"
            trace_dir = self._create_trace(
                tmpdir,
                "run_001/data/paths/path_0001",
                image_count=2,
                metrics_root=run_root,
                configured_depth_limit=2,
                configured_breadth=1,
            )
            output_path = tmpdir / "multimodal_summary.json"
            args = eval_cli.parse_args(
                [
                    "--input_path",
                    str(run_root),
                    "--target_level",
                    "paths",
                    "--judge_mode",
                    "path_multimodal",
                    "--judge_base_url",
                    "http://localhost:8000/v1",
                    "--judge_model",
                    "mock-judge",
                    "--output_path",
                    str(output_path),
                ]
            )
            fake_result = {
                "sample_id": trace_dir.name,
                "sample_kind": "path",
                "trace_dir": str(trace_dir),
                "app_name": "DemoApp",
                "task_description": "Open DemoApp and search for camera results.",
                "trajectory_completeness_score": 8,
                "image_coherence_score": 6,
                "task_operation_match_score": 7,
                "subscores": {
                    "goal_coverage": 8,
                    "visual_action_alignment": 7,
                    "click_target_grounding": 6,
                    "inter_image_continuity": 6,
                    "termination_quality": 7,
                },
                "strengths": ["Screens are mostly sequential."],
                "issues": ["One transition is abrupt."],
                "summary": "Mostly coherent path with a small jump.",
                "step_count": 3,
                "image_count": 2,
                "judge_mode": JUDGE_MODE_PATH_MULTIMODAL,
                "evaluated_at": "2026-04-22T12:00:00",
                "judge_model": "mock-judge",
                "judge_base_url": "http://localhost:8000/v1",
            }

            with mock.patch("auto_explore.eval.cli.LLMTrajectoryJudge") as mocked_judge_cls:
                mocked_judge = mocked_judge_cls.return_value
                mocked_judge.evaluate.return_value = fake_result
                summary = eval_cli.run(args)

            saved = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["judge_mode"], "path_multimodal")
            self.assertEqual(saved["averages"]["trajectory_completeness_score"], 8.0)
            self.assertEqual(saved["averages"]["image_coherence_score"], 6.0)
            self.assertEqual(saved["averages"]["task_operation_match_score"], 7.0)
            self.assertEqual(saved["averages"]["goal_coverage"], 8.0)
            self.assertEqual(saved["averages"]["visual_action_alignment"], 7.0)
            self.assertEqual(saved["averages"]["click_target_grounding"], 6.0)
            self.assertEqual(saved["averages"]["inter_image_continuity"], 6.0)
            self.assertEqual(saved["averages"]["termination_quality"], 7.0)
            self.assertNotIn("overall_score", saved["averages"])
            self.assertEqual(summary["output_path"], str(output_path))
        finally:
            remove_workspace_tempdir(tmpdir)

    def test_summarize_batch_results_ignores_failed_items_in_average(self):
        summary = summarize_batch_results(
            input_path="demo",
            target_level="paths",
            judge_model="mock",
            judge_base_url="http://localhost",
            results=[
                {"status": "ok", "trajectory_completeness_score": 8, "reasoning_quality_score": 6, "overall_score": 7},
                {"status": "error", "error": "bad"},
            ],
        )
        self.assertEqual(summary["sample_count"], 2)
        self.assertEqual(summary["success_count"], 1)
        self.assertEqual(summary["averages"]["overall_score"], 7.0)

    def test_summarize_batch_results_path_multimodal_averages_all_scores(self):
        summary = summarize_batch_results(
            input_path="demo",
            target_level="paths",
            judge_model="mock",
            judge_base_url="http://localhost",
            judge_mode=JUDGE_MODE_PATH_MULTIMODAL,
            results=[
                {
                    "status": "ok",
                    "trajectory_completeness_score": 8,
                    "image_coherence_score": 6,
                    "task_operation_match_score": 7,
                    "subscores": {
                        "goal_coverage": 9,
                        "visual_action_alignment": 6,
                        "click_target_grounding": 8,
                        "inter_image_continuity": 5,
                        "termination_quality": 7,
                    },
                },
                {"status": "error", "error": "bad"},
            ],
            planned_sample_count=3,
            interrupted=True,
        )
        self.assertEqual(summary["sample_count"], 2)
        self.assertEqual(summary["success_count"], 1)
        self.assertTrue(summary["interrupted"])
        self.assertEqual(summary["planned_sample_count"], 3)
        self.assertEqual(summary["evaluated_sample_count"], 2)
        self.assertEqual(summary["remaining_sample_count"], 1)
        self.assertEqual(summary["averages"]["trajectory_completeness_score"], 8.0)
        self.assertEqual(summary["averages"]["image_coherence_score"], 6.0)
        self.assertEqual(summary["averages"]["task_operation_match_score"], 7.0)
        self.assertEqual(summary["averages"]["goal_coverage"], 9.0)
        self.assertEqual(summary["averages"]["visual_action_alignment"], 6.0)
        self.assertEqual(summary["averages"]["click_target_grounding"], 8.0)
        self.assertEqual(summary["averages"]["inter_image_continuity"], 5.0)
        self.assertEqual(summary["averages"]["termination_quality"], 7.0)
        self.assertNotIn("overall_score", summary["averages"])

    def test_path_multimodal_recovers_scores_from_malformed_judge_json(self):
        payload = _load_judge_payload(
            """
            {
              "trajectory_completeness_score": 8,
              "image_coherence_score": 6,
              "task_operation_match_score": 7,
              "subscores": {
                "goal_coverage": 9,
                "visual_action_alignment": 6,
                "click_target_grounding": 9,
                "inter_image_continuity": 5,
                "termination_quality": 9
              },
              "strengths": [bad
            """,
            judge_mode=JUDGE_MODE_PATH_MULTIMODAL,
        )

        self.assertEqual(payload["trajectory_completeness_score"], 8)
        self.assertEqual(payload["image_coherence_score"], 6)
        self.assertEqual(payload["task_operation_match_score"], 7)
        self.assertEqual(payload["subscores"]["click_target_grounding"], 9)
        self.assertIn("not valid JSON", payload["issues"][0])

    def test_eval_cli_writes_partial_summary_on_keyboard_interrupt(self):
        tmpdir = make_workspace_tempdir("auto_explore_eval")
        try:
            run_root = tmpdir / "run_001"
            self._create_trace(
                tmpdir,
                "run_001/data/paths/path_0001",
                image_count=1,
                metrics_root=run_root,
                configured_depth_limit=1,
                configured_breadth=1,
            )
            self._create_trace(
                tmpdir,
                "run_001/data/paths/path_0002",
                image_count=1,
                metrics_root=run_root,
                configured_depth_limit=1,
                configured_breadth=1,
            )
            output_path = tmpdir / "partial_summary.json"
            args = eval_cli.parse_args(
                [
                    "--input_path",
                    str(run_root),
                    "--target_level",
                    "paths",
                    "--judge_mode",
                    "path_multimodal",
                    "--judge_base_url",
                    "http://localhost:8000/v1",
                    "--judge_model",
                    "mock-judge",
                    "--output_path",
                    str(output_path),
                ]
            )
            fake_result = {
                "sample_id": "path_0001",
                "sample_kind": "path",
                "trace_dir": str(run_root / "data" / "paths" / "path_0001"),
                "app_name": "DemoApp",
                "task_description": "Open DemoApp and search for camera results.",
                "trajectory_completeness_score": 8,
                "image_coherence_score": 7,
                "task_operation_match_score": 6,
                "subscores": {},
                "strengths": [],
                "issues": [],
                "summary": "Partial result.",
                "step_count": 2,
                "image_count": 1,
                "judge_mode": JUDGE_MODE_PATH_MULTIMODAL,
                "evaluated_at": "2026-04-22T12:00:00",
                "judge_model": "mock-judge",
                "judge_base_url": "http://localhost:8000/v1",
            }

            with mock.patch("auto_explore.eval.cli.LLMTrajectoryJudge") as mocked_judge_cls:
                mocked_judge = mocked_judge_cls.return_value
                mocked_judge.evaluate.side_effect = [fake_result, KeyboardInterrupt()]
                with self.assertRaises(eval_cli.EvaluationInterrupted) as raised:
                    eval_cli.run(args)

            saved = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertTrue(saved["interrupted"])
            self.assertEqual(saved["planned_sample_count"], 2)
            self.assertEqual(saved["evaluated_sample_count"], 1)
            self.assertEqual(saved["remaining_sample_count"], 1)
            self.assertEqual(saved["averages"]["task_operation_match_score"], 6.0)
            self.assertEqual(raised.exception.summary["output_path"], str(output_path))
        finally:
            remove_workspace_tempdir(tmpdir)

    def test_parse_args_prefers_sjtu_api_key_defaults(self):
        with mock.patch.dict(
            os.environ,
            {
                "SJTU_API_KEY": "sjtu-key",
            },
            clear=True,
        ):
            args = eval_cli.parse_args(
                [
                    "--input_path",
                    "auto_explore/data/demo",
                    "--judge_model",
                    "mock-judge",
                ]
            )
        self.assertEqual(args.judge_api_key, "sjtu-key")
        self.assertEqual(args.judge_base_url, "https://models.sjtu.edu.cn/api/v1")
