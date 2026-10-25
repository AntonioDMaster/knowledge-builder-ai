"""Tests for the crawl CLI: help flags, --max-file-bytes parsing, usage
errors, and the two output modes. crawl_with_stats is monkeypatched
(calls recorded as (root, kwargs)) except in the two tmp_path
integration tests; the module-execution test drives the `python -m`
path via runpy with sys.argv patched to a help request.
"""

import runpy
import sys

import pytest

from knowledge_builder import crawl_cli
from knowledge_builder.crawl import CrawlResult, estimate_tokens

_TOKENS = estimate_tokens("bundle-text")  # pyright: ignore[reportCallIssue]
_STATS = f"[knowledge-builder] files=2/5 chars=11 ~tokens={_TOKENS}\n"
_DOC = crawl_cli.__doc__ or ""


@pytest.fixture
def fake_crawl(monkeypatch):
    calls = []

    def _crawl(root, **kwargs):
        calls.append((root, kwargs))
        return CrawlResult("bundle-text", 2, 5)  # pyright: ignore[reportCallIssue]

    monkeypatch.setattr(crawl_cli, "crawl_with_stats", _crawl)
    return calls


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_prints_docstring_to_stdout(flag, fake_crawl, capsys):
    crawl_cli.main([flag])
    captured = capsys.readouterr()
    assert captured.out == _DOC.strip() + "\n"
    assert captured.err == ""
    assert fake_crawl == []


def test_help_beats_positional_args(fake_crawl, capsys):
    crawl_cli.main(["/some/root", "--help"])
    captured = capsys.readouterr()
    assert captured.out == _DOC.strip() + "\n"
    assert fake_crawl == []


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["/root", "--max-file-bytes", "1000"], 1000),
        (["/root", "--max-file-bytes=1000"], 1000),
        (["--max-file-bytes", "1000", "/root"], 1000),
        (["/root", "--max-file-bytes", "10", "--max-file-bytes", "20"], 20),
        (["/root", "--max-file-bytes", "0"], 0),
    ],
)
def test_max_file_bytes_flag_passthrough(argv, expected, fake_crawl):
    crawl_cli.main(argv)
    assert fake_crawl == [("/root", {"max_file_bytes": expected})]


def test_max_file_bytes_with_output_file(fake_crawl, tmp_path):
    out = tmp_path / "bundle.txt"
    crawl_cli.main(["/some/root", str(out), "--max-file-bytes", "1000"])
    assert out.read_text(encoding="utf-8") == "bundle-text"
    assert fake_crawl == [("/some/root", {"max_file_bytes": 1000})]


@pytest.mark.parametrize(
    "argv, message",
    [
        (["/root", "--max-file-bytes"], "needs a value"),
        (["/root", "--max-file-bytes", "abc"], "non-negative integer"),
        (["/root", "--max-file-bytes", "-5"], "non-negative integer"),
        (["/root", "--max-file-bytes", "3.5"], "non-negative integer"),
        (["/root", "--max-file-bytes="], "non-negative integer"),
    ],
)
def test_max_file_bytes_usage_errors(argv, message, fake_crawl, capsys):
    with pytest.raises(SystemExit) as excinfo:
        crawl_cli.main(argv)
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err
    assert message in captured.err
    assert _DOC.strip() in captured.err
    assert fake_crawl == []


def test_max_file_bytes_compacts_oversized_file(tmp_path, capsys):
    (tmp_path / "code.py").write_text("x = 1\n" * 200, encoding="utf-8")
    crawl_cli.main([str(tmp_path), "--max-file-bytes", "100"])
    assert "[SMART CRAWL:" in capsys.readouterr().out


def test_max_file_bytes_zero_disables_cap(tmp_path, capsys):
    (tmp_path / "code.py").write_text("x = 1\n" * 200, encoding="utf-8")
    crawl_cli.main([str(tmp_path), "--max-file-bytes", "0"])
    out = capsys.readouterr().out
    assert "[SMART CRAWL:" not in out
    assert "x = 1" in out


@pytest.mark.parametrize("argv", [[], ["a", "b", "c"]])
def test_bad_usage_prints_docstring_to_stderr_exits_2(argv, capsys):
    with pytest.raises(SystemExit) as excinfo:
        crawl_cli.main(argv)
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert _DOC.strip() in captured.err
    assert captured.out == ""


def test_one_arg_prints_bundle_to_stdout(fake_crawl, capsys):
    crawl_cli.main(["/some/root"])
    captured = capsys.readouterr()
    assert captured.out == "bundle-text\n"
    assert captured.err == _STATS
    assert fake_crawl == [("/some/root", {})]


def test_two_args_write_output_file(fake_crawl, capsys, tmp_path):
    out = tmp_path / "bundle.txt"
    crawl_cli.main(["/some/root", str(out)])
    assert out.read_text(encoding="utf-8") == "bundle-text"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == _STATS


def test_module_execution_shows_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["crawl_cli", "--help"])
    # runpy re-executes an already-imported module only noisily; dropping
    # it reproduces the fresh-interpreter state of a real `python -m` run.
    monkeypatch.delitem(sys.modules, "knowledge_builder.crawl_cli")
    runpy.run_module("knowledge_builder.crawl_cli", run_name="__main__")
    assert _DOC.strip() in capsys.readouterr().out
