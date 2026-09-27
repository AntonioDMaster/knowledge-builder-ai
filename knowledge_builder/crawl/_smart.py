"""Common-core source compaction: turn an oversized source file into a
small navigational index ("architecture digest") for LLM contexts.

Used by default by `crawl()` for files above `max_file_bytes`, and
directly: `smart_crawl(path, budget=...)`.

The core is STRUCTURAL -- braces, indentation, quote/comment
delimiters, line shapes (`name(`, `name =`) -- no language keyword is
hardcoded, so any file gets an honest digest. SEMANTIC tuning is
optional data: `rules/common/smart_*.txt` defaults plus per-language
`rules/<lang>/smart_*.txt` overrides (union; the language's
`smart_backend.txt` wins). Missing files are empty sets; invalid regex
lines are skipped with a warning in the digest header.

Backends: `ast` (exact, tuning-activated, Python), `brace` (C-family),
`indent` (colon/indent blocks), `plain` (outline); `auto` picks
structurally. The output is an index, NOT source code; a budget too
small for the metadata raises ValueError.
"""

import bisect
import hashlib
import os
import re

__all__ = ["DEFAULT_SMART_BUDGET", "compact_text", "smart_crawl"]

DEFAULT_SMART_BUDGET = 6000  # approximate output tokens; char cap = budget * 3

_MIN_BUDGET = 300
_RULES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules")
_KNOWN_BACKENDS = ("auto", "brace", "indent", "plain", "ast")
_TYPE_KINDS = ("class", "interface", "struct", "trait", "impl", "enum")

_SHAPE_RE = re.compile(r"[\w:~]+\s*\(")
_FN_TAIL_RE = re.compile(
    r"([\w:~]+)\s*\([^()]*\)\s*"
    r"(?:\([^()]*\)|const\b|noexcept\b|->\s*[\w:*&<>]+\b|:\s*[^{}]*"
    r"|[A-Za-z_][\w\[\]*.\s]*)?\s*$",
    re.DOTALL,
)
_CALL_RE = re.compile(r"(?<![\w.])([\w.]+)\s*\(")
_WRITE_RE = re.compile(r"(?<![\w.])([\w.]+)\s*=(?![=>])")
_ASSIGN_RE = re.compile(r"^\s*(?:[A-Za-z_]\w*[\s*&*]+)?[A-Za-z_][\w.]*\s*(?::\s*[^=]+)?=(?![=>])")
_INDENT_NAME_RE = re.compile(r"([\w.]+)\s*[(:]")

_BACKEND_WARNING = {
    "ast": "exact syntax tree; signatures are structural, call sites unresolved",
    "brace": "LEXICAL HEURISTIC (brace scan, no grammar); declarations can be "
    "missed or misidentified -- verify against original lines",
    "indent": "INDENTATION HEURISTIC (colon/indent blocks, no grammar); "
    "verify against original lines",
    "plain": "no block structure detected; outline only",
}


def clip(s, n=160):
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def _unique(items, limit=12):
    return list(dict.fromkeys(items))[:limit]


def _indent_of(line):
    return len(line) - len(line.lstrip())


# ---------------------------------------------------------------------------
# Tuning data (rules/common/smart_*.txt + rules/<lang>/smart_*.txt)
# ---------------------------------------------------------------------------


class _Tuning:
    __slots__ = ("backend", "control", "decls", "entry", "imports", "warnings")

    def __init__(self):
        self.backend = "auto"
        self.decls = []
        self.control = []
        self.imports = []  # compiled regexes
        self.entry = []  # compiled regexes (IGNORECASE)
        self.warnings = []


