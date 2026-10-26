"""Shared fixtures for golden evals. Deterministic: no model is called here."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
