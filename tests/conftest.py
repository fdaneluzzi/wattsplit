"""Make the engine importable without Home Assistant installed."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "wattsplit"))