def _data_lines(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return [l.strip() for l in fh if l.strip() and not l.lstrip().startswith("#")]
    except OSError:
        return []


def _compile_patterns(lines, where, tuning, flags=0):
    out = []
    for line in lines:
        try:
            out.append(re.compile(line, flags))
        except re.error as exc:
            tuning.warnings.append(f"tuning: skipped invalid pattern in {where}: {line!r} ({exc})")
    return out


_TUNING_CACHE = {}
_EXT_MAP_CACHE = {}


def _get_tuning(lang, rules_root=None):
    root = rules_root or _RULES_DIR
    key = (root, lang)
    if key in _TUNING_CACHE:
        return _TUNING_CACHE[key]
    t = _Tuning()
    dirs = [os.path.join(root, "common")]
    if lang:
        dirs.append(os.path.join(root, lang))
    for d in dirs:
        where = os.path.basename(d)
        backends = _data_lines(os.path.join(d, "smart_backend.txt"))
        if backends:
            val = backends[0].lower()
            if val in _KNOWN_BACKENDS:
                t.backend = val  # language directory overrides common
            else:
                t.warnings.append(f"tuning: ignored unknown backend {val!r} in {where}")
        t.decls.extend(_data_lines(os.path.join(d, "smart_decls.txt")))
        t.control.extend(_data_lines(os.path.join(d, "smart_control.txt")))
        t.imports.extend(
            _compile_patterns(
                _data_lines(os.path.join(d, "smart_imports.txt")),
                f"{where}/smart_imports.txt",
                t,
            )
        )
        t.entry.extend(
            _compile_patterns(
                _data_lines(os.path.join(d, "smart_entry.txt")),
                f"{where}/smart_entry.txt",
                t,
                re.IGNORECASE,
            )
        )
    t.decls = list(dict.fromkeys(t.decls))
    t.control = list(dict.fromkeys(t.control))
    _TUNING_CACHE[key] = t
    return t


def _lang_for_ext(ext, rules_root=None):
    """Map an extension to a rules/ language via ext.txt files.

    Filesystem-only (never imports the rules package, so it cannot
    disturb the lazy rule caches); first language in sorted order wins.
    """
    root = rules_root or _RULES_DIR
    if root not in _EXT_MAP_CACHE:
        mapping = {}
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            entries = []
        for entry in entries:
            d = os.path.join(root, entry)
            if entry in ("common", "__pycache__") or not os.path.isdir(d):
                continue
            if not os.path.isfile(os.path.join(d, "__init__.py")):
                continue
            for e in _data_lines(os.path.join(d, "ext.txt")):
                mapping.setdefault(e, entry)
        _EXT_MAP_CACHE[root] = mapping
    return _EXT_MAP_CACHE[root].get(ext)


def _clear_caches():
    """Forget cached tuning/ext data; wired into crawl.refresh()."""
    _TUNING_CACHE.clear()
    _EXT_MAP_CACHE.clear()


# ---------------------------------------------------------------------------
# Structural masking
# ---------------------------------------------------------------------------


def _mask(src):
    """Blank out comments and string contents, preserving every character
    position (newlines included) so masked and original text stay aligned.

    Lexical defaults: // and /* */ comments, # line comments (masked
    unless attached to an identifier, keeping JS `obj.#field` intact),
    and ' " ` strings including r"..." raw prefixes.
    """
    out = list(src)
    n = len(src)
    i = 0
    state = ""
    end = ""
    while i < n:
        c = src[i]
        d = src[i : i + 2]
        if not state:
            if d == "//":
                state = "line"
                out[i : i + 2] = "  "
                i += 2
                continue
            if d == "/*":
                state = "block"
                out[i : i + 2] = "  "
                i += 2
                continue
            if c == "#" and (i == 0 or not (src[i - 1].isalnum() or src[i - 1] in "._$")):
                state = "line"
                out[i] = " "
                i += 1
                continue
            if c == "r" and d == 'r"':
                state = "raw"
                out[i : i + 2] = "  "
                i += 2
                continue
            if c in "\"'`":
                state = "string"
                end = c
                out[i] = " "
                i += 1
                continue
        elif state == "line":
            if c == "\n":
                state = ""
            else:
                out[i] = " "
            i += 1
            continue
        elif state == "block":
            if d == "*/":
                out[i : i + 2] = "  "
                i += 2
                state = ""
                continue
            if c != "\n":
                out[i] = " "
            i += 1
            continue
        else:  # string or raw
            if c == "\\" and state == "string":
                span = src[i : i + 2]
                out[i : i + len(span)] = " " * len(span)
                i += 2
                continue
            if c == end or (state == "raw" and c == '"'):
                state = ""
            if c != "\n":
                out[i] = " "
            i += 1
            continue
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Shared extraction helpers
# ---------------------------------------------------------------------------


def _decl_regex(decls):
    if not decls:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in decls) + r")\s+([\w:]+)")


