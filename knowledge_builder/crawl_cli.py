"""CLI entry point: knowledge-builder <crawl_target_path> [<output_file_path>]

Crawls the target directory; prints to stdout, or writes the output file
when one is given. Stats go to stderr in both modes.
"""

from __future__ import annotations

import sys

from knowledge_builder.crawl import crawl_with_stats


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if not 1 <= len(args) <= 2:
        print(__doc__.strip(), file=sys.stderr)  # pyright: ignore[reportOptionalMemberAccess]
        raise SystemExit(2)

    result = crawl_with_stats(args[0])  # pyright: ignore[reportCallIssue]
    if len(args) == 2:
        with open(args[1], "w", encoding="utf-8") as f:
            f.write(result.text)
    else:
        print(result.text)

    print(
        f"[knowledge-builder] files={result.file_count}/{result.total_files} "
        f"chars={result.char_count} ~tokens={result.token_count}",
        file=sys.stderr,
    )
