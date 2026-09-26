"""Tests for knowledge_builder.crawl filtering rules.

Each test builds an isolated tree under tmp_path so rule data in the
repo is never mutated; the lazy rules caches therefore need no refresh().
"""

import os

import pytest

from knowledge_builder.crawl import (
    CrawlResult,
    crawl,
    crawl_with_stats,
    estimate_tokens,
    list_files,
)


def make(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def tree(tmp_path):
    """Standard source tree covering the default rules.

    Returns (root, {rel_path: content}).
    """
    root = tmp_path / "repo"
    files = {
        "src/app.py": "print('hi')\n",
        "src/util.py": "x = 1\n",
        "src/nested/deep.go": "package main\n",
        "README.md": "# repo\n",
        "Dockerfile": "FROM python\n",
        "notes.txt": "plain text\n",
        "data.bin": "\x00\x01\x02binary\x03\n",
        "node_modules/pkg/index.js": "module.exports = 1;\n",
        "node_modules/pkg/sub/inner.js": "more;\n",
        "__pycache__/app.cpython-314.pyc": "bytecode\n",
        "build/out.py": "generated\n",
        "big.py": "x = 1\n" * 300_000,  # > max_file_bytes
    }
    for rel, text in files.items():
        make(root, rel, text)
    return root, files


def kept(root, **kwargs):
    return {os.path.relpath(p, root) for p in list_files(root, **kwargs)}  # pyright: ignore[reportCallIssue]


def test_keep_ext_and_keep_names(tree):
    root, _ = tree
    got = kept(root)
    # .py / .go / .md / .txt kept by keep_ext; Dockerfile by keep_names
    assert {
        "src/app.py",
        "src/util.py",
        "src/nested/deep.go",
        "README.md",
        "notes.txt",
        "Dockerfile",
    } <= got
    # .bin is not in the default keep sets
    assert "data.bin" not in got


def test_skip_dirs_prunes_recursively(tree):
    root, _ = tree
    got = kept(root)
    assert not any(p.startswith("node_modules/") for p in got)
    assert not any(p.startswith("__pycache__/") for p in got)
    assert not any(p.startswith("build/") for p in got)


def test_max_file_bytes_cap(tree):
    root, _ = tree
    got = kept(root)
    assert "big.py" not in got
    # small files unaffected
    assert "src/app.py" in got


def test_total_vs_kept_counts(tree):
    root, files = tree
    result = crawl_with_stats(root)  # pyright: ignore[reportCallIssue]
    # total_files is the find(1)-style count of ALL files on disk,
    # including those in pruned dirs (node_modules/, __pycache__/, build/)
    assert result.total_files == len(files)
    assert 0 < result.file_count < result.total_files
    assert result.file_count == len(list_files(root))  # pyright: ignore[reportCallIssue]


def test_total_files_excludes_git_metadata(tmp_path):
    root = tmp_path / "repo"
    make(root, "a.py", "a\n")
    make(root, ".git/HEAD", "ref: refs/heads/main\n")
    make(root, ".git/objects/ab/cdef", "\x00\x01\x02")

    result = crawl_with_stats(root)  # pyright: ignore[reportCallIssue]
    assert result.total_files == 1  # .git/ contents are not counted
    assert result.file_count == 1


def test_crawlignore_file(tmp_path):
    root = tmp_path / "repo"
    make(root, "src/keep.py", "a = 1\n")
    make(root, "src/drop.py", "b = 2\n")
    (root / ".crawlignore").write_text("src/drop.py\n")

    got = kept(root)
    assert "src/keep.py" in got
    assert "src/drop.py" not in got


def test_crawlignore_dir_only_pattern(tmp_path):
    root = tmp_path / "repo"
    make(root, "gen/a/x.py", "x\n")
    make(root, "keep.py", "k\n")
    (root / ".crawlignore").write_text("gen/\n")

    assert kept(root) == {"keep.py"}


def test_crawlignore_negation(tmp_path):
    root = tmp_path / "repo"
    for name in ("a.py", "b.py", "c.py"):
        make(root, f"src/{name}", name)
    (root / ".crawlignore").write_text("src/*.py\n!src/b.py\n")

    assert kept(root) == {"src/b.py"}


def test_crawlignore_wins_over_crawlwanted(tmp_path):
    root = tmp_path / "repo"
    make(root, "src/rescued.bin", "ignored regardless\n")
    (root / ".crawlignore").write_text("src/rescued.bin\n")
    (root / ".crawlwanted").write_text("*.bin\n")

    assert kept(root) == set()


def test_crawlwanted_bypasses_filters(tmp_path):
    root = tmp_path / "repo"
    make(root, "special.noext", "wanted\n")
    make(root, "huge.noext", "x" * 600_000)  # over max_file_bytes
    (root / ".crawlwanted").write_text("*.noext\n")

    assert kept(root) == {"special.noext", "huge.noext"}


def test_include_restricts(tmp_path):
    root = tmp_path / "repo"
    make(root, "src/core/a.go", "a\n")
    make(root, "src/other/b.go", "b\n")
    make(root, "c.py", "c\n")

    got = kept(root, include=["src/core/**"])
    assert got == {os.path.join("src", "core", "a.go")}


def test_exclude_drops_after_include(tmp_path):
    root = tmp_path / "repo"
    make(root, "src/a.py", "a\n")
    make(root, "src/a_test.py", "t\n")

    got = kept(root, include=["src/**"], exclude=["*_test.py"])
    assert got == {"src/a.py"}


def test_explicit_kwargs_override_defaults(tmp_path):
    root = tmp_path / "repo"
    make(root, "src/a.py", "a\n")
    make(root, "src/b.txt", "b\n")

    got = kept(root, keep_ext={".txt"})
    assert got == {"src/b.txt"}


def test_crawl_text_format(tmp_path):
    root = tmp_path / "repo"
    make(root, "a.py", "one\ntwo\n")

    text = crawl(root)  # pyright: ignore[reportCallIssue]
    assert "File: a.py" in text
    assert "one\ntwo" in text
    assert "=" * 60 in text


def test_crawlresult_stats(tmp_path):
    root = tmp_path / "repo"
    make(root, "a.py", "hello world\n")
    make(root, "b.bin", "\x00\x01\n")

    result = crawl_with_stats(root)  # pyright: ignore[reportCallIssue]
    assert isinstance(result, CrawlResult)  # pyright: ignore[reportArgumentType]
    assert result.file_count == 1
    assert result.total_files == 2
    assert result.char_count == len(result.text)
    assert result.token_count == estimate_tokens(result.text)  # pyright: ignore[reportCallIssue]
    # tuple unpacking contract
    text, chars, tokens, files, total = result
    assert (text, chars, tokens, files, total) == (
        result.text,
        result.char_count,
        result.token_count,
        result.file_count,
        result.total_files,
    )


def test_estimate_tokens():
    assert (
        estimate_tokens("How are you?") == 4  # pyright: ignore[reportCallIssue]
    )  # 3 words + '?'
    assert estimate_tokens("") == 0  # pyright: ignore[reportCallIssue]
    assert estimate_tokens("one two three") == 3  # pyright: ignore[reportCallIssue]
    assert (
        estimate_tokens("a,b.c") == 5  # pyright: ignore[reportCallIssue]
    )  # a , b . c
