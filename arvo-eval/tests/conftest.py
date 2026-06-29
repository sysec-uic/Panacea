import sys
from pathlib import Path

# Make arvo-eval/ importable so tests use the same bare imports as the scripts.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
