# Smart compaction rules reference

How the optional per-language tuning files (`smart_*.txt`) work, what
every line in them does, and how to debug a digest that looks wrong.
This is the detailed companion to section 5 of [README.md](README.md)
("Smart compaction") — read that first for the big picture.

## The one-minute version

- Tuning is **data, never code**: five optional `.txt` files per
  language, next to its `ext.txt`.
- Defaults live in `rules/common/`; a language overrides by dropping
  same-named files into `rules/<lang>/`. Words are **unioned** with the
  common defaults; the backend value from the language directory
  **wins**.
- Every file is line-based: one entry per line, blank lines and `#`
  comments ignored, a missing file counts as empty. Same format as
  `ext.txt` / `skip_dirs.txt` / `keep_names.txt`.
- Nothing here can crash a crawl: a bad line can only degrade a digest,
  and says so in the digest header. Run `refresh()` after editing.

The five files:

| File | Controls | Entry type |
|---|---|---|
| `smart_backend.txt` | which backend runs | one value |
| `smart_decls.txt` | what counts as a declaration | literal keyword |
| `smart_control.txt` | what is filtered out / annotated | literal keyword |
| `smart_imports.txt` | what shows up as an import | regex |
| `smart_entry.txt` | which names get priority | regex |

## How a digest is produced (and where tuning fits in)

