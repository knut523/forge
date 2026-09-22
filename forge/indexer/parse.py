"""tree-sitter extraction — one source file in, structured records out.

A single iterative walker serves every grammar; the per-language differences
live in `languages.GRAMMAR`. Iterative rather than recursive on purpose: a
deeply-nested generated file would blow Python's recursion limit mid-index.

What we pull out, and why each earns its place:
  symbols  — the definitions an engineer needs to locate ("where is Foo")
  imports  — the file-level dependency edges, and how a ref resolves cross-file
  refs     — call/usage sites, which are what make "who calls this" answerable
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from tree_sitter_language_pack import get_parser

from .languages import GRAMMAR

_PARSERS: dict[str, object] = {}

MAX_SIG = 400

# A URL path: leading slash, at least two characters, and only the characters
# routes are actually built from. Deliberately strict — loosening it turns every
# filesystem path, regex and format string in the tree into a false seam.
_PATH_RE = re.compile(r"^/[A-Za-z0-9_\-./{}:$]{2,180}$")

# Absolute filesystem paths match the route shape exactly, and every codebase is
# full of them. Left in, "/dev/null" links two repos that share nothing.
_FS_ROOTS = frozenset({
    "dev", "proc", "sys", "tmp", "usr", "etc", "var", "bin", "sbin", "lib",
    "lib64", "mnt", "media", "srv", "home", "root", "opt", "boot", "run",
})


def _parser(lang: str):
    if lang not in _PARSERS:
        _PARSERS[lang] = get_parser(lang)
    return _PARSERS[lang]


def _text(node) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


@dataclass
class Symbol:
    name: str
    qualname: str
    kind: str
    start_line: int
    end_line: int
    signature: str = ""
    doc: str | None = None
    exported: bool = False
    parent: str | None = None


@dataclass
class Import:
    module: str
    symbol: str | None
    alias: str | None
    line: int


@dataclass
class Ref:
    name: str            # the bare callee name — what we resolve on
    full: str            # the written form, e.g. svc.get_user
    line: int
    src: str | None      # qualname of the enclosing symbol, None at module level
    kind: str = "call"


@dataclass
class Literal:
    """A string constant worth remembering — currently URL paths.

    These are the seam between repos. A frontend that fetches
    "/api/v1/ace/start" and a backend that declares "/start" under a router
    prefixed "/api/v1/ace" share no symbol and no import, so the symbol graph
    cannot connect them. The string can.
    """
    value: str
    line: int
    kind: str = "path"


@dataclass
class ParsedFile:
    lang: str
    loc: int
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    literals: list[Literal] = field(default_factory=list)
    error: str | None = None


# ─── helpers ────────────────────────────────────────────────────────────────

def _py_docstring(node) -> str | None:
    """Python docstring = a bare string as the first statement of the body."""
    body = node.child_by_field_name("body")
    if body is None or body.named_child_count == 0:
        return None
    first = body.named_child(0)
    if first.type == "expression_statement" and first.named_child_count:
        s = first.named_child(0)
        if s.type == "string":
            return _text(s).strip("\"'").strip()[:600]
    return None


def _jsdoc(node) -> str | None:
    """TS/JS doc = a block comment immediately above the decl (or its export)."""
    target = node
    if node.parent is not None and node.parent.type == "export_statement":
        target = node.parent
    prev = target.prev_sibling
    if prev is not None and prev.type == "comment":
        t = _text(prev)
        if t.startswith("/**"):
            return t.strip("/*").strip()[:600]
    return None


def _signature(node, lang: str, name: str) -> str:
    params = node.child_by_field_name("parameters")
    if params is None:
        # TS method/arrow decls carry params under a differently-named node
        for c in node.named_children:
            if c.type in ("formal_parameters", "parameters"):
                params = c
                break
    sig = name + (_text(params) if params is not None else "")
    ret = node.child_by_field_name("return_type")
    if ret is not None:
        sig += " " + _text(ret)
    return " ".join(sig.split())[:MAX_SIG]


def _is_exported(node) -> bool:
    p = node.parent
    return p is not None and p.type == "export_statement"


def _callee_name(node, lang: str) -> tuple[str, str] | None:
    """(bare_name, written_form) for a call node, or None if not resolvable."""
    fn = node.child_by_field_name("function")
    if fn is None:
        return None
    full = " ".join(_text(fn).split())[:200]
    if fn.type in ("identifier", "property_identifier"):
        return _text(fn), full
    if fn.type == "attribute":                      # python: obj.method(...)
        attr = fn.child_by_field_name("attribute")
        return (_text(attr), full) if attr is not None else None
    if fn.type == "member_expression":              # ts/js: obj.method(...)
        prop = fn.child_by_field_name("property")
        return (_text(prop), full) if prop is not None else None
    return None


def _py_imports(node) -> list[Import]:
    line = node.start_point[0] + 1
    out: list[Import] = []
    if node.type == "import_statement":
        for c in node.named_children:
            if c.type == "dotted_name":
                out.append(Import(_text(c), None, None, line))
            elif c.type == "aliased_import":
                nm = c.child_by_field_name("name")
                al = c.child_by_field_name("alias")
                out.append(Import(_text(nm), None, _text(al) or None, line))
    else:  # import_from_statement
        mod = node.child_by_field_name("module_name")
        module = _text(mod) if mod is not None else ""
        names = [c for c in node.named_children if c is not mod]
        if not names:
            out.append(Import(module, None, None, line))
        for c in names:
            if c.type == "aliased_import":
                nm = c.child_by_field_name("name")
                al = c.child_by_field_name("alias")
                out.append(Import(module, _text(nm), _text(al) or None, line))
            elif c.type in ("dotted_name", "identifier"):
                out.append(Import(module, _text(c), None, line))
            elif c.type == "wildcard_import":
                out.append(Import(module, "*", None, line))
    return out


def _ts_imports(node) -> list[Import]:
    line = node.start_point[0] + 1
    src = node.child_by_field_name("source")
    module = _text(src).strip("\"'`") if src is not None else ""
    if not module:
        return []
    out: list[Import] = []
    clause = next((c for c in node.named_children if c.type == "import_clause"), None)
    if clause is None:
        return [Import(module, None, None, line)]          # bare side-effect import
    for c in clause.named_children:
        if c.type == "identifier":                          # default import
            out.append(Import(module, "default", _text(c), line))
        elif c.type == "named_imports":
            for spec in c.named_children:
                if spec.type != "import_specifier":
                    continue
                nm = spec.child_by_field_name("name")
                al = spec.child_by_field_name("alias")
                out.append(Import(module, _text(nm), _text(al) or None, line))
        elif c.type == "namespace_import":
            ident = next((g for g in c.named_children if g.type == "identifier"), None)
            out.append(Import(module, "*", _text(ident) or None, line))
    return out or [Import(module, None, None, line)]


def _ts_lexical_symbols(node, scope: tuple[str, ...]) -> list[Symbol]:
    """Arrow-function and UPPER_CASE consts — declarations the node-type map
    can't catch, because the interesting part is the initialiser."""
    out: list[Symbol] = []
    for dec in node.named_children:
        if dec.type != "variable_declarator":
            continue
        nm = dec.child_by_field_name("name")
        if nm is None or nm.type != "identifier":
            continue
        name = _text(nm)
        val = dec.child_by_field_name("value")
        vtype = val.type if val is not None else ""
        if vtype in ("arrow_function", "function_expression", "function"):
            kind = "function"
            sig = _signature(val, "ts", name)
        elif not scope and name.isupper():
            kind = "const"
            sig = f"{name} = {' '.join(_text(val).split())[:120]}" if val is not None else name
        else:
            continue
        out.append(Symbol(
            name=name,
            qualname=".".join(scope + (name,)),
            kind=kind,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=sig,
            doc=_jsdoc(node),
            exported=_is_exported(node),
            parent=".".join(scope) or None,
        ))
    return out


