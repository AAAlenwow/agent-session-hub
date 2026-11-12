import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
for sub in ("server", "client", "mcp", "hooks"):
    p = str(ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)
