#!/usr/bin/env python
"""离线自测： ``python tools/selftest.py``（不碰网络，不读 .env）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    suite = unittest.defaultTestLoader.discover(
        str(ROOT / "tools" / "tests"), pattern="test_*.py", top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
