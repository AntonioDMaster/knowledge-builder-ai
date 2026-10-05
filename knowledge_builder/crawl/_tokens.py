"""Token estimator kept in its own dep-free module so `_crawl` (stats)
and `_smart` (coverage footer) can share it without a dependency cycle."""

import re

__all__ = ["estimate_tokens"]

_WORD_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def estimate_tokens(text):
    """Estimate LLM context tokens (word + punctuation regex).

    ~10-15% off real BPE for English prose; undercounts code and
    non-Latin text. For capacity checks, not billing.
    """
    return len(_WORD_RE.findall(text))
