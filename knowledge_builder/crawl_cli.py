"""CLI entry point: python -m knowledge_builder.crawl_cli <crawl_target_path> [<output_file_path>]

Crawls the target directory; prints to stdout, or writes the output file
when one is given. Stats go to stderr in both modes.

Options:
  --max-file-bytes N  byte cap above which a file renders as a
                      navigational digest instead of full text
                      (default 500000; 0 disables the cap entirely).
                      --max-file-bytes=N is accepted too.

`-h`/`--help` prints this text to stdout; bad usage prints it to stderr
and exits 2.
"""

from __future__ import annotations

import sys
from typing import NoReturn

from knowledge_builder.crawl import crawl_with_stats

_MAX_FILE_BYTES_FLAG = "--max-file-bytes"


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if "-h" in args or "--help" in args:
        print(__doc__.strip())  # pyright: ignore[reportOptionalMemberAccess]
        return

    positionals, max_file_bytes = _parse_args(args)
    if not 1 <= len(positionals) <= 2:
        _usage("expected <crawl_target_path> [<output_file_path>]")

    kwargs: dict[str, int] = {}
    if max_file_bytes is not None:
        kwargs["max_file_bytes"] = max_file_bytes
    result = crawl_with_stats(positionals[0], **kwargs)  # pyright: ignore[reportCallIssue]
    if len(positionals) == 2:
        with open(positionals[1], "w", encoding="utf-8") as f:
            f.write(result.text)
    else:
        print(result.text)

    print(
        f"[knowledge-builder] files={result.file_count}/{result.total_files} "
        f"chars={result.char_count} ~tokens={result.token_count}",
        file=sys.stderr,
    )


def _parse_args(args: list[str]) -> tuple[list[str], int | None]:
    """Split argv into positionals plus the --max-file-bytes value (None = unset)."""
    positionals: list[str] = []
    max_file_bytes: int | None = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == _MAX_FILE_BYTES_FLAG:
            if i + 1 == len(args):
                _usage(f"{_MAX_FILE_BYTES_FLAG} needs a value")
            value = args[i + 1]
            i += 2
        elif arg.startswith(_MAX_FILE_BYTES_FLAG + "="):
            value = arg.partition("=")[2]
            i += 1
        else:
            positionals.append(arg)
            i += 1
            continue
        max_file_bytes = _parse_non_negative(value)
    return positionals, max_file_bytes


def _parse_non_negative(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        _usage(f"{_MAX_FILE_BYTES_FLAG} wants a non-negative integer, got {raw!r}")
    if value < 0:
        _usage(f"{_MAX_FILE_BYTES_FLAG} wants a non-negative integer, got {raw!r}")
    return value


def _usage(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    print(__doc__.strip(), file=sys.stderr)  # pyright: ignore[reportOptionalMemberAccess]
    raise SystemExit(2)


if __name__ == "__main__":
    main()
