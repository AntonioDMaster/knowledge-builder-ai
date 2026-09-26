"""<LANGUAGE_NAME> language rules.

To add a language: copy this directory to
knowledge_builder/crawl/rules/<lang_name>/ and fill in the .txt files.
Discovery is automatic; no other file needs to change.
"""

from .._loader import load_rule  # pyright: ignore[reportMissingImports]

KEEP_EXT, SKIP_DIRS, KEEP_NAMES = load_rule(__file__)

__all__ = ["KEEP_EXT", "SKIP_DIRS", "KEEP_NAMES"]
