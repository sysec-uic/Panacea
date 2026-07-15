import sys
from pathlib import Path

# Make arvo-eval/ importable so tests use the same bare imports as the scripts.
_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_root))
# transfer_*.py / crash_taxonomy.py / arvo_bugs.py live in transfer/ (moved out of
# the root during the 2026-07-13 cleanup); their tests still use bare imports.
sys.path.insert(0, str(_root / "transfer"))
