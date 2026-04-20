from __future__ import annotations

import sys
from pathlib import Path


def _ensure_repo_root_on_syspath() -> None:
    repo_root = str(Path(__file__).resolve().parents[3])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)


_ensure_repo_root_on_syspath()
