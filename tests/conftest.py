from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Some modules live under py_modules in Decky projects
sys.path.insert(0, str(ROOT / "py_modules"))
