"""pytest bootstrap: expose the project root and the vendored analysis cores."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for path in (ROOT, ROOT / "vendor"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
