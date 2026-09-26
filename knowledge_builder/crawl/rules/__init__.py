"""File filtering rules for the crawler, split out per language.

    rules/common/       language-agnostic rules
    rules/<language>/   one data-driven subpackage per language

Each rule subpackage exposes three frozensets -- KEEP_EXT, SKIP_DIRS,
KEEP_NAMES -- loaded from sibling ext.txt / skip_dirs.txt / keep_names.txt
(see templates/crawl/rules/lang_rule/). Adding a language: copy the
template directory and fill in the .txt files; discovery is automatic.

LANGUAGES and DEFAULT_* are lazy via module __getattr__ (PEP 562):
importing this package alone reads no rule data.
"""

import importlib
import pkgutil

# aggregate name -> per-module attribute it unions
_LAZY_AGGREGATES = {
    "DEFAULT_KEEP_EXT": "KEEP_EXT",
    "DEFAULT_SKIP_DIR": "SKIP_DIRS",
    "DEFAULT_KEEP_NAMES": "KEEP_NAMES",
}

__all__ = ["LANGUAGES", *_LAZY_AGGREGATES]  # pyright: ignore[reportUnsupportedDunderAll]


def _language_modules():
    for mod in pkgutil.iter_modules(__path__):
        if mod.ispkg and mod.name != "common":
            yield importlib.import_module(f".{mod.name}", __name__)


def __getattr__(name):
    if name == "LANGUAGES":
        # name -> module, e.g. LANGUAGES["python"].KEEP_EXT
        value = {m.__name__.rpartition(".")[2]: m for m in _language_modules()}
    elif name in _LAZY_AGGREGATES:
        from . import common

        attr = _LAZY_AGGREGATES[name]
        value = getattr(common, attr, frozenset())
        for module in _language_modules():
            value |= getattr(module, attr, frozenset())
        value = frozenset(value)
    else:
        try:
            # rule subpackages (rules.python, ...) on direct attribute access
            value = importlib.import_module(f".{name}", __name__)
        except ModuleNotFoundError:
            raise AttributeError(
                f"module {__name__!r} has no attribute {name!r}"
            ) from None
    globals()[name] = value  # cache: next access skips the indirection
    return value


def __dir__():
    return sorted(set(globals()) | {"LANGUAGES", *_LAZY_AGGREGATES})
