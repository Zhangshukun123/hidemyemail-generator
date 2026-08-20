from __future__ import annotations

import sys
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parent
for path in (ROOT / "src", PROJECT):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)