def _py_const(node, scope: tuple[str, ...]) -> Symbol | None:
    """Module-level UPPER_CASE assignment — the config surface of a Python file."""
    if scope:
        return None
    left = node.child_by_field_name("left")
    if left is None or left.type != "identifier":
        return None
    name = _text(left)
    if not name.isupper() or len(name) < 2:
        return None
    right = node.child_by_field_name("right")
    return Symbol(
        name=name,
        qualname=name,
        kind="const",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        signature=f"{name} = {' '.join(_text(right).split())[:120]}" if right is not None else name,
        exported=True,
        parent=None,
    )


# ─── the walker ─────────────────────────────────────────────────────────────

def parse_source(src: bytes, lang: str) -> ParsedFile:
    cfg = GRAMMAR.get(lang)
    if cfg is None:
        return ParsedFile(lang=lang, loc=0, error=f"no grammar for {lang}")

    loc = src.count(b"\n") + 1
    out = ParsedFile(lang=lang, loc=loc)
    try:
        tree = _parser(lang).parse(src)
    except Exception as e:                       # a grammar crash must not kill the index
        out.error = f"{type(e).__name__}: {str(e)[:140]}"
        return out

    defs = cfg["defs"]
    import_types = cfg["imports"]
    call_types = cfg["calls"]
    string_types = cfg["string_types"]
    is_py = lang == "python"

    # (node, scope) — scope carries the enclosing qualname parts, so a symbol's
    # parent falls out of the traversal instead of needing a second pass.
    stack: list[tuple[object, tuple[str, ...]]] = [(tree.root_node, ())]
    while stack:
        node, scope = stack.pop()
        ntype = node.type
        child_scope = scope

        if ntype in defs:
            nm = node.child_by_field_name("name")
            if nm is not None:
                name = _text(nm)
                kind = defs[ntype]
                if kind == "function" and scope:
                    kind = "method"          # a def inside a class is a method
                qual = ".".join(scope + (name,))
                out.symbols.append(Symbol(
                    name=name,
                    qualname=qual,
                    kind=kind,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    signature=_signature(node, lang, name),
                    doc=_py_docstring(node) if is_py else _jsdoc(node),
                    exported=True if is_py and not scope else _is_exported(node),
                    parent=".".join(scope) or None,
                ))
                child_scope = scope + (name,)

        elif ntype in import_types:
            out.imports.extend(_py_imports(node) if is_py else _ts_imports(node))

        elif ntype in call_types:
            hit = _callee_name(node, lang)
            if hit is not None:
                out.refs.append(Ref(hit[0], hit[1], node.start_point[0] + 1,
                                    ".".join(scope) or None))

        elif not is_py and ntype in ("lexical_declaration", "variable_declaration"):
            syms = _ts_lexical_symbols(node, scope)
            out.symbols.extend(syms)
            # an arrow-function const opens a scope for the calls inside it
            if len(syms) == 1 and syms[0].kind == "function":
                child_scope = scope + (syms[0].name,)

        elif is_py and ntype == "assignment":
            c = _py_const(node, scope)
            if c is not None:
                out.symbols.append(c)

        elif ntype in string_types:
            raw = _text(node)
            val = raw.strip("\"'`")
            # `${id}` and `{id}` are the same route slot; normalise so a
            # template literal matches the server's declared parameter.
            norm = re.sub(r"\$\{[^}]*\}", "{}", val)
            norm = re.sub(r"\{[^}]*\}", "{}", norm)
            # A client almost always writes `${API_URL}/api/v1/...`, so the
            # interesting path is preceded by a base-URL placeholder. Drop it,
            # or every real call site fails the leading-slash test.
            while norm.startswith("{}"):
                norm = norm[2:]
            if (_PATH_RE.match(norm) and "//" not in norm
                    and norm.split("/")[1].lower() not in _FS_ROOTS):
                out.literals.append(Literal(norm.rstrip("/") or "/",
                                            node.start_point[0] + 1))

        elif not is_py and ntype in ("jsx_opening_element", "jsx_self_closing_element"):
            nm = node.child_by_field_name("name")
            if nm is not None:
                name = _text(nm)
                # lowercase names are html tags; uppercase are components
                if name[:1].isupper():
                    out.refs.append(Ref(name.split(".")[-1], name,
                                        node.start_point[0] + 1,
                                        ".".join(scope) or None, kind="jsx"))

        for i in range(node.named_child_count - 1, -1, -1):
            stack.append((node.named_child(i), child_scope))

    return out
