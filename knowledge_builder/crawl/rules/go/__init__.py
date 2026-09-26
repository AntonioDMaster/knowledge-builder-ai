"""go language rules, loaded from sibling .txt data files."""

from .._loader import load_rule

KEEP_EXT, SKIP_DIRS, KEEP_NAMES = load_rule(__file__)

__all__ = ["KEEP_EXT", "SKIP_DIRS", "KEEP_NAMES"]
