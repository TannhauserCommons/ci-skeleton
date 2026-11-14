"""Tests for the fixture app."""
import sys
from pathlib import Path

# Why: the app lives in fixture/, which is not on the path when the tests are
# found from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