def _control_start_regex(control):
    if not control:
        return None
    return re.compile("^(?:" + "|".join(re.escape(w) for w in control) + r")\b")


def _note_regex(control):
    if not control:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in control) + r")\b[^;\n]{0,100}")


def _new_item(name, kind, start, sig, depth):
    return {
        "name": name,
        "kind": kind,
        "start": start,
        "end": 0,
        "sig": clip(sig, 420),
        "calls": [],
        "writes": [],
        "deco": [],
        "doc": "",
        "notes": [],
        "annotations": [],
        "depth": depth,
    }


def _scan_imports(lines, tuning):
    out, seen = [], set()
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if not s or i in seen:
            continue
        if any(rx.search(s) for rx in tuning.imports):
            seen.add(i)
            out.append(f"L{i}: {clip(s, 220)}")
    return out[:80]


def _scan_constants(lines, covered, top_level_only=True):
    out = []
    for i, line in enumerate(lines, 1):
        if i in covered:
            continue
        if top_level_only and line[:1].isspace():
            continue
        if _ASSIGN_RE.match(line):
            out.append(f"L{i}: {clip(line.strip(), 170)}")
        if len(out) >= 35:
            break
    return out


def _drop_child_calls(items):
    """Remove nested declaration names from their parents' call lists."""
    kids = {}
    for it in items:
        parent = it.pop("_parent", None)
        if parent is not None:
            kids.setdefault(id(parent), []).append(it["name"].rsplit(".", 1)[-1])
    for it in items:
        child_names = set(kids.get(id(it), ()))
        if child_names:
            it["calls"] = [
                c for c in it["calls"] if c.rsplit(".", 1)[-1] not in child_names
            ]


def _covered_lines(items):
    covered = set()
    for it in items:
        covered.update(range(it["start"], it["end"] + 1))
    return covered


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


