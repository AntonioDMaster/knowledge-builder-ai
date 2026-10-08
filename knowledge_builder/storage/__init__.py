"""Credentials storage: each OS's native secret store behind one tiny
zero-dependency API -- Secret Service via secret-tool on Linux, Keychain
via the security CLI on macOS, Credential Manager via ctypes on Windows.
Lazy PEP 562 re-exports like the crawl and llm packages; nothing is
cached between calls, so there is no refresh().

Secrets never pass through argv, environment, or disk in the clear, and
there is no fallback store: an unreachable keychain is a
SecretStoreError, never a silent downgrade. Seeding is interactive
(`python -m knowledge_builder.storage_cli set <name>`). See README.md.
"""

import importlib

_LAZY_EXPORTS = {
    "SecretStoreError": "_keychain",
    "get_secret": "_keychain",
    "set_secret": "_keychain",
    "delete_secret": "_keychain",
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
