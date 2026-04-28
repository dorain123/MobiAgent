import json
from types import SimpleNamespace
from unittest import TestCase, mock

from test_support import install_test_stubs, make_workspace_tempdir, remove_workspace_tempdir

install_test_stubs()

from auto_explore.core.decider import execute_decider_one_step
from auto_explore.core.dfs import _get_repeat_skip_reason, explore_dfs


class _Metrics:
    def __init__(self):
        self.saved_traces = {"partial": 0, "complete": 0}

    def now(self):
        return 0.0

    def relative_time(self, value=None):
        return 0.0 if value is None else value

    def record_decider_call(self, *_args, **_kwargs):
        return None

    def record_timing_trace(self, *_args, **_kwargs):
        return None

    def record_step_duration(self, *_args, **_kwargs):
        return None

    def record_backtrack(self, *_args, **_kwargs):
        return None

    def record_artifact_io(self, *_args, **_kwargs):
        return None

    def record_saved_trace(self, status):
        self.saved_traces[status] = self.saved_traces.get(status, 0) + 1


class DeciderHistoryTests(TestCase):
    def test_execute_decider_one_step_passes_history_into_prompt_builder(self):
        runtime = SimpleNamespace(
            metrics=_Metrics(),
            features=SimpleNamespace(hierarchy_text_decider=False, async_artifact_io=False),
        )
        fake_device = mock.Mock()
        fake_device.click = mock.Mock()

        with mock.patch("auto_explore.core.decider.get_screenshot", return_value="img-b64"), mock.patch(
            "auto_explore.core.decider.get_hierarchy_text", return_value="<root />"
        ), mock.patch(
            "auto_explore.core.decider.build_auto_decider_messages", return_value=[{"role": "user", "content": []}]
        ) as mocked_messages, mock.patch(
            "auto_explore.core.decider.call_model_with_validation_retry",
            return_value={"reasoning": "ok", "action": "click", "parameters": {"bbox": [0, 0, 10, 10]}},
        ), mock.patch(
            "auto_explore.core.decider.save_raw_screenshot", return_value="dummy.jpg"
        ), mock.patch(
            "auto_explore.core.decider.save_hierarchy"
        ), mock.patch(
            "auto_explore.core.decider.refine_bbox_with_hierarchy", return_value=[0, 0, 10, 10]
        ):
            execute_decider_one_step(
                decider_client=object(),
                decider_model="demo-model",
                device=fake_device,
                device_type="Android",
                app_name="DemoApp",
                step_task="tap target",
                use_qwen3=False,
                allow_hierarchy_text_decider=False,
                output_dir="unused",
                step_index=1,
                runtime=runtime,
                history=["Step 1: open home", "Step 2: tap search"],
            )

        self.assertEqual(
            mocked_messages.call_args.kwargs["history"],
            ["Step 1: open home", "Step 2: tap search"],
        )


