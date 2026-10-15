"""Travel Storyboard backend package."""

from pathlib import Path

__version__ = (Path(__file__).resolve().parents[2] / "VERSION").read_text(encoding="utf-8").strip()
