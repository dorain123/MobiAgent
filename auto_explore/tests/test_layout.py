import sys
from pathlib import Path
from unittest import TestCase


class AutoExploreLayoutTests(TestCase):
    def test_auto_explore_top_level_layout_exists(self):
        required = [
            Path("auto_explore/README.md"),
            Path("auto_explore/configs/simulators.example.json"),
            Path("auto_explore/scripts/run_single.sh"),
            Path("auto_explore/scripts/run_parallel.sh"),
            Path("auto_explore/scripts/run_single.bat"),
            Path("auto_explore/src/auto_explore/__init__.py"),
            Path("auto_explore/src/auto_explore/cli/__init__.py"),
            Path("auto_explore/src/auto_explore/core/__init__.py"),
            Path("auto_explore/src/auto_explore/adapters/__init__.py"),
        ]
        for path in required:
            self.assertTrue(path.exists(), f"missing: {path}")

    def test_docs_reference_new_auto_explore_paths(self):
        content = Path("docs/auto-search.md").read_text(encoding="utf-8")
        self.assertIn("auto_explore/", content)
        self.assertNotIn("runner/mobiagent/auto-search.py", content)

    def test_import_auto_explore_adds_repo_root_to_sys_path(self):
        import auto_explore

        repo_root = str(Path(__file__).resolve().parents[2])
        self.assertIn(repo_root, sys.path)

    def test_run_scripts_do_not_depend_on_pwd_for_pythonpath(self):
        single = Path("auto_explore/scripts/run_single.sh").read_text(encoding="utf-8")
        parallel = Path("auto_explore/scripts/run_parallel.sh").read_text(encoding="utf-8")

        self.assertNotIn('${PWD}/auto_explore/src', single)
        self.assertNotIn('${PWD}/auto_explore/src', parallel)

    def test_auto_search_cli_is_trimmed_to_orchestration_layer(self):
        content = Path("auto_explore/src/auto_explore/cli/auto_search.py").read_text(encoding="utf-8")
        self.assertNotIn("def explore_dfs(", content)
        self.assertNotIn("def call_explorer_model(", content)
        self.assertNotIn("def execute_decider_one_step(", content)
