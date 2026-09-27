"""Tests for the smart compaction core (_smart.py) and crawl() wiring."""

import re
import time

import knowledge_builder.crawl as crawl_pkg
from knowledge_builder.crawl import _smart

MAP_ROW_RE = re.compile(r"^((?:async )?\w+) ([\w:.]+) \[L(\d+)-(\d+)\]$", re.MULTILINE)
COVERAGE_RE = re.compile(r"Indexed (\d+)/(\d+) declarations; detailed (\d+)/(\d+)")


def _big_python(n_classes=1500):
    """~400 KB of valid Python: classes with entry/private/async methods."""
    parts = ['"""Generated large module for compaction tests."""\n\nimport json\nimport os\n\nLIMIT = 10\n']
    for i in range(n_classes):
        parts.append(
            f"\nclass C{i}:\n"
            f"    \"\"\"Service number {i}.\"\"\"\n\n"
            f"    def load(self, payload):\n"
            f"        return {{\"id\": payload.get(\"id\"), \"n\": {i}}}\n\n"
            f"    def _helper(self, x):\n"
            f"        return x * {i + 1}\n\n"
            f"    async def run(self, items):\n"
            f"        out = [self.load(i) for i in items]\n"
            f"        return self._helper(len(out))\n"
        )
    parts.append("\ndef main():\n    return C0().load({})[\"n\"]\n")
    return "".join(parts)


def _big_go(n=800):
    """~180 KB of Go-style brace source: structs, methods, initializers."""
    out = ["package big\n\nimport (\n\t\"fmt\"\n)\n\n\n"]
    for i in range(n):
        out.append(
            f"\ntype T{i} struct {{\n"
            f"\tID   int\n"
            f"\tName string\n}}\n\n"
            f"func (t *T{i}) Load(x int) int {{\n"
            f"\tcfg := Config{{Retries: {i}, Name: \"t{i}\"}}\n"
            f"\treturn t.ID + x + cfg.Retries\n}}\n\n"
            f"func NewT{i}() *T{i} {{ return &T{i}{{}} }}\n"
        )
    return "".join(out)


def _big_yaml(n=2500):
    """~230 KB of colon/indent YAML with nested lists and block scalars."""
    out = ["# generated yaml fixture\n"]
    for i in range(n):
        out.append(
            f"svc_{i:04d}:\n"
            f"  kind: service\n"
            f"  window: {i}\n"
            f"  endpoints:\n"
            f"    - code: e{i}\n"
            f"      url: http://host:{8000 + i}/v1\n"
            f"      notes: |\n"
            f"        colons: everywhere: like: this\n"
        )
    return "".join(out)


def _big_prose(n=1500):
    """~150 KB of structureless prose; lines occasionally collide with
    declaration keywords ("class diagram") to stress the plain outline."""
    words = ["service", "handler", "gateway", "registry", "cache", "stream", "pipeline", "audit", "ledger"]
    out = []
    for i in range(n):
        sentence = " ".join(words[(i + j) % len(words)] for j in range(14)).capitalize() + "."
        out.append(sentence)
        if i % 13 == 0:
            out.append("A handler class diagram documents this stage.")
    return "\n".join(out) + "\n"

C_SAMPLE = """#include <stdio.h>
#define MAX 10

int counter = 0;

int add(int a, int b) { return a + b; }

struct Point { int x; int y; };

class Shape {
public:
    void draw() { render_all(); }
    int area() { return _w * _h; }
private:
    int _w, _h;
};

int main(int argc, char** argv) {
    if (argc > 1) { counter = add(counter, MAX); }
    return 0;
}
"""

PY_SAMPLE = """import os

MAX = 10

def add(a, b):
    return a + b

class Shape:
    def area(self):
        return self._w * self._h

def main():
    s = Shape()
    if s:
        total = add(1, 2)
    return total
"""


def test_mask_preserves_positions():
    src = 'int x = 1; // } comment {\nchar *s = "a}b{";\n# hash } here\nint y = 2;\n'
    masked = _smart._mask(src)
    assert len(masked) == len(src)
    assert masked.count("\n") == src.count("\n")
    assert "}" not in masked and "{" not in masked
    assert "int x = 1;" in masked
    assert "int y = 2;" in masked


def test_brace_backend_on_c_sample():
    out = _smart.compact_text(C_SAMPLE, name="sample.c", ext=".c")
    assert "Backend: brace" in out
    assert "# Architecture digest: sample.c" in out
    assert "#include <stdio.h>" in out
    assert "int counter = 0;" in out
    for decl in ("function add", "struct Point", "class Shape", "function main"):
        assert decl in out, decl
    # nested methods indexed; if-blocks are not declarations
    assert "function draw" in out
    assert "function area" in out
    assert "function if" not in out
    signals = out.split("## Implementation signals", 1)[1]
    assert "function main" in signals  # entry name floats to the detail section
    assert "calls: add" in signals


