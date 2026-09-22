"""Language registry — which files we parse, and what a symbol looks like in each.

Kept as data rather than per-language classes: every language we support is a
node-type lookup away from the same generic walker in `parse.py`. Adding a
language means adding a row here, not a new code path.
"""
from __future__ import annotations

# Extension -> tree-sitter-language-pack grammar name.
EXT_LANG: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "tsx",  # the tsx grammar parses JSX; the javascript one does not
}

# Directories we never descend into. Indexing node_modules is how an indexer
# turns a 4k-file repo into a 400k-file one and stops being useful.
SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".env", "dist", "build", ".next", ".nuxt", "out", "target",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "coverage", ".cache",
    "vendor", "third_party", ".terraform", "site-packages", ".tox",
})

# Substrings that mark a directory as a build artefact or a snapshot copy even
# when it isn't named exactly "dist". Real trees grow directories like
# `panel-backend-dist.bak.perpage-pre` and `knowledge-dist`, whose bundled
# sources otherwise dominate the symbol table and, worse, hijack name
# resolution — a unique-looking name in a bundle steals edges from the real
# definition.
SKIP_DIR_MARKERS: tuple[str, ...] = (
    "-dist", "dist.", ".bak", "-backup", ".old", "-old.", ".orig",
)

# A generated bundle is technically code, but indexing it buries the hand-written
# symbols it was generated from under thousands of minified names.
SKIP_FILE_SUFFIXES: tuple[str, ...] = (
    ".min.js", ".min.ts", ".bundle.js", ".d.ts", ".pb.go", "_pb2.py",
    ".generated.ts", ".gen.go",
)

MAX_FILE_BYTES = 1_500_000   # beyond this it is data or a bundle, not source

# Shape-based generated-code detection, for bundles that no name pattern
# catches. Hand-written source is broken into short lines; a bundler's output
# is not.
MAX_MEAN_LINE = 200
MAX_SINGLE_LINE = 2000

# ─── Per-grammar node types ──────────────────────────────────────────────────
# `defs` maps a tree-sitter node type to the symbol kind we record.

PYTHON = {
    "defs": {
        "function_definition": "function",
        "class_definition": "class",
    },
    "imports": ("import_statement", "import_from_statement"),
    "calls": ("call",),
    "string_types": ("string",),
}

TS_LIKE = {
    "defs": {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
        "enum_declaration": "enum",
        "abstract_class_declaration": "class",
    },
    "imports": ("import_statement",),
    "calls": ("call_expression",),
    "string_types": ("string", "template_string"),
}

GRAMMAR: dict[str, dict] = {
    "python": PYTHON,
    "typescript": TS_LIKE,
    "tsx": TS_LIKE,
    "javascript": TS_LIKE,
}


def skip_dir(name: str) -> bool:
    """Whether to prune a directory during the crawl."""
    if name in SKIP_DIRS or name.startswith("."):
        return True
    low = name.lower()
    return any(m in low for m in SKIP_DIR_MARKERS)


def lang_for(path: str) -> str | None:
    """Grammar name for a path, or None if we don't index this file type."""
    low = path.lower()
    if low.endswith(SKIP_FILE_SUFFIXES):
        return None
    for ext, lang in EXT_LANG.items():
        if low.endswith(ext):
            return lang
    return None


def looks_generated(src: bytes) -> bool:
    """Bundled/minified output, judged by line shape rather than by name.

    Catches the artefacts that slip past both .gitignore and the directory
    patterns — the ones that would otherwise own the hotspot list.
    """
    if not src:
        return False
    lines = src.split(b"\n")
    if max((len(ln) for ln in lines), default=0) > MAX_SINGLE_LINE:
        return True
    return len(src) / max(1, len(lines)) > MAX_MEAN_LINE
