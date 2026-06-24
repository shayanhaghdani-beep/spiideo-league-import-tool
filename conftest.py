"""Ensure the app root is importable so `import league_dataload` works under
pytest without needing `pip install -e .`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
