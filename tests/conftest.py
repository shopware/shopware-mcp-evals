"""Put the repo root and functional/ on sys.path so tests can import the
shared client, the reporting harness, and the functional runner directly."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "functional"))
