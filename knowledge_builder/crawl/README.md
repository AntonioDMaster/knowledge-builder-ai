# Crawl

Convert a repository into a single string suitable for an LLM context
window: walk the tree, filter out noise, read the surviving files, and
concatenate them with per-file headers.

```
============================================================
File: src/main.py
============================================================
<file contents>
```

## Contents

1. [Crawl behavior](#crawl-behavior)
2. [Adding a new language rule](#adding-a-new-language-rule)
3. [`.crawlignore`](#crawlignore)
4. [`.crawlwanted`](#crawlwanted)
5. [Runtime rule changes](#runtime-rule-changes)

## Crawl behavior

Entry point: `crawl(root, **kwargs)` in `_crawl.py`, built on
`list_files(root, **kwargs)`.

The pipeline, in order:

1. **Tree walk.** `os.walk` from `root`, top-down. Subdirectories are
   pruned before descending (see step 2), so ignored subtrees are never
   scanned at all.
2. **Directory pruning.** A directory is pruned when any of these apply:
   - its basename is in `skip_dirs` (the union of every language rule's
     `SKIP_DIRS` plus the universal noise in
     `rules/common/skip_dirs.txt`);
   - a `.crawlignore` pattern matches it (see section 3), unless the
     last matching pattern re-includes it with `!`.
3. **`.crawlignore` (files).** A file matching a `.crawlignore` ignore
   pattern is dropped before any other file-level check (directories
   were already handled in step 2).
4. **Filename filter.** A file is kept only if its basename is in
   `keep_names` (extensionless source files such as `Dockerfile`) or its
   extension is in `keep_ext` (the union of every language rule's
   `KEEP_EXT` plus the language-agnostic categories in
   `rules/common/ext.txt`: markup, config, styles, templates, schemas).
   A `.crawlwanted` match (see section 4) bypasses this filter -- the
   file is kept regardless of extension or name.
5. **Path filters** (optional keyword arguments):
   - `include`: gitignore-style pattern list. If non-empty, only files
     matching at least one pattern are kept.
   - `exclude`: gitignore-style pattern list; matches are dropped.
     Applied after `include`, so you can whitelist a subtree and carve
     exceptions out of it.
6. **Size cap.** `max_file_bytes` (default 500,000) drops oversized
   files such as generated dumps and minified bundles. A `.crawlwanted`
   match bypasses the cap.

Surviving files are read as UTF-8 by `safe_read`, which returns `None`
on decode or permission errors so one bad file cannot abort the walk.
`crawl()` concatenates everything with `File: <relative path>` headers,
paths relative to `root`.

For size statistics, `crawl_with_stats(root, **kwargs)` returns a
`CrawlResult` with `.text` (the same string `crawl()` builds),
`.char_count`, estimated `.token_count`, `.file_count` (files kept by
the filters), and `.total_files` — a find(1)-style count of every file
on disk under the root (excluding `.git` metadata; pruned directories
like `.venv/` ARE included). The token estimate comes from a
zero-dependency heuristic (`estimate_tokens`: word + punctuation
regex, roughly 10–15% off real BPE counts for English prose; it
undercounts on non-Latin scripts and code-heavy text). Ignored files
are never read, so they contribute nothing to the token estimate. Use
it for context-window capacity checks, not billing. The result object
also unpacks as a tuple:
`text, chars, tokens, files, total = crawl_with_stats("repo/")`.

Counting semantics: `os.walk` counts every non-directory entry, so
symlinks to files are included (a symlink to a directory is treated as
a directory and not descended) — on symlink-heavy trees the count can
slightly exceed `find -type f`, which counts regular files only.

All filters are keyword arguments (`keep_ext`, `keep_names`,
`skip_dirs`, `max_file_bytes`, `include`, `exclude`) and all default
sets are frozensets, so they compose:

```python
from knowledge_builder.crawl import crawl, rules

# add to defaults
crawl("repo/", skip_dirs=rules.DEFAULT_SKIP_DIR | {"my-generated-dir"})

# restrict to one language
crawl("repo/", keep_ext=rules.python.KEEP_EXT)

# Python plus Go, with module manifests
crawl("repo/", keep_ext=rules.python.KEEP_EXT | rules.go.KEEP_EXT
      | {".mod", ".sum"})
```

Determinism: filenames within a directory are processed in sorted
order.

**Lazy loading.** Importing `knowledge_builder.crawl` is cheap: rule
data is only scanned and parsed on first access to `rules.LANGUAGES` or
the `DEFAULT_*` aggregates (module `__getattr__`, PEP 562), and
`pathspec` imports only when a crawl function actually runs. Rule data
is cached after first access; see section 5 for re-reading it at
runtime.

## Adding a new language rule

Language rules are data-driven. There is no central registry: a
language is just a directory under `rules/`, and `rules/__init__.py`
discovers every subpackage automatically via `pkgutil`.

To add a language, e.g. `foobar`:

1. Copy the template:

   ```
   cp -r templates/crawl/rules/lang_rule knowledge_builder/crawl/rules/foobar
   ```

   (path relative to the repository root)

2. Fill in the data files. Each is line-based: one entry per line,
   blank lines and `#` comments ignored, a missing file counts as an
   empty set.

   | File | Content | Loaded into |
   |---|---|---|
   | `ext.txt` | file extensions, leading dot included (`.py`) | `KEEP_EXT` |
   | `skip_dirs.txt` | directory basenames to prune (`node_modules`) | `SKIP_DIRS` |
   | `keep_names.txt` | extensionless source filenames (`Rakefile`) | `KEEP_NAMES` |

3. Optionally adjust the `<LANGUAGE_NAME>` placeholders in the
   directory's `__init__.py` docstring. The code itself stays
   untouched -- it just loads the `.txt` files via
   `rules/_loader.py`.

Nothing else needs to change: the new rule shows up in
`rules.LANGUAGES["foobar"]` and its sets are unioned into
`rules.DEFAULT_KEEP_EXT`, `rules.DEFAULT_SKIP_DIR` and
`rules.DEFAULT_KEEP_NAMES`. If the crawl module was already imported
and its caches populated, call `knowledge_builder.crawl.refresh()` to
pick the change up -- see section 5.

Conventions:

- `skip_dirs.txt` is for language-specific tool directories only
  (build output, dependency caches). Universal noise -- VCS internals,
  tests, docs, examples, i18n, generic build/cache directories,
  editor settings -- already lives in `rules/common/skip_dirs.txt`.
- `keep_names.txt` is for language-owned filenames only
  (`Rakefile` for Ruby). Stack-independent ones (`Dockerfile`,
  `Makefile`, `README`) live in `rules/common/keep_names.txt`.
- `rules/common/` follows the same data format, with the
  language-agnostic extension categories kept as comment sections
  inside its `ext.txt`.

## `.crawlignore`

A `.crawlignore` file in the crawl target directory (`root`) controls
which paths are ignored, using **gitignore syntax and semantics**:
globs, `**`, trailing `/` for directory-only patterns, `#` comments,
and last-matching-rule-wins precedence.

```
# .crawlignore example
*.min.js
build/
!node_modules
```

Capabilities:

- **Ignore paths.** A plain pattern ignores matching files and
  directories (and, by pruning during the walk, everything below an
  ignored directory). Patterns are matched against paths relative to
  `root`.
- **Re-include with `!`.** A negated pattern (`!pattern`) re-includes
  a path. Because rules are evaluated in file order with
  last-match-wins, a later `!` beats an earlier ignore pattern.
- **Override `skip_dirs`.** This is the key difference from plain
  ignoring: a `!pattern` re-includes a directory that the built-in
  `skip_dirs` rules would prune -- for the target directory and all of
  its subdirectories (`!node_modules` rescues every `node_modules` in
  the tree, since pruning propagates through the walk). Conversely, a
  plain pattern can ignore paths that no built-in rule covers.
- **Ignored files are dropped; ignored directories are never scanned.**
  Pruning happens before any file is read.

Semantics in detail:

- If no rule matches a path, built-in filtering (`skip_dirs`,
  `keep_ext`, ...) decides, exactly as without a `.crawlignore`.
- If a rule matches, `.crawlignore` decides: matched-and-not-negated
  means dropped, last matching rule being a `!` means kept.
- Directory-only patterns (`build/`) match directories; a bare name
  (`build`) matches both files and directories.
- A `!pattern` cannot rescue paths inside a directory that was itself
  pruned by an ignore rule higher up the tree -- same as git: the walk
  never descends into pruned directories.

Interaction with the `include`/`exclude` keyword arguments: `.crawlignore`
is applied first (as part of the walk); `include`/`exclude` still apply
afterwards to whatever survived. Missing `.crawlignore` file changes
nothing.

## `.crawlwanted`

A `.crawlwanted` file in the crawl target directory (`root`) lists paths
that must be kept **even when the default rules would drop them**. It
uses the same **gitignore syntax** as `.crawlignore`: globs, `**`,
trailing `/` for directory-only patterns, `#` comments, and
last-matching-rule-wins precedence.

```
# .crawlwanted example
data/*.bin
!data/generated.bin
vendor/
```

What a `.crawlwanted` match overrides (the default ignore rules):

- **`skip_dirs`**: a wanted directory is walked even if its basename is
  pruned by built-in rules (`vendor/` rescues the vendored tree, in the
  target directory and all subdirectories).
- **`keep_ext` / `keep_names`**: a wanted file is kept even if its
  extension is not a known source extension (`data.bin`).
- **`max_file_bytes`**: a wanted file bypasses the size cap.

What it does **not** override:

- **`.crawlignore` always wins.** If a path is ignored by
  `.crawlignore`, `.crawlwanted` cannot rescue it. This is the defining
  precedence: ignore decisions are final, wanted decisions can only
  override the *default* rules.
- **`include` / `exclude` keyword arguments** remain authoritative:
  they are explicit caller choices, not defaults.

Semantics:

- Plain pattern = "wanted": the path (and, for a directory pattern,
  everything below it -- gitignore descendant semantics) is kept.
- `!pattern` = "not wanted": the path falls back to the default rules;
  negation in `.crawlwanted` never ignores anything.
- A missing `.crawlwanted` file changes nothing.
- Typical use: keep generated artifacts, binary fixtures, or vendored
code that the default noise filters exist to exclude, without touching
the caller-side keyword arguments.

Precedence summary for a single path, from strongest to weakest:

1. `.crawlignore` ignore decision (final)
2. `.crawlwanted` wanted decision
3. `include` / `exclude` keyword arguments
4. default rules: `skip_dirs`, `keep_ext` / `keep_names`,
   `max_file_bytes`

## Runtime rule changes

Rule data is cached after first access: `rules.LANGUAGES`, the
`DEFAULT_*` aggregates, and the loaded language subpackages all hold
their parsed sets. Adding, removing, or editing directories under
`rules/` while the process is running therefore needs an explicit
refresh:

```python
import knowledge_builder.crawl as kc

# ... create/edit/remove rules/<language>/ directories ...
kc.refresh()

# next lookups rebuild from the current .txt files:
"newlang" in kc.rules.LANGUAGES
".newext" in kc.DEFAULT_KEEP_EXT
kc.crawl("repo/")  # default filters pick up the change
```

`refresh()` clears every cache layer (package re-exports, the `crawl`
module's lazy `DEFAULT_*` attributes, and the `rules` package's
caches), purges the loaded rule subpackages from `sys.modules`, and
lets the next access re-read the `.txt` files. References already held
in local variables elsewhere stay stale; only fresh lookups see the
refreshed data.

Related files: `_crawl.py` (implementation), `rules/` (filter rule
data, `rules/_loader.py` is the `.txt` loader),
`templates/crawl/rules/lang_rule/` (new-language template).
