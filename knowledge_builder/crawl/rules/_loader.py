"""Loader for data-driven language rule directories.

A rule directory holds optional line-based .txt files (ext.txt,
skip_dirs.txt, keep_names.txt): one entry per line, blank lines and `#`
comments ignored, a missing file counts as an empty set.
"""

import os


def _read_set(path):
    if not os.path.exists(path):
        return frozenset()
    items = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                items.append(line)
    return frozenset(items)


def load_rule(package_file):
    """Read the .txt rule files sibling to `package_file` (an __init__.py path)."""
    d = os.path.dirname(package_file)
    return (
        _read_set(os.path.join(d, "ext.txt")),
        _read_set(os.path.join(d, "skip_dirs.txt")),
        _read_set(os.path.join(d, "keep_names.txt")),
    )
