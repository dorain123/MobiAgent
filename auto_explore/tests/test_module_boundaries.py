from unittest import TestCase
from pathlib import Path


class AutoExploreModuleBoundaryTests(TestCase):
    def test_core_modules_are_importable(self):
        from auto_explore.core import artifacts  # noqa: F401
        from auto_explore.core import decider  # noqa: F401
        from auto_explore.core import dfs  # noqa: F401
        from auto_explore.core import explorer  # noqa: F401
        from auto_explore.core import fingerprints  # noqa: F401
        from auto_explore.core import navigation  # noqa: F401
        from auto_explore.core import prompting  # noqa: F401
        from auto_explore.core import ui_collect  # noqa: F401

    def test_selected_core_modules_have_local_implementations(self):
        module_files = [
            Path("auto_explore/src/auto_explore/core/artifacts.py"),
            Path("auto_explore/src/auto_explore/core/prompting.py"),
            Path("auto_explore/src/auto_explore/core/fingerprints.py"),
            Path("auto_explore/src/auto_explore/core/explorer.py"),
            Path("auto_explore/src/auto_explore/core/ui_collect.py"),
            Path("auto_explore/src/auto_explore/core/navigation.py"),
            Path("auto_explore/src/auto_explore/core/decider.py"),
            Path("auto_explore/src/auto_explore/core/dfs.py"),
        ]
        for path in module_files:
            content = path.read_text(encoding="utf-8")
            self.assertTrue(
                "def " in content or "class " in content,
                f"{path} should contain local implementations",
            )

    def test_auto_explore_source_no_longer_references_legacy_module(self):
        for path in Path("auto_explore/src/auto_explore").rglob("*.py"):
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("_legacy_auto_search", content, f"{path} still references legacy module")

    def test_prompting_reuses_runner_decider_prompt_module(self):
        content = Path("auto_explore/src/auto_explore/core/prompting.py").read_text(encoding="utf-8")
        self.assertIn("from prompts.decider_qwen3_e2e import", content)
        self.assertNotIn("e2e_qwen3_system.md", content)

    def test_snapshot_adapter_uses_auto_explore_snapshot_manager(self):
        content = Path("auto_explore/src/auto_explore/adapters/snapshot_init.py").read_text(encoding="utf-8")
        self.assertIn('"auto_explore" / "scripts" / "snapshot_manager.py"', content)
        self.assertNotIn('"MobileWorld" / "scripts" / "snapshot_manager.py"', content)
