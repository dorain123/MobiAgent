import importlib.util
from pathlib import Path


def load_snapshot_manager_client():
    script_path = Path(__file__).resolve().parents[4] / "auto_explore" / "scripts" / "snapshot_manager.py"
    spec = importlib.util.spec_from_file_location("mobileworld_snapshot_manager", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 snapshot_manager.py: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SnapshotManagerClient
