"""LLM connection stack; see README.md for the contract. Lazy PEP 562
re-exports; nothing is cached between calls, so there is no refresh().
"""

import importlib

_LAZY_EXPORTS = {
    "LLMConfig": "_client",
    "LLMError": "_client",
    "call_llm": "_client",
    "chat": "_client",
    "clear_cache": "_client",
    "read_prompt": "_plumbing",
    "fill": "_plumbing",
    "extract_mermaid": "_plumbing",
    "parse_json": "_plumbing",
    "parse_yaml": "_plumbing",
    "json_call": "_plumbing",
    "yaml_call": "_plumbing",
}

__all__ = [*_LAZY_EXPORTS]  # pyright: ignore[reportUnsupportedDunderAll]  # noqa: PLE0604


def __getattr__(name):
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f".{_LAZY_EXPORTS[name]}", __name__)
    value = getattr(module, name)
    globals()[name] = value  # cache: next access skips the indirection
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