def test_indent_backend_on_python_sample():
    out = _smart.compact_text(PY_SAMPLE, name="sample.py", ext=".pythonish", backend="indent")
    assert "Backend: indent" in out
    assert "import os" in out
    assert "MAX = 10" in out
    for decl in ("def add", "class Shape", "def area", "def main"):
        assert decl in out, decl
    assert "block if" not in out and "def if" not in out
    signals = out.split("## Implementation signals", 1)[1]
    assert "calls: Shape, add" in signals


def test_ast_backend_via_python_rules():
    # rules/python/smart_backend.txt selects the exact ast backend
    out = _smart.compact_text(PY_SAMPLE, name="sample.py", ext=".py")
    assert "Backend: ast" in out
    assert "def main" in out
    assert "doc:" not in out  # sample has no docstrings


def test_plain_backend_on_structureless_text():
    out = _smart.compact_text("just prose\nno structure\nvalue 3\n", name="d.txt")
    assert "Backend: plain" in out
    assert "## Outline" in out


def test_budget_caps_output():
    out = _smart.compact_text(C_SAMPLE, name="s.c", ext=".c", budget=300)
    assert len(out) <= 300 * 3
    assert out.startswith("# Architecture digest")


def test_budget_too_small_raises():
    import pytest

    with pytest.raises(ValueError):
        _smart.compact_text(C_SAMPLE, name="s.c", ext=".c", budget=100)


def test_invalid_tuning_regex_is_skipped_with_warning(tmp_path):
    common = tmp_path / "common"
    common.mkdir()
    (common / "smart_imports.txt").write_text("^[unclosed\n^\\s*ok_include\b\n")
    out = _smart.compact_text(
        "ok_include <x>\n", name="s.x", backend="plain", rules_root=str(tmp_path)
    )
    assert "skipped invalid pattern" in out
    assert "ok_include" in out


def test_crawl_smart_compacts_oversized_files(tmp_path):
    (tmp_path / "small.py").write_text("x = 1\n")
    (tmp_path / "big.py").write_text("def main():\n    return 42\n" * 50)
    text = crawl_pkg.crawl(tmp_path, smart=True, max_file_bytes=100)  # pyright: ignore[reportCallIssue]
    assert "SMART CRAWL" in text
    assert "Architecture digest: big.py" in text
    assert "def main" in text
    assert "x = 1" in text  # small files unaffected
    # smart compaction is the default: no kwarg needed
    default = crawl_pkg.crawl(tmp_path, max_file_bytes=100)  # pyright: ignore[reportCallIssue]
    assert "Architecture digest: big.py" in default
    # opting out drops the oversized file again
    plain = crawl_pkg.crawl(tmp_path, smart=False, max_file_bytes=100)  # pyright: ignore[reportCallIssue]
    assert "big.py" not in plain
    assert "small.py" in plain


def test_cap_boundary_only_strictly_over_cap_compacts(tmp_path):
    # exactly at the cap -> included in full; one byte over -> digest
    cap = crawl_pkg.DEFAULT_MAX_FILE_BYTES
    (tmp_path / "small.py").write_text("x = 1\n")
    (tmp_path / "at_cap.py").write_bytes(b"# " + b"a" * (cap - 2))  # pyright: ignore[reportOperatorIssue]
    (tmp_path / "over_cap.py").write_bytes(b"# " + b"a" * (cap - 1))  # pyright: ignore[reportOperatorIssue]
    text = crawl_pkg.crawl(tmp_path)  # pyright: ignore[reportCallIssue]
    assert "x = 1" in text
    assert (tmp_path / "at_cap.py").read_text() in text  # whole file, not a digest
    assert "Architecture digest: at_cap.py" not in text
    assert "[SMART CRAWL:" in text
    assert "Architecture digest: over_cap.py" in text
    plain = crawl_pkg.crawl(tmp_path, smart=False)  # pyright: ignore[reportCallIssue]
    assert "over_cap.py" not in plain
    assert "File: at_cap.py" in plain


def test_crawlwanted_bypasses_compaction(tmp_path):
    (tmp_path / ".crawlwanted").write_text("big.py\n")
    (tmp_path / "big.py").write_text("def main():\n    return 42\n" * 50)
    text = crawl_pkg.crawl(tmp_path, smart=True, max_file_bytes=100)  # pyright: ignore[reportCallIssue]
    assert "SMART CRAWL" not in text
    assert "return 42" in text  # included in full, not compacted


def test_crawl_with_stats_smart(tmp_path):
    (tmp_path / "big.py").write_text("def main():\n    return 42\n" * 50)
    result = crawl_pkg.crawl_with_stats(tmp_path, smart=True, max_file_bytes=100)  # pyright: ignore[reportCallIssue]
    assert result.file_count == 1
    assert "Architecture digest" in result.text
    assert result.token_count > 0


def test_refresh_clears_smart_caches():
    _smart._get_tuning("python")
    _smart._lang_for_ext(".py")
    assert _smart._TUNING_CACHE
    assert _smart._EXT_MAP_CACHE
    crawl_pkg.refresh()
    assert not _smart._TUNING_CACHE
    assert not _smart._EXT_MAP_CACHE


