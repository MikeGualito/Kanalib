"""Shared path bootstrap so the examples run from a checkout uninstalled."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