def _brace_backend(src, masked, tuning):
    """C-family lexical scan: brace stack + header plausibility."""
    lines = src.splitlines()
    starts = [0]
    for m in re.finditer("\n", src):
        starts.append(m.end())

    def line(pos):
        return bisect.bisect_right(starts, pos)

    decl_re = _decl_regex(tuning.decls)
    control_start = _control_start_regex(tuning.control)
    control_set = set(tuning.control)
    note_re = _note_regex(tuning.control)
    items = []
    stack = []
    boundary = 0
    for i, ch in enumerate(masked):
        if ch == "{":
            # floor prev at boundary - 1: rfind returns -1 on no-match,
            # which would span the header across the whole file prefix
            prev = max(
                boundary - 1,
                masked.rfind(";", boundary, i),
                masked.rfind("}", boundary, i),
                masked.rfind("{", boundary, i),
            )
            head = masked[prev + 1 : i].strip()
            original = src[prev + 1 : i].strip()
            item = None
            plausible = 0 < len(head) < 700 and "=" not in head
            if plausible and control_start and control_start.match(head):
                plausible = False
            if plausible and not ((decl_re and decl_re.search(head)) or _SHAPE_RE.search(head)):
                plausible = False
            if plausible:
                kind = name = None
                moff = 0
                m = decl_re.search(head) if decl_re else None
                if m:
                    kind, name = m.group(1), m.group(2)
                    moff = m.start()
                else:
                    fm = _FN_TAIL_RE.search(original)
                    if fm:
                        kind, name = "function", fm.group(1)
                        moff = fm.start()
                if kind:
                    # start at the declaration match (headers may span back
                    # across blank lines, e.g. semicolon-free Go); signature
                    # keeps only the header's last blank-line paragraph
                    raw = masked[prev + 1 : i]
                    lead = len(raw) - len(raw.lstrip())
                    hs = prev + 1 + lead + moff
                    sig_src = re.split(r"\n\s*\n", original)[-1].strip()
                    item = _new_item(name, kind, line(hs), sig_src, len(stack))
                    item["_parent"] = stack[-1][1] if stack else None
                    items.append(item)
            stack.append((i, item))
        elif ch == "}":
            if stack:
                opened, item = stack.pop()
                if item is not None:
                    item["end"] = line(i)
                    bodymask = masked[opened + 1 : i]
                    item["calls"] = _unique(
                        c for c in _CALL_RE.findall(bodymask) if c not in control_set
                    )
                    item["writes"] = _unique(
                        (
                            m.group(1)
                            for m in _WRITE_RE.finditer(bodymask)
                            if "." in m.group(1)
                        ),
                        8,
                    )
                    if note_re:
                        item["notes"] = _unique(
                            (
                                f"L{line(opened + 1 + m.start())}: {clip(m.group(0), 110)}"
                                for m in note_re.finditer(bodymask)
                            ),
                            5,
                        )
            if not stack:
                boundary = i + 1
        elif ch == ";" and not stack:
            boundary = i + 1
    items = [x for x in items if x["end"]]
    _drop_child_calls(items)
    return {
        "imports": _scan_imports(lines, tuning),
        "constants": _scan_constants(lines, _covered_lines(items)),
        "items": items,
        "extra": [],
    }


def _indent_backend(src, masked, tuning):
    """Colon/indent block scan (Python-like, YAML-like)."""
    masked_lines = masked.splitlines()
    orig_lines = src.splitlines()
    n = len(masked_lines)
    decl_re = _decl_regex(tuning.decls)
    control_start = _control_start_regex(tuning.control)
    control_set = set(tuning.control)
    note_re = _note_regex(tuning.control)
    items = []
    stack = []  # (indent, item, header line index)

    def close_to(ind, end_line):
        while stack and ind <= stack[-1][0]:
            _, it, hdr = stack.pop()
            if it is None:
                continue
            it["end"] = max(end_line, hdr + 2)
            body_lines = masked_lines[hdr + 1 : end_line]
            body = "\n".join(body_lines)
            it["calls"] = _unique(
                c for c in _CALL_RE.findall(body) if c not in control_set
            )
            it["writes"] = _unique(
                (m.group(1) for m in _WRITE_RE.finditer(body) if "." in m.group(1)),
                8,
            )
            if note_re:
                notes = []
                for k, bl in enumerate(body_lines):
                    for m in note_re.finditer(bl):
                        notes.append(f"L{hdr + 2 + k}: {clip(m.group(0), 110)}")
                it["notes"] = _unique(notes, 5)

    for i in range(n):
        stripped = masked_lines[i].strip()
        if not stripped:
            continue
        ind = _indent_of(masked_lines[i])
        close_to(ind, i)
        if not stripped.endswith(":"):
            continue
        j = i + 1
        while j < n and not masked_lines[j].strip():
            j += 1
        if j >= n or _indent_of(masked_lines[j]) <= ind:
            continue
        if control_start and control_start.match(stripped):
            continue
        kind = name = None
        m = decl_re.search(stripped) if decl_re else None
        if m:
            kind, name = m.group(1), m.group(2)
        else:
            sm = _INDENT_NAME_RE.match(stripped)
            if sm:
                kind, name = "block", sm.group(1)
        if not kind:
            continue
        deco = []
        k = i - 1
        while k >= 0 and orig_lines[k].strip().startswith("@"):
            deco.insert(0, clip(orig_lines[k].strip(), 80))
            k -= 1
        it = _new_item(name, kind, i + 1, orig_lines[i].strip(), len(stack))
        it["deco"] = deco
        it["_parent"] = stack[-1][1] if stack else None
        items.append(it)
        stack.append((ind, it, i))
    close_to(-1, n)
    items = [x for x in items if x["end"]]
    _drop_child_calls(items)
    return {
        "imports": _scan_imports(orig_lines, tuning),
        "constants": _scan_constants(orig_lines, _covered_lines(items)),
        "items": items,
        "extra": [],
    }