class DfsProgressGateTests(TestCase):
    def test_explore_dfs_does_not_save_leaf_path_when_page_does_not_change(self):
        tmpdir = make_workspace_tempdir("dfs_progress_gate")
        try:
            data_dir = tmpdir / "data"
            steps_dir = data_dir / "steps"
            paths_dir = data_dir / "paths"
            partial_paths_dir = data_dir / "partial_paths"
            ui_pages_dir = data_dir / "ui-pages"
            steps_dir.mkdir(parents=True, exist_ok=True)
            paths_dir.mkdir(parents=True, exist_ok=True)
            partial_paths_dir.mkdir(parents=True, exist_ok=True)
            ui_pages_dir.mkdir(parents=True, exist_ok=True)

            runtime = SimpleNamespace(
                metrics=_Metrics(),
                features=SimpleNamespace(
                    popup_auto_dismiss=False,
                    already_explored_filter=False,
                    explorer_cache=False,
                    candidate_dedup=False,
                    screen_cache=False,
                    concurrent_fingerprint=False,
                    hierarchy_text_decider=False,
                    async_artifact_io=False,
                    replay_recovery=False,
                    triple_verify=False,
                ),
            )
            fake_device = mock.Mock()
            fake_device.start_app = mock.Mock()

            with mock.patch(
                "auto_explore.core.dfs._capture_screen",
                return_value=("img-b64", "<root><node text='home'/></root>"),
            ), mock.patch(
                "auto_explore.core.dfs.call_explorer_model",
                return_value=([{"rank": 1, "single_step_task": "tap target", "reason": "test"}], None),
            ), mock.patch(
                "auto_explore.core.dfs.compute_fingerprints",
                return_value=("pagefp", "structfp", "dhash"),
            ), mock.patch(
                "auto_explore.core.dfs.execute_decider_one_step",
                return_value={
                    "action_record": {"action_index": 1, "source_task": "tap target", "type": "click"},
                    "react_item": {"action_index": 1},
                    "post_hierarchy_text": "<root><node text='home'/></root>",
                },
            ), mock.patch(
                "auto_explore.core.dfs.get_hierarchy_text", return_value="<root><node text='home'/></root>"
            ), mock.patch(
                "auto_explore.core.dfs.get_screenshot", return_value=""
            ), mock.patch(
                "auto_explore.core.dfs._persist_step_output_safe"
            ), mock.patch(
                "auto_explore.core.dfs.perform_backtrack_action"
            ), mock.patch(
                "auto_explore.core.dfs._is_app_in_foreground", return_value=True
            ), mock.patch(
                "auto_explore.core.dfs._simple_verify", return_value=True
            ), mock.patch(
                "auto_explore.core.dfs.copy_step_artifacts_to_path"
            ) as mocked_copy_to_path, mock.patch(
                "auto_explore.core.dfs._persist_outputs_safe"
            ):
                path_counter = [0]
                partial_path_counter = [0]
                explore_dfs(
                    app_name="DemoApp",
                    depth_limit=1,
                    breadth=1,
                    current_depth=0,
                    decider_client=object(),
                    decider_model="decider",
                    explorer_client=object(),
                    explorer_model="explorer",
                    device=fake_device,
                    device_type="Android",
                    use_qwen3=False,
                    allow_hierarchy_text_decider=False,
                    data_dir=str(data_dir),
                    actions=[],
                    reacts=[],
                    step_counter=[0],
                    path_counter=path_counter,
                    partial_path_counter=partial_path_counter,
                    page_counter=[0],
                    steps_dir=str(steps_dir),
                    paths_dir=str(paths_dir),
                    partial_paths_dir=str(partial_paths_dir),
                    enable_ui_semantic_collect=False,
                    ui_pages_dir=str(ui_pages_dir),
                    page_registry={},
                    collect_queue=None,
                    queue_lock=None,
                    index_lock=None,
                    index_path=None,
                    ui_collect_async=False,
                    ui_collect_queue_size=0,
                    runtime=runtime,
                    visited_tasks={},
                    explorer_cache=None,
                    screen_cache=None,
                    popup_dismiss_max_attempts=0,
                )

            self.assertEqual(path_counter[0], 0)
            self.assertEqual(partial_path_counter[0], 0)
            mocked_copy_to_path.assert_not_called()
        finally:
            remove_workspace_tempdir(tmpdir)


