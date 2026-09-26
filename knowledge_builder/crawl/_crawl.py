"""Crawl a repo into a single string for an LLM.

Keeps common source extensions, skips noise, and honors `.crawlignore` /
`.crawlwanted` files (gitignore syntax) in the crawl target; see README.md.
"""

import os
import re

import pathspec

__all__ = [
    "DEFAULT_KEEP_EXT",
    "DEFAULT_KEEP_NAMES",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_SKIP_DIR",
    "CrawlResult",
    "crawl",
    "crawl_with_stats",
    "estimate_tokens",
    "list_files",
    "safe_read",
]

# DEFAULT_* are rules/ unions, lazily resolved; refresh() clears the cache.

DEFAULT_MAX_FILE_BYTES = 500_000

_UNSET = object()

# Valueless declarations so linters accept the lazy __getattr__ names in __all__.
DEFAULT_KEEP_EXT: frozenset
DEFAULT_KEEP_NAMES: frozenset
DEFAULT_SKIP_DIR: frozenset


def __getattr__(name):
    if name in ("DEFAULT_KEEP_EXT", "DEFAULT_KEEP_NAMES", "DEFAULT_SKIP_DIR"):
        from . import rules

        value = getattr(rules, name)
        globals()[name] = value  # cache until refresh() clears it
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _wanted(filename, keep_ext, keep_names):
    if filename in keep_names:
        return True
    return os.path.splitext(filename)[1] in keep_ext


def _compile(patterns):
    """Compile a list of .gitignore-style patterns into a PathSpec, or None if empty."""
    if not patterns:
        return None
    return pathspec.GitIgnoreSpec.from_lines(patterns)


def _load_patterns(root, filename):
    """Load `filename` from `root` as [(negated, spec), ...]; None if absent."""
    path = os.path.join(root, filename)
    if not os.path.isfile(path):
        return None
    text = safe_read(path)
    if text is None:
        return None
    rules = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        pattern = line[1:] if negated else line
        rules.append((negated, pathspec.GitIgnoreSpec.from_lines([pattern])))
    return rules or None


def _pattern_decision(rel, rules, is_dir=False):
    """True = plain match, False = negated, None = no match.

    Dirs are also tested with a trailing slash so `build/` patterns match.
    """
    if rules is None:
        return None
    candidates = (rel, rel + "/") if is_dir else (rel,)
    decision = None
    for negated, spec in rules:
        if any(spec.match_file(c) for c in candidates):
            decision = not negated
    return decision


def _resolve_default(value, name):
    if value is not _UNSET:
        return value
    from . import rules

    return getattr(rules, name)


def _count_all_files(root):
    """find(1)-style on-disk file count under root, excluding .git.

    Unpruned by design (contrast with the filtered walk below); listings
    only, no content reads.
    """
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        total += len(filenames)
    return total


def _filtered_walk(
    root,
    *,
    keep_ext=_UNSET,
    skip_dirs=_UNSET,
    keep_names=_UNSET,
    max_file_bytes=DEFAULT_MAX_FILE_BYTES,
    include=None,
    exclude=None,
):
    """Walk the tree; return (paths that pass the filters, total files on disk).

    Filter args left unset default to the rules/ aggregates at call time;
    include / exclude are gitignore-style pattern lists applied after the
    default filters. No content read.
    """
    keep_ext = _resolve_default(keep_ext, "DEFAULT_KEEP_EXT")
    skip_dirs = _resolve_default(skip_dirs, "DEFAULT_SKIP_DIR")
    keep_names = _resolve_default(keep_names, "DEFAULT_KEEP_NAMES")

    out = []
    total = _count_all_files(root)
    skip = set(skip_dirs)  # pyright: ignore[reportArgumentType]
    include_spec = _compile(include)
    exclude_spec = _compile(exclude)
    ignore_rules = _load_patterns(root, ".crawlignore")
    wanted_rules = _load_patterns(root, ".crawlwanted")
    for dirpath, dirnames, filenames in os.walk(root):
        parent_rel = os.path.relpath(dirpath, root)
        kept = []
        for d in dirnames:
            dir_rel = d if parent_rel == "." else os.path.join(parent_rel, d)
            decision = _pattern_decision(dir_rel, ignore_rules, is_dir=True)
            if decision is True:
                continue  # .crawlignore always wins
            wanted = _pattern_decision(dir_rel, wanted_rules, is_dir=True)
            if decision is False or wanted is True or d not in skip:
                kept.append(d)
        dirnames[:] = kept
        for f in sorted(filenames):
            path = os.path.join(dirpath, f)
            rel = os.path.relpath(path, root)
            if _pattern_decision(rel, ignore_rules) is True:
                continue  # .crawlignore always wins
            wanted = _pattern_decision(rel, wanted_rules)
            if wanted is not True and not _wanted(f, keep_ext, keep_names):
                continue
            if include_spec is not None and not include_spec.match_file(rel):
                continue
            if exclude_spec is not None and exclude_spec.match_file(rel):
                continue
            if (
                max_file_bytes
                and wanted is not True
                and os.path.getsize(path) > max_file_bytes
            ):
                continue
            out.append(path)
    return out, total


def list_files(root, **kwargs):
    """Paths under root that pass the filters; same kwargs as _filtered_walk."""
    kept, _ = _filtered_walk(root, **kwargs)
    return kept


def safe_read(path):
    """Read a file as UTF-8; return None on decode or permission errors."""
    try:
        return open(path, encoding="utf-8").read()
    except UnicodeDecodeError, PermissionError:
        return None


_WORD_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def estimate_tokens(text):
    """Estimate LLM context tokens (word + punctuation regex).

    ~10-15% off real BPE for English prose; undercounts code and
    non-Latin text. For capacity checks, not billing.
    """
    return len(_WORD_RE.findall(text))


class CrawlResult:
    """Crawl output plus size statistics.

    text / char_count / token_count (estimated) describe the output
    string; file_count is files kept by the filters; total_files is the
    on-disk count under the root (excluding .git). Ignored files are
    never read, so they add nothing to the token estimate.
    """

    __slots__ = ("char_count", "file_count", "text", "token_count", "total_files")

    def __init__(self, text, file_count, total_files):
        self.text = text
        self.char_count = len(text)
        self.token_count = estimate_tokens(text)
        self.file_count = file_count
        self.total_files = total_files

    def __iter__(self):
        """Unpack as (text, char_count, token_count, file_count, total_files)."""
        yield self.text
        yield self.char_count
        yield self.token_count
        yield self.file_count
        yield self.total_files

    def __repr__(self):
        return (
            f"CrawlResult(file_count={self.file_count}, "
            f"total_files={self.total_files}, char_count={self.char_count}, "
            f"token_count={self.token_count})"
        )


def _render(root, files):
    parts = []
    for path in files:
        text = safe_read(path)
        if text is None:
            continue
        rel = os.path.relpath(path, root)
        parts.append(f"{'=' * 60}\nFile: {rel}\n{'=' * 60}\n{text}\n")
    return "\n".join(parts)


def crawl(root, **kwargs):
    """Walk root, read every kept file, return one concatenated string with file headers."""
    return _render(root, list_files(root, **kwargs))


def crawl_with_stats(root, **kwargs):
    """Like crawl(), but return a CrawlResult with file, char and token counts."""
    kept, total = _filtered_walk(root, **kwargs)
    return CrawlResult(_render(root, kept), file_count=len(kept), total_files=total)
