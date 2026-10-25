"""Tests for the crawl CLI: help flags, usage errors, and the two output
modes. crawl_with_stats is monkeypatched so no real crawl runs; the
module-execution test drives the `python -m` path via runpy with
sys.argv patched to a help request.
"""

import runpy
import sys

import pytest

from knowledge_builder import crawl_cli
from knowledge_builder.crawl import CrawlResult, estimate_tokens

_STATS = f"[knowledge-builder] files=2/5 chars=11 ~tokens={estimate_tokens('bundle-text')}\n"


@pytest.fixture
def fake_crawl(monkeypatch):
    calls = []

    def _crawl(root):
        calls.append(root)
        return CrawlResult("bundle-text", 2, 5)

    monkeypatch.setattr(crawl_cli, "crawl_with_stats", _crawl)
    return calls


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_prints_docstring_to_stdout(flag, fake_crawl, capsys):
    crawl_cli.main([flag])
    captured = capsys.readouterr()
    assert captured.out == crawl_cli.__doc__.strip() + "\n"
    assert captured.err == ""
    assert fake_crawl == []


def test_help_beats_positional_args(fake_crawl, capsys):
    crawl_cli.main(["/some/root", "--help"])
    captured = capsys.readouterr()
    assert captured.out == crawl_cli.__doc__.strip() + "\n"
    assert fake_crawl == []


@pytest.mark.parametrize("argv", [[], ["a", "b", "c"]])
def test_bad_usage_prints_docstring_to_stderr_exits_2(argv, capsys):
    with pytest.raises(SystemExit) as excinfo:
        crawl_cli.main(argv)
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert crawl_cli.__doc__.strip() in captured.err
    assert captured.out == ""


def test_one_arg_prints_bundle_to_stdout(fake_crawl, capsys):
    crawl_cli.main(["/some/root"])
    captured = capsys.readouterr()
    assert captured.out == "bundle-text\n"
    assert captured.err == _STATS
    assert fake_crawl == ["/some/root"]


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
    assert crawl_cli.__doc__.strip() in capsys.readouterr().out