1. **Mask.** Comments and string literal contents are blanked out with
   every character position preserved (`//`, `/* */`, `#` line comments
   unless attached to an identifier, `'` `"` `` ` `` strings, `r"…"`
   raw strings). All structural scanning happens on this masked copy;
   displayed text comes from the original.
2. **Pick a backend** — explicitly from tuning, else structurally:
   - lines ending in `{` → `brace`
   - colon-ended lines followed by deeper indentation → `indent`
   - neither → `plain`
3. **Index blocks.** The backend emits items: `name`, `kind`, start/end
   lines, signature, calls, writes, notes. Tuning decides *which*
   blocks qualify as declarations and how they are named.
4. **Assemble** under the token budget: imports → constants → complete
   one-line declaration map → detail blocks for top-scoring items →
   coverage footer. `smart_entry.txt` patterns add +35 to the score.

Tuning only ever *adds information or filters noise*. With all five
files empty, the backends still run — the digest is just noisier
(e.g. `if (x) {` blocks would be indexed, since filtering them needs
control words).

## `smart_backend.txt` — pin the backend

One value on the first line (`auto brace indent plain ast`,
case-insensitive). Missing or `auto` = structural detection. The
language directory's value wins over `common/`'s; an unknown value is
ignored with a warning in the digest header.

| Value | Use for | Behavior |
|---|---|---|
| `auto` (default) | | structural detection as above |
| `brace` | C, C++, Java, Go, Rust, JS, TS, C#, ... | brace-stack scan of the masked text |
| `indent` | Python-like and YAML-like files | colon-ended header + deeper-indent body |
| `plain` | data formats, markup | outline only, says so |
| `ast` | Python only | exact stdlib syntax tree; on `SyntaxError` falls back to structural detection (warning in header) |

The shipped data pins `ast` for Python (`rules/python/smart_backend.txt`).
Everything else uses `auto`.

## `smart_decls.txt` — what is a declaration

Each line is a **literal keyword** (not a regex; it is regex-escaped
before use). A block header containing one becomes a declaration:

- `kind` = the keyword itself (`class Foo {` → kind `class`),
- `name` = the next word after it: `\b(keyword)\s+([\w:]+)`.

So the entry `function` turns `function handleClick(event) {` into
`function handleClick`. Without any matching keyword, a block whose
header ends in an identifier before `(` still counts as a `function`
(shape-based fallback: `int add(int a, int b)`), and a colon-ended
header with a leading identifier becomes a `block`.

Current common defaults (keep lists conservative — a word here can
only add index entries, never remove):

```
class interface struct enum trait impl namespace module
function func fn def type protocol extension record object
```

Guidelines:

- Add words that *introduce* declarations in your language
  (`property`, `event`, `actor`, ...).
- Do **not** add words that can start a statement that merely *opens a
  block* you don't want indexed — that's `smart_control.txt`'s job.
- Prefer per-language files over growing the common list: `object`
  means a declaration in Scala, but the word can appear in headers of
  unrelated languages.

## `smart_control.txt` — what is noise

Each line is a **literal keyword**, used in three places:

1. **Block exclusion.** A block header *starting* with one of these
   words is not indexed (`^(?:if|for|...)\b`): `if (x) {`, `for ... {`,
   `try {`, `match x {` — their bodies still belong to the enclosing
   declaration.
2. **Call filtering.** Names captured as calls that match a control
   word exactly are dropped from `calls:` lists (so `if (...)` never
   appears as a call).
3. **Notes.** Lines in a body containing a control word feed the
   `control/returns:` notes (`L12: if err != nil {`), giving the digest
   its "what does this function actually do" flavor.

Current common defaults:

```
if else elif elseif for foreach while switch case do try except
rescue ensure catch finally return throw throws match select defer
go await yield break continue with using lock synchronized loop
unless until repeat guard when new
```

Guidelines:

- Add block-starting words that pollute the map in your language.
- Do **not** add words that prefix real declarations. The common list
  deliberately omits `async` (`async def` is a declaration) and
  `unsafe` (`unsafe impl` in Rust). If you add such a word per-language
  you will hide those declarations.
- Known trade-off: Scala `case class` is lost because `case` must stay
  a control word (match/case blocks dominate across languages).

## `smart_imports.txt` — what shows up as an import

Each line is a **regex** (one per line, no delimiters), matched with
`re.search` against every stripped source line. Matching lines are
listed under `## Imports / dependencies` (first 80, deduplicated by
line, `L<line>: <clipped line>` format). Used by the structural
backends only — the `ast` backend takes imports from the syntax tree
directly.

Current common defaults:

```
^\s*import\b
^\s*from\s+[\w.]+\s+import\b
^\s*#include\b
^\s*use\s+
^\s*package\s+[\w.]
^\s*mod\s+[\w]
^\s*export\b.*\bfrom\b
^\s*require\b
^\s*library\b
^\s*extern\s+crate\b
```

Guidelines:

- Anchor with `^\s*` -- mid-line matches produce noise.
- Union semantics only: a language file *adds* patterns but cannot
  remove or override a common one. If a common pattern misfires for
  your language, fix it in `rules/common/` (or accept the noise).
- **Invalid regexes never crash a crawl**: the line is skipped and a
  `tuning: skipped invalid pattern in ...` warning appears in the
  digest header.

## `smart_entry.txt` — which names get priority

Each line is a **regex** (case-insensitive), matched against the bare
declaration name — the last component after dots (`CrawlResult.__init__`
→ `__init__`). Matches add **+35** to the score that decides which
declarations get expanded into detail blocks when the budget runs out
(see scoring below).

Current common default (one line):

```
^(main|cli|run|start|serve|handle|dispatch|route|register|init|setup|load|save|parse|build|create|validate|authenticate|connect|execute|process|__init__|index|handler|worker|app)$
```

Guidelines:

- Anchor it (`^...$`) -- matching is `re.search`, so an unanchored
  pattern matches substrings and boosts names you did not intend.
- Add framework entry-point names per language (`@main`-style
  functions, test hooks, HTTP handlers).

## Scoring — how detail priority is computed

When the budget cannot fit every detail block, items are expanded in
descending score:

| Signal | Points |
|---|---|
| every declaration | +10 |
| kind is a type-like word (`class`, `interface`, `struct`, `trait`, `impl`, `enum`) | +30 |
| name matches a `smart_entry.txt` pattern | +35 |
| name starts with a single `_` (private convention) | −9 |
| decorator containing `route` or `command` | +25 |
| each call | +2 (cap +18) |
| each attribute write | +3 (cap +18) |
| size | +1 per 15 lines (cap +16) |

Only the six kind words listed get the type bonus; kinds coming from
your own `smart_decls.txt` entries score +10 base unless they happen to
be one of those six.

## Precedence and caching

- `rules/common/smart_*.txt` are always read; `rules/<lang>/smart_*.txt`
  are read on top. Keyword lists are unioned (order-preserving,
  deduplicated); the backend value from the language directory wins.
- Language resolution: a file's extension is mapped to a language by
  scanning every `rules/*/ext.txt` (first language in sorted order wins
  a shared extension). Extensions only listed in `rules/common/ext.txt`
  get common tuning only. Unknown extensions get common tuning too —
  the digest header shows `Language: unknown`.
- Results are cached per (rules directory, language). After editing
  any `smart_*.txt` at runtime, call
  `knowledge_builder.crawl.refresh()` — it clears the smart caches
  together with the filter-rule caches.
- The five files are invisible to the classic filter loader
  (`rules/_loader.py` reads exactly `ext.txt`, `skip_dirs.txt`,
  `keep_names.txt`), so adding them cannot change which files a crawl
  keeps.

## Debugging a digest

1. **Read the header.** It names the backend used and every tuning
   warning (unknown backend value, skipped regex, ast fallback).
2. **Which tuning is active?** Reproduce with `compact_text` and a
   scratch rules directory:

   ```python
   from knowledge_builder.crawl import compact_text
   print(compact_text(src, ext=".mylang", rules_root="scratch_rules/"))
   ```

   `scratch_rules/common/` can start as a copy of the shipped defaults;
   edit from there and compare digests.
3. **Declaration missing?** Check, in order: does a `smart_decls.txt`
   keyword or the `identifier(` shape match the header; does the header
   start with a control word; does it contain `=` (initializers are
   rejected — a `struct Point p = {1, 2};` is a variable, not a
   declaration); is the header longer than 700 chars; for `brace`, do
   the braces balance?
4. **Too much noise?** Add the offending block-starting word to
   `smart_control.txt` in the language directory.
5. **Imports polluted?** Your language's own patterns union over the
   common ones; a too-loose common pattern can only be fixed in
   `common/`.

## Limitations by design

- Lexical backends have no grammar: C/C++ macros, Rust macros, and
  TypeScript-only syntax can confuse them (the digest header warns).
- Headers containing `=` are treated as initializers, so C++ default
  arguments (`int f(int x = 3)`) are not indexed.
- `#` line-comment masking skips `#` attached to an identifier
  (`obj.#field`), but a bare `#` mid-line blanks the rest of the line
  in the masked copy.
- The digest is an index, not source: reread original line ranges
  before editing anything you found through it.

Related: [README.md](README.md) (crawl behavior, smart compaction
overview), `rules/common/smart_*.txt` (shipped defaults),
`templates/crawl/rules/lang_rule/smart_*.txt` (per-language template),
`_smart.py` (implementation).