def _plain_backend(src, masked, tuning):
    """No block structure: declaration-keyword outline plus head/tail clips."""
    lines = src.splitlines()
    decl_re = _decl_regex(tuning.decls)
    extra = []
    if decl_re:
        for i, s in enumerate(lines, 1):
            t = s.strip()
            if t and decl_re.search(t):
                extra.append(f"L{i}: {clip(t, 110)}")
            if len(extra) >= 40:
                break
    content = [(i, s.strip()) for i, s in enumerate(lines, 1) if s.strip()]
    for i, s in content[:3]:
        extra.append(f"L{i}: {clip(s, 110)}")
    for i, s in content[-3:]:
        extra.append(f"L{i}: {clip(s, 110)}")
    return {
        "imports": _scan_imports(lines, tuning),
        "constants": _scan_constants(lines, set(), top_level_only=False),
        "items": [],
        "extra": _unique(extra, 46),
    }


def _ast_backend(src, masked, tuning):
    """Exact Python syntax tree (tuning-activated; stdlib ast)."""
    import ast

    tree = ast.parse(src)
    # ast linenos break on \r\n and \r but not \f; str.splitlines()
    # splits on \f too and would misalign lineno-based slicing
    lines = re.split(r"\r\n|\r|\n", src)

    def segment(node):
        """ast.get_source_segment() over precomputed lines; the stdlib
        helper re-splits the whole source per call (O(n^2) on large
        modules)."""
        if (
            getattr(node, "lineno", None) is None
            or getattr(node, "col_offset", None) is None
            or getattr(node, "end_lineno", None) is None
            or getattr(node, "end_col_offset", None) is None
        ):
            return ""
        lo, hi = node.lineno - 1, node.end_lineno
        if not 0 <= lo < hi <= len(lines):
            return ""
        seg = lines[lo:hi]
        seg[-1] = seg[-1][: node.end_col_offset]
        seg[0] = seg[0][node.col_offset :]
        return "\n".join(seg)

    items, imports, constants = [], [], []
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            imports.append(clip(segment(n), 240))
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            names = [
                x.id
                for x in ast.walk(n)
                if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)
            ]
            if names:
                constants.append(f"L{n.lineno}: {clip(segment(n), 170)}")

    def walk(body, parent=""):
        for n in body:
            iscls = isinstance(n, ast.ClassDef)
            isfn = isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            if not (iscls or isfn):
                continue
            name = f"{parent}.{n.name}" if parent else n.name
            first = min([n.lineno] + [d.lineno for d in n.decorator_list])
            start = n.lineno - 1
            bodyline = n.body[0].lineno - 1 if n.body else start
            header = "\n".join(lines[start : bodyline + 1])
            own_nodes = []
            for child in n.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                own_nodes.extend(ast.walk(child))
            calls, refs = [], []
            for x in own_nodes:
                if isinstance(x, ast.Call):
                    try:
                        calls.append(ast.unparse(x.func))
                    except (ValueError, TypeError):
                        pass
                if isinstance(x, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    target = x.targets if isinstance(x, ast.Assign) else [x.target]
                    for t in target:
                        val = segment(t)
                        if "." in val or iscls:
                            refs.append(clip(val, 60))
            kind = "class" if iscls else ("async def" if isinstance(n, ast.AsyncFunctionDef) else "def")
            annotations = []
            if isfn:
                for arg in (*n.args.posonlyargs, *n.args.args, *n.args.kwonlyargs):
                    if arg.annotation:
                        annotations.append(
                            arg.arg + ":" + clip(segment(arg.annotation), 80)
                        )
            detail = []
            for stmt in n.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                if isinstance(
                    stmt,
                    (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With,
                     ast.AsyncWith, ast.Match, ast.Return, ast.Raise),
                ):
                    detail.append(f"L{stmt.lineno} {clip(lines[stmt.lineno - 1].strip(), 125)}")
            items.append(
                {
                    "name": name,
                    "kind": kind,
                    "start": first,
                    "end": n.end_lineno,
                    "sig": clip(header, 420),
                    "calls": _unique(calls),
                    "writes": _unique(refs, 8),
                    "deco": [clip(segment(d), 80) for d in n.decorator_list],
                    "doc": clip(ast.get_docstring(n) or "", 190),
                    "notes": _unique(detail, 5),
                    "annotations": annotations,
                    "depth": name.count("."),
                }
            )
            if iscls:
                walk(n.body, name)

    walk(tree.body)
    return {"imports": imports, "constants": constants[:35], "items": items, "extra": []}


def _detect_backend(src, masked):
    """Structural backend choice: trailing braces vs colon+indent blocks."""
    masked_lines = masked.splitlines()
    brace = sum(1 for l in masked_lines if l.rstrip().endswith("{"))
    colon = 0
    prev = None
    for l in masked_lines:
        s = l.strip()
        if not s:
            continue
        ind = _indent_of(l)
        if prev is not None and prev[0].endswith(":") and ind > prev[1]:
            colon += 1
        prev = (s, ind)
    if brace >= 2 and brace >= colon:
        return "brace"
    if colon >= 2:
        return "indent"
    if brace:
        return "brace"
    if colon:
        return "indent"
    return "plain"


def _run_structural(backend_name, src, masked, tuning):
    if backend_name == "brace":
        return _brace_backend(src, masked, tuning)
    if backend_name == "indent":
        return _indent_backend(src, masked, tuning)
    return _plain_backend(src, masked, tuning)


# ---------------------------------------------------------------------------
# Scoring and report assembly
# ---------------------------------------------------------------------------


def _score(item, entry_res):
    name = item["name"].rsplit(".", 1)[-1]
    v = 10 + (30 if item["kind"] in _TYPE_KINDS else 0)
    if any(r.search(name) for r in entry_res):
        v += 35
    if name.startswith("_") and not name.startswith("__"):
        v -= 9
    if item["deco"] and any("route" in d or "command" in d for d in item["deco"]):
        v += 25
    v += min(len(item["calls"]) * 2, 18) + min(len(item["writes"]) * 3, 18)
    v += min(max(item["end"] - item["start"], 0) // 15, 16)
    return v


def _describe(item):
    out = [
        f"{item['kind']} {item['name']} [L{item['start']}-{item['end']}]",
        f"  signature: {item['sig']}",
    ]
    if item["deco"]:
        out.append("  decorators: " + ", ".join(item["deco"]))
    if item["doc"]:
        out.append("  doc: " + item["doc"])
    if item["annotations"]:
        out.append("  types: " + ", ".join(item["annotations"][:10]))
    if item["calls"]:
        out.append("  calls: " + ", ".join(item["calls"]))
    if item["writes"]:
        out.append("  writes: " + ", ".join(item["writes"]))
    if item["notes"]:
        out.append("  control/returns: " + " | ".join(item["notes"]))
    return "\n".join(out) + "\n"


def _assemble(name, raw, lang, backend_name, warning, tuning, data, budget):
    cap = budget * 3
    # reserve room for the truncation note + coverage footer so a
    # budget-pressed digest always says what was omitted
    soft = cap - 150
    head_lines = [
        f"# Architecture digest: {name}",
        f"Language: {lang}; size: {len(raw)} bytes; SHA256: {hashlib.sha256(raw).hexdigest()}",
        f"Backend: {backend_name} -- {warning}",
        *tuning.warnings,
        (
            "This is an index, NOT source code. Do not infer omitted semantics; "
            "reread original line ranges before edits."
        ),
    ]
    head = "\n".join(head_lines) + "\n"
    if len(head) > cap:
        raise ValueError("budget too small for metadata")
    parts = [head]
    used = len(head)

    def add(s, hard=False):
        nonlocal used
        limit = cap if hard else soft
        if used + len(s) > limit:
            return False
        parts.append(s)
        used += len(s)
        return True

    add("\n## Imports / dependencies (first 80)\n")
    for imp in data["imports"]:
        if not add(imp + "\n"):
            break
    add("\n## Module assignments / configuration (first 35)\n")
    for c in data["constants"]:
        if not add(c + "\n"):
            break
    if data["extra"]:
        add("\n## Outline (no block declarations detected)\n")
        for e in data["extra"]:
            if not add(e + "\n"):
                break
    items = data["items"]
    add(f"\n## Declarations ({len(items)} found; lines are in original source)\n")
    selected = []
    for x in items:
        if add(f"{x['kind']} {x['name']} [L{x['start']}-{x['end']}]\n"):
            selected.append(x)
        else:
            break
    add(f"... {len(items) - len(selected)} declarations not indexed due to budget\n", hard=True)
    add("\n## Implementation signals (highest-priority declarations first)\n")
    details = 0
    for x in sorted(selected, key=lambda a: (-_score(a, tuning.entry), a["start"])):
        if add(_describe(x) + "\n"):
            details += 1
    text = "".join(parts)
    add(
        f"\n## Coverage\nIndexed {len(selected)}/{len(items)} declarations; "
        f"detailed {details}/{len(selected)}. approx tokens: {(len(text) + 2) // 3}\n",
        hard=True,
    )
    return "".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _compact(raw, name, ext, lang=None, backend=None, budget=DEFAULT_SMART_BUDGET, rules_root=None):
    if budget < _MIN_BUDGET:
        raise ValueError(f"budget must be >= {_MIN_BUDGET}")
    src = raw.decode("utf-8-sig", errors="replace")
    if lang is None:
        lang = _lang_for_ext(ext, rules_root)
    display_lang = lang or "unknown"
    tuning = _get_tuning(lang, rules_root)
    masked = _mask(src)
    backend_name = backend or tuning.backend
    if backend_name == "auto":
        backend_name = _detect_backend(src, masked)
    if backend_name == "ast":
        try:
            data = _ast_backend(src, masked, tuning)
        except SyntaxError:
            tuning.warnings.append("tuning: ast parse failed; fell back to structural backend")
            backend_name = _detect_backend(src, masked)
            data = _run_structural(backend_name, src, masked, tuning)
    else:
        data = _run_structural(backend_name, src, masked, tuning)
    return _assemble(
        name, raw, display_lang, backend_name, _BACKEND_WARNING[backend_name], tuning, data, budget
    )


def smart_crawl(path, *, budget=DEFAULT_SMART_BUDGET, backend=None):
    """Compact one source file on disk into a navigational digest string.

    budget: approximate output tokens (char cap = budget * 3);
    backend: force brace/indent/plain/ast, overriding tuning data.
    """
    path = os.fspath(path)
    with open(path, "rb") as fh:
        raw = fh.read()
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    return _compact(raw, name, ext, backend=backend, budget=budget)


def compact_text(
    text,
    *,
    name="<text>",
    ext="",
    lang=None,
    backend=None,
    budget=DEFAULT_SMART_BUDGET,
    rules_root=None,
):
    """Compact source text directly; `rules_root` overrides the tuning
    directory (tests, experiments)."""
    return _compact(
        text.encode("utf-8", "replace"),
        name,
        ext,
        lang=lang,
        backend=backend,
        budget=budget,
        rules_root=rules_root,
    )
