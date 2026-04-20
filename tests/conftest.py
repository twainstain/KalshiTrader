"""Pytest bootstrap — wire `src/` and `lib/trading_platform/src/` onto `sys.path`.

`pyproject.toml` sets this too, but running `pytest tests/` directly from a
dev shell that didn't `pip install -e .` still needs it.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

for rel in ("src", "lib/trading_platform/src", "scripts"):
    p = _REPO_ROOT / rel
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))
