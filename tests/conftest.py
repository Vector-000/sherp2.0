import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if "cogs" not in sys.modules:
    _cogs_pkg = types.ModuleType("cogs")
    _cogs_pkg.__path__ = [str(ROOT / "cogs")]
    sys.modules["cogs"] = _cogs_pkg