class DfsRepeatFilterTests(TestCase):
    def test_same_page_repeat_is_skipped_from_executed_task_history(self):
        reason = _get_repeat_skip_reason(
            task="点击底部导航栏的'我的淘宝'图标",
            current_page_fp="pagefp",
            executed_tasks_by_page={"pagefp": {"点击底部导航栏的'我的淘宝'图标"}},
            path_level_repeat_history=[],
        )

        self.assertEqual(reason, "same_page")

    def test_cross_page_navigation_repeat_is_skipped_from_path_history(self):
        reason = _get_repeat_skip_reason(
            task="点击底部导航栏的'我的淘宝'图标",
            current_page_fp="pagefp-next",
            executed_tasks_by_page={},
            path_level_repeat_history=["点击底部导航栏的'我的淘宝'图标"],
        )

        self.assertEqual(reason, "path_navigation")

    def test_cross_page_non_navigation_repeat_is_not_skipped(self):
        reason = _get_repeat_skip_reason(
            task="点击商品卡片'iPhone 15'",
            current_page_fp="pagefp-next",
            executed_tasks_by_page={},
            path_level_repeat_history=["点击商品卡片'iPhone 15'"],
        )

        self.assertIsNone(reason)

    def test_explore_dfs_skips_repeated_navigation_candidate_before_step_creation(self):
        tmpdir = make_workspace_tempdir("dfs_repeat_skip")
        try:
            data_dir = tmpdir / "data"
            steps_dir = data_dir / "steps"
            paths_dir = data_dir / "paths"
            partial_paths_dir = data_dir / "partial_paths"
            ui_pages_dir = data_dir / "ui-pages"
            steps_dir.mkdir(parents=True, exist_ok=True)
            paths_dir.mkdir(parents=True, exist_ok=True)
            partial_paths_dir.mkdir(parents=True, exist_ok=True)
            ui_pages_dir.mkdir(parents=True, exist_ok=True)

            runtime = SimpleNamespace(
                metrics=_Metrics(),
                features=SimpleNamespace(
                    popup_auto_dismiss=False,
                    already_explored_filter=True,
                    explorer_cache=False,
                    candidate_dedup=False,
                    screen_cache=False,
                    concurrent_fingerprint=False,
                    hierarchy_text_decider=False,
                    async_artifact_io=False,
                    replay_recovery=False,
                    triple_verify=False,
                ),
            )
            fake_device = mock.Mock()
            fake_device.start_app = mock.Mock()

            path_actions = [
                {
                    "action_index": 1,
                    "source_task": "点击底部导航栏的'我的淘宝'图标",
                    "type": "click",
                }
            ]
            path_reacts = [{"action_index": 1}]

            with mock.patch(
                "auto_explore.core.dfs._capture_screen",
                return_value=("img-b64", "<root><node text='my taobao'/></root>"),
            ), mock.patch(
                "auto_explore.core.dfs.call_explorer_model",
                return_value=(
                    [{"rank": 1, "single_step_task": "点击底部导航栏的'我的淘宝'图标", "reason": "same tab"}],
                    None,
                ),
            ), mock.patch(
                "auto_explore.core.dfs.compute_fingerprints",
                return_value=("pagefp", "structfp", "dhash"),
            ), mock.patch(
                "auto_explore.core.dfs._filter_repeated_candidates",
                side_effect=lambda candidates, **_kwargs: candidates,
            ), mock.patch(
                "auto_explore.core.dfs.execute_decider_one_step"
            ) as mocked_execute, mock.patch(
                "auto_explore.core.dfs.get_hierarchy_text", return_value="<root><node text='my taobao'/></root>"
            ), mock.patch(
                "auto_explore.core.dfs.get_screenshot", return_value=""
            ), mock.patch(
                "auto_explore.core.dfs._persist_step_output_safe"
            ), mock.patch(
                "auto_explore.core.dfs.perform_backtrack_action"
            ), mock.patch(
                "auto_explore.core.dfs._is_app_in_foreground", return_value=True
            ), mock.patch(
                "auto_explore.core.dfs._simple_verify", return_value=True
            ), mock.patch(
                "auto_explore.core.dfs.copy_step_artifacts_to_path"
            ), mock.patch(
                "auto_explore.core.dfs._persist_outputs_safe"
            ):
                step_counter = [0]
                explore_dfs(
                    app_name="DemoApp",
                    depth_limit=2,
                    breadth=1,
                    current_depth=0,
                    decider_client=object(),
                    decider_model="decider",
                    explorer_client=object(),
                    explorer_model="explorer",
                    device=fake_device,
                    device_type="Android",
                    use_qwen3=False,
                    allow_hierarchy_text_decider=False,
                    data_dir=str(data_dir),
                    actions=[],
                    reacts=[],
                    step_counter=step_counter,
                    path_counter=[0],
                    partial_path_counter=[0],
                    page_counter=[0],
                    steps_dir=str(steps_dir),
                    paths_dir=str(paths_dir),
                    partial_paths_dir=str(partial_paths_dir),
                    enable_ui_semantic_collect=False,
                    ui_pages_dir=str(ui_pages_dir),
                    page_registry={},
                    collect_queue=None,
                    queue_lock=None,
                    index_lock=None,
                    index_path=None,
                    ui_collect_async=False,
                    ui_collect_queue_size=0,
                    runtime=runtime,
                    path_actions=path_actions,
                    path_reacts=path_reacts,
                    visited_tasks={},
                    explorer_cache=None,
                    screen_cache=None,
                    popup_dismiss_max_attempts=0,
                )

            mocked_execute.assert_not_called()
            self.assertEqual(step_counter[0], 0)
            self.assertEqual(list(steps_dir.iterdir()), [])
        finally:
            remove_workspace_tempdir(tmpdir)

    def test_explore_dfs_saves_partial_trace_without_done_success(self):
        tmpdir = make_workspace_tempdir("dfs_partial_trace")
        try:
            data_dir = tmpdir / "data"
            steps_dir = data_dir / "steps"
            paths_dir = data_dir / "paths"
            partial_paths_dir = data_dir / "partial_paths"
            ui_pages_dir = data_dir / "ui-pages"
            steps_dir.mkdir(parents=True, exist_ok=True)
            paths_dir.mkdir(parents=True, exist_ok=True)
            partial_paths_dir.mkdir(parents=True, exist_ok=True)
            ui_pages_dir.mkdir(parents=True, exist_ok=True)

            metrics = _Metrics()
            runtime = SimpleNamespace(
                metrics=metrics,
                features=SimpleNamespace(
                    popup_auto_dismiss=False,
                    already_explored_filter=False,
                    explorer_cache=False,
                    candidate_dedup=False,
                    screen_cache=False,
                    concurrent_fingerprint=False,
                    hierarchy_text_decider=False,
                    async_artifact_io=False,
                    replay_recovery=False,
                    triple_verify=False,
                ),
            )
            fake_device = mock.Mock()
            fake_device.start_app = mock.Mock()

            path_actions = [
                {"action_index": 1, "source_task": "step1", "type": "click"},
                {"action_index": 2, "source_task": "step2", "type": "click"},
            ]
            path_reacts = [{"action_index": 1}, {"action_index": 2}]

            with mock.patch(
                "auto_explore.core.dfs._capture_screen",
                return_value=("img-b64", "<root><node text='home'/></root>"),
            ), mock.patch(
                "auto_explore.core.dfs.call_explorer_model",
                return_value=([{"rank": 1, "single_step_task": "tap-search", "reason": "test"}], None),
            ), mock.patch(
                "auto_explore.core.dfs.compute_fingerprints",
                return_value=("pagefp", "structfp", "dhash"),
            ), mock.patch(
                "auto_explore.core.dfs.execute_decider_one_step",
                return_value={
                    "action_record": {"action_index": 3, "source_task": "tap-search", "type": "click"},
                    "react_item": {"action_index": 3},
                    "post_hierarchy_text": "<root><node text='home'/></root>",
                },
            ), mock.patch(
                "auto_explore.core.dfs.get_hierarchy_text", return_value="<root><node text='home'/></root>"
            ), mock.patch(
                "auto_explore.core.dfs.get_screenshot", return_value=""
            ), mock.patch(
                "auto_explore.core.dfs.save_named_raw_screenshot"
            ), mock.patch(
                "auto_explore.core.dfs.save_named_hierarchy"
            ), mock.patch(
                "auto_explore.core.dfs.perform_backtrack_action"
            ), mock.patch(
                "auto_explore.core.dfs._is_app_in_foreground", return_value=True
            ), mock.patch(
                "auto_explore.core.dfs._simple_verify", return_value=True
            ):
                partial_path_counter = [0]
                explore_dfs(
                    app_name="DemoApp",
                    depth_limit=4,
                    breadth=1,
                    current_depth=0,
                    decider_client=object(),
                    decider_model="decider",
                    explorer_client=object(),
                    explorer_model="explorer",
                    device=fake_device,
                    device_type="Android",
                    use_qwen3=False,
                    allow_hierarchy_text_decider=False,
                    data_dir=str(data_dir),
                    actions=[],
                    reacts=[],
                    step_counter=[2],
                    path_counter=[0],
                    partial_path_counter=partial_path_counter,
                    page_counter=[0],
                    steps_dir=str(steps_dir),
                    paths_dir=str(paths_dir),
                    partial_paths_dir=str(partial_paths_dir),
                    enable_ui_semantic_collect=False,
                    ui_pages_dir=str(ui_pages_dir),
                    page_registry={},
                    collect_queue=None,
                    queue_lock=None,
                    index_lock=None,
                    index_path=None,
                    ui_collect_async=False,
                    ui_collect_queue_size=0,
                    runtime=runtime,
                    path_actions=path_actions,
                    path_reacts=path_reacts,
                    visited_tasks={},
                    explorer_cache=None,
                    screen_cache=None,
                    popup_dismiss_max_attempts=0,
                )

            self.assertEqual(partial_path_counter[0], 1)
            self.assertEqual(metrics.saved_traces["partial"], 1)
            partial_dir = partial_paths_dir / "path_0001"
            self.assertTrue((partial_dir / "actions.json").exists())
            self.assertTrue((partial_dir / "trace_meta.json").exists())
            actions_payload = json.loads((partial_dir / "actions.json").read_text(encoding="utf-8"))
            self.assertEqual(actions_payload["action_count"], 3)
            self.assertNotIn("done", json.dumps(actions_payload, ensure_ascii=False))
            meta_payload = json.loads((partial_dir / "trace_meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta_payload["status"], "partial")
            self.assertEqual(meta_payload["saved_reason"], "no_progress")
        finally:
            remove_workspace_tempdir(tmpdir)
