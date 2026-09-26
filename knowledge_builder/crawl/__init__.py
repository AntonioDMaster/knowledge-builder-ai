"""Repo crawling package.

Public names re-export lazily via module __getattr__ (PEP 562): importing
this package touches neither the rules data nor `pathspec` until a public
name is used. Rule data is cached after first access; refresh() re-reads
it after rule-directory changes.
"""

import importlib
import sys
import types

# name -> submodule providing it
_LAZY_EXPORTS = {
    "rules": "rules",
    "crawl": "_crawl",
    "crawl_with_stats": "_crawl",
    "CrawlResult": "_crawl",
    "estimate_tokens": "_crawl",
    "list_files": "_crawl",
    "safe_read": "_crawl",
    "DEFAULT_KEEP_EXT": "_crawl",
    "DEFAULT_KEEP_NAMES": "_crawl",
    "DEFAULT_SKIP_DIR": "_crawl",
    "DEFAULT_MAX_FILE_BYTES": "_crawl",
}

# names cached by rules/__init__.py and _crawl.py module __getattr__s
_RULE_CACHES = (
    "LANGUAGES",
    "DEFAULT_KEEP_EXT",
    "DEFAULT_SKIP_DIR",
    "DEFAULT_KEEP_NAMES",
)

__all__ = [*_LAZY_EXPORTS, "refresh"]  # pyright: ignore[reportUnsupportedDunderAll]


def __getattr__(name):
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f".{_LAZY_EXPORTS[name]}", __name__)
    if name == "rules":
        value = module  # the submodule itself, not an attribute of it
    else:
        value = getattr(module, name)
    globals()[name] = value  # cache: next access skips the indirection
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


def refresh():
    """Forget cached rule data so rules/ changes are picked up.

    Later LANGUAGES / DEFAULT_* accesses and list_files() / crawl() calls
    rebuild from the current .txt files. Data already held in local
    variables elsewhere stays stale; importlib.reload alone is not enough
    (cached __getattr__ values survive it).
    """
    pkg = __name__
    for name in _LAZY_EXPORTS:
        globals().pop(name, None)

    crawl_mod = sys.modules.get(f"{pkg}._crawl")
    if crawl_mod is not None:
        for name in _RULE_CACHES:
            if name != "LANGUAGES":
                crawl_mod.__dict__.pop(name, None)

    rules_mod = sys.modules.get(f"{pkg}.rules")
    if rules_mod is not None:
        for name in _RULE_CACHES:
            rules_mod.__dict__.pop(name, None)
        # drop cached subpackage attributes (common, python, ...) so direct
        # access like rules.python re-imports and re-reads the .txt files;
        # stdlib module references (os, pkgutil, ...) are left untouched
        for name, value in list(vars(rules_mod).items()):
            if isinstance(value, types.ModuleType) and value.__name__.startswith(
                f"{pkg}.rules."
            ):
                delattr(rules_mod, name)

    prefix = f"{pkg}.rules."
    for mod_name in [n for n in sys.modules if n.startswith(prefix)]:
        del sys.modules[mod_name]