# ---------------------------------------------------------------------------
# Large-content compaction (600 KB-class fixtures; see tmp/smart/ for the
# full offline analysis harness that motivated these tests)
# ---------------------------------------------------------------------------


def test_large_python_compaction_fast_and_exact():
    src = _big_python()  # ~400 KB, valid Python
    assert len(src) > 350_000
    t0 = time.perf_counter()
    out = _smart.compact_text(src, name="big.py", ext=".py")
    dt = time.perf_counter() - t0
    assert "Backend: ast" in out
    # regression guard: ast.get_source_segment re-split the whole source per
    # node before the precomputed-lines fix -- ~400 KB took >10 s, now <1 s
    assert dt < 8.0, f"compaction took {dt:.1f}s"
    classes = set(re.findall(r"^class (\w+)", src, re.MULTILINE))
    section = out.split("## Declarations", 1)[1].split("## ", 1)[0]
    rows = MAP_ROW_RE.findall(section)
    assert rows
    for kind, name, *_ in rows:
        if kind == "class":
            assert name in classes, name
        else:
            cls, _, meth = name.rpartition(".")
            assert meth and (cls in classes or name == "main"), name


def test_large_python_indexed_names_exist_in_source():
    src = _big_python(n_classes=400)
    out = _smart.compact_text(src, name="big.py", ext=".py", budget=3000)
    lines = src.splitlines()
    section = out.split("## Declarations", 1)[1].split("## ", 1)[0]
    rows = MAP_ROW_RE.findall(section)
    assert rows
    for _, name, s, e in rows:
        bare = name.rsplit(".", 1)[-1]
        assert bare in "\n".join(lines[int(s) - 1 : int(e)]), f"{name} not at L{s}-L{e}"


def test_truncated_map_reports_omissions_and_coverage():
    # many declarations + modest budget -> the map must end with the
    # "not indexed" terminator and a Coverage footer whose Indexed count
    # equals the number of map rows (both were silently dropped before)
    out = _smart.compact_text(_big_python(n_classes=600), name="big.py", ext=".py", budget=2500)
    assert re.search(r"^\.+ \d+ declarations not indexed due to budget$", out, re.MULTILINE)
    cov = COVERAGE_RE.search(out)
    assert cov, "Coverage footer missing"
    indexed, found, detailed, _ = (int(g) for g in cov.groups())
    section = out.split("## Declarations", 1)[1].split("## ", 1)[0]
    assert indexed == len(MAP_ROW_RE.findall(section))
    assert found > indexed > 0
    assert detailed == 0  # breadth first: the map consumed the budget


def test_budget_monotonic_more_budget_indexes_more():
    src = _big_python(n_classes=500)
    counts = []
    for budget in (1500, 4000, 9000):
        out = _smart.compact_text(src, name="big.py", ext=".py", budget=budget)
        assert len(out) <= budget * 3
        counts.append(int(COVERAGE_RE.search(out).group(1)))  # pyright: ignore[reportOptionalMemberAccess]
    assert counts == sorted(counts) and counts[0] < counts[-1]


def test_large_brace_source_initializers_not_indexed():
    src = _big_go()
    assert len(src) > 150_000
    out = _smart.compact_text(src, name="big.go", ext=".go", budget=6000)
    assert "Backend: brace" in out
    section = out.split("## Declarations", 1)[1].split("## ", 1)[0]
    names = {name for _, name, *_ in MAP_ROW_RE.findall(section)}
    assert "Config" not in names  # `cfg := Config{...}` is an initializer, not a decl
    assert any(n.startswith("NewT") for n in names)
    assert any(n == "Load" for n in names)  # methods indexed via the fn-tail fallback


def test_large_yaml_indent_backend_coverage():
    src = _big_yaml()
    assert len(src) > 200_000
    out = _smart.compact_text(src, name="big.yaml", ext=".yaml", budget=6000)
    assert "Backend: indent" in out
    cov = COVERAGE_RE.search(out)
    assert cov and int(cov.group(1)) > 500
    assert "block endpoints" in out


def test_large_prose_plain_backend_outline_capped():
    src = _big_prose()
    out = _smart.compact_text(src, name="big.txt", ext=".txt", budget=6000)
    assert "Backend: plain" in out
    assert "## Outline" in out
    outline = out.split("## Outline", 1)[1].split("## ", 1)[0]
    entries = [l for l in outline.splitlines() if l.startswith("L")]
    assert 0 < len(entries) <= 46  # _unique(extra, 46) cap
    assert "L1:" in out  # head clip survives


def test_crawl_smart_over_several_oversized_files(tmp_path):
    for i in range(3):
        (tmp_path / f"big{i}.py").write_text(_big_python(n_classes=450))
    result = crawl_pkg.crawl_with_stats(tmp_path, smart=True, max_file_bytes=100_000)  # pyright: ignore[reportCallIssue]
    assert result.file_count == 3
    for i in range(3):
        assert f"Architecture digest: big{i}.py" in result.text
    assert result.text.count("[SMART CRAWL:") == 3
    assert result.token_count > 0
