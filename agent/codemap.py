"""Code map — what imports what, and what breaks if you change it.

Reading a folder tells you what exists. It doesn't tell you which module
everything leans on, which pair quietly import each other, or which file you
can delete without anyone noticing. Those are the questions you have before
touching an unfamiliar codebase, and none of them are answerable by opening
files one at a time.

This builds the import graph statically — no engine call, no guessing. It
parses Python with the `ast` module rather than regexes, because a regex over
source will find `import` inside a string or a comment and report a
dependency that doesn't exist. JavaScript has no equivalent in the standard
library, so that side IS a regex, and says so: it is a good approximation and
not a parse.

Four things worth knowing about a codebase, in the order people ask:

  What depends on this? — the blast radius before a change.
  What does everything depend on? — the chokepoints, where a mistake is
    expensive and a test is worth writing.
  What imports each other in a circle? — the pairs that can't be understood,
    tested, or moved separately.
  What does nothing import? — either an entry point or dead weight, and the
    difference matters.
"""
from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timezone
from pathlib import Path

# Directories never worth mapping: dependencies, build output, caches.
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist",
             "build", ".mypy_cache", ".pytest_cache", ".idea", ".vscode",
             "site-packages", ".next", "coverage", "htmlcov"}

PY_EXT = {".py"}
JS_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs"}
MAX_FILES = 4000
MAX_BYTES = 1_500_000

# Shipped with Python, so not a dependency you took on.
try:
    import sys as _sys
    _STDLIB = set(_sys.stdlib_module_names)          # 3.10+
except AttributeError:                               # pragma: no cover
    _STDLIB = {"os", "sys", "re", "json", "time", "math", "pathlib",
               "datetime", "typing", "collections", "itertools", "functools",
               "subprocess", "threading", "logging", "sqlite3", "hashlib",
               "uuid", "shutil", "tempfile", "traceback", "textwrap", "ast"}
_STDLIB |= {"__future__"}

# JS/TS import forms. Explicitly an approximation — see the module docstring.
_JS_IMPORT = re.compile(
    r"""(?:^|\n)\s*(?:import\s[^;'"]*from\s*['"]([^'"]+)['"]"""
    r"""|import\s*['"]([^'"]+)['"]"""
    r"""|(?:const|let|var)\s+[^=]+=\s*require\(\s*['"]([^'"]+)['"]\s*\))""",
    re.M)


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _walk(root: Path) -> list:
    out = []
    for p in root.rglob("*"):
        if len(out) >= MAX_FILES:
            break
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in (PY_EXT | JS_EXT):
            continue
        try:
            if p.stat().st_size > MAX_BYTES:
                continue
        except OSError:
            continue
        out.append(p)
    return out


def _py_imports(path: Path) -> tuple:
    """Imports and definitions, via the real parser.

    A regex over source finds `import` inside strings and comments and
    reports dependencies that don't exist."""
    try:
        tree = ast.parse(path.read_text("utf-8", "replace"), str(path))
    except SyntaxError as exc:
        return [], [], f"syntax error on line {exc.lineno}"
    imports, defines = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # `from . import memory` carries the target in the NAMES, not
                # in `module`. Dropping it collapsed every sibling import onto
                # the package itself, which made `agent` look like the thing
                # 56 modules depend on when nothing imports it directly.
                dots = "." * node.level
                if node.module:
                    # the imported NAME may itself be a module —
                    # `from pkg import a` means pkg.a, not pkg
                    imports += [f"{dots}{node.module}.{a.name}"
                                for a in node.names]
                    imports.append(dots + node.module)
                else:
                    imports += [dots + a.name for a in node.names]
            elif node.module:
                # same for an absolute import: `from pkg import a` is a
                # dependency on pkg.a when that's a module, and resolving it
                # to the package hid every real edge behind one
                imports += [f"{node.module}.{a.name}" for a in node.names]
                imports.append(node.module)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defines.append(node.name)
        elif isinstance(node, ast.ClassDef):
            defines.append(node.name)
    return imports, defines, ""


def _js_imports(path: Path) -> tuple:
    text = path.read_text("utf-8", "replace")
    hits = [g for m in _JS_IMPORT.finditer(text) for g in m.groups() if g]
    defines = re.findall(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)",
                         text, re.M)
    defines += re.findall(r"^\s*(?:export\s+)?class\s+(\w+)", text, re.M)
    return hits, defines, ""


def _module_name(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    if path.suffix in PY_EXT:
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)
    return str(rel).replace("\\", "/")


def _resolve(raw: str, source: Path, root: Path, known: dict) -> str:
    """Turn an import string into a module in THIS codebase, or nothing.

    Third-party and standard-library imports are not part of the map — the
    question is how your own code hangs together."""
    s = (raw or "").strip()
    if not s:
        return ""
    if s.startswith("."):                     # python relative import
        pkg = _module_name(source, root).rsplit(".", 1)[0]
        ups = len(s) - len(s.lstrip("."))
        base = pkg.split(".")
        base = base[:len(base) - (ups - 1)] if ups > 1 else base
        tail = s.lstrip(".")
        cand = ".".join([p for p in base if p] + ([tail] if tail else []))
        return cand if cand in known else ""
    if s.startswith("./") or s.startswith("../"):     # js relative import
        target = (source.parent / s).resolve()
        for ext in ("", ".js", ".jsx", ".ts", ".tsx", ".mjs"):
            cand = Path(str(target) + ext)
            try:
                name = _module_name(cand, root)
            except ValueError:
                continue
            if name in known:
                return name
        return ""
    # an absolute python import that happens to be one of ours
    if s in known:
        return s
    head = s.split(".")[0]
    for name in known:
        if name == head or name.endswith("." + head):
            return name
    return ""


def scan(folder: str) -> dict:
    """Build the import graph for a folder."""
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        return {"ok": False, "error": f"{folder} isn't a folder I can read."}
    files = _walk(root)
    if not files:
        return {"ok": False,
                "error": ("No Python or JavaScript found there. This maps "
                          ".py, .js, .ts and .tsx — it skips node_modules, "
                          ".venv and build output.")}

    known, meta = {}, {}
    for f in files:
        name = _module_name(f, root)
        known[name] = f
        meta[name] = {"path": str(f.relative_to(root)),
                      "lines": f.read_text("utf-8", "replace").count("\n") + 1,
                      "kind": "python" if f.suffix in PY_EXT else "javascript"}

    edges, problems, external = [], [], {}
    for name, f in known.items():
        if f.suffix in PY_EXT:
            raw, defines, err = _py_imports(f)
        else:
            raw, defines, err = _js_imports(f)
        if err:
            problems.append({"module": name, "problem": err})
        meta[name]["defines"] = len(defines)
        for r in raw:
            target = _resolve(r, f, root, known)
            if target and target != name:
                if not any(e["from"] == name and e["to"] == target
                           for e in edges):
                    edges.append({"from": name, "to": target})
            elif not target and not r.startswith((".", "/")):
                head = r.split(".")[0].split("/")[0]
                # the standard library isn't a dependency anyone chose, and
                # listing json and pathlib as your top dependencies is noise
                if head and head not in _STDLIB:
                    external[head] = external.get(head, 0) + 1

    return {"ok": True, "at": _iso(), "root": str(root),
            "modules": meta, "edges": edges, "problems": problems,
            "external": dict(sorted(external.items(),
                                    key=lambda kv: -kv[1])[:25]),
            "counts": {"modules": len(known), "edges": len(edges),
                       "lines": sum(m["lines"] for m in meta.values())},
            "note": ("Python is parsed properly; JavaScript is matched with a "
                     "regex, which is a good approximation and not a parse.")}


# --------------------------------------------------------------------------- #
#  the four questions
# --------------------------------------------------------------------------- #
def _adj(edges: list) -> tuple:
    out, inc = {}, {}
    for e in edges:
        out.setdefault(e["from"], set()).add(e["to"])
        inc.setdefault(e["to"], set()).add(e["from"])
    return out, inc


def impact(graph: dict, module: str) -> dict:
    """Everything that would feel a change to this module."""
    _, inc = _adj(graph["edges"])
    seen, queue = set(), [module]
    while queue:
        cur = queue.pop()
        for dep in inc.get(cur, ()):
            if dep not in seen:
                seen.add(dep)
                queue.append(dep)
    direct = sorted(inc.get(module, ()))
    return {"module": module, "direct": direct,
            "all_affected": sorted(seen), "count": len(seen),
            "verdict": ("nothing imports it — changing it is safe, or it's "
                        "dead" if not seen else
                        f"{len(seen)} module(s) would feel it, "
                        f"{len(direct)} directly")}


def hotspots(graph: dict, top: int = 10) -> list:
    """What everything leans on. A mistake here is expensive; a test is
    worth writing."""
    _, inc = _adj(graph["edges"])
    rows = [{"module": m, "imported_by": len(inc.get(m, ())),
             "lines": graph["modules"][m]["lines"]}
            for m in graph["modules"]]
    rows.sort(key=lambda r: (-r["imported_by"], -r["lines"]))
    return [r for r in rows if r["imported_by"]][:top]


def cycles(graph: dict) -> list:
    """Modules that import each other in a circle.

    A cycle is not a style complaint: those modules cannot be understood,
    tested or moved independently, and an import cycle is how a change to one
    quietly breaks the other."""
    out, _ = _adj(graph["edges"])
    index, low, stack, on, found, counter = {}, {}, [], set(), [], [0]

    def strong(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in out.get(v, ()):
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                found.append(sorted(comp))

    import sys
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 10000))
    try:
        for v in list(graph["modules"]):
            if v not in index:
                strong(v)
    finally:
        sys.setrecursionlimit(limit)
    return sorted(found, key=len, reverse=True)


def orphans(graph: dict, entry_hints=("main", "server", "app", "index",
                                      "__init__", "setup", "run")) -> dict:
    """Imported by nothing. Either an entry point or dead weight — and the
    difference is worth stating rather than leaving you to guess."""
    _, inc = _adj(graph["edges"])
    entries, dead = [], []
    for m in graph["modules"]:
        if inc.get(m):
            continue
        tail = m.replace("/", ".").split(".")[-1].replace(".js", "")
        (entries if any(h in tail for h in entry_hints) else dead).append(m)
    return {"entry_points": sorted(entries), "unreferenced": sorted(dead),
            "note": ("Unreferenced means nothing in THIS folder imports it. "
                     "A script you run by hand, or something loaded "
                     "dynamically, will show up here and is not dead.")}


def report(folder: str) -> dict:
    g = scan(folder)
    if not g.get("ok"):
        return g
    cyc = cycles(g)
    orp = orphans(g)
    return {**g, "hotspots": hotspots(g), "cycles": cyc, "orphans": orp,
            "summary": _summary(g, cyc, orp)}


def _summary(g: dict, cyc: list, orp: dict) -> str:
    c = g["counts"]
    bits = [f"{c['modules']} modules, {c['lines']:,} lines, "
            f"{c['edges']} internal imports"]
    if cyc:
        bits.append(f"{len(cyc)} import cycle(s) — those modules can't be "
                    f"changed independently")
    if orp["unreferenced"]:
        bits.append(f"{len(orp['unreferenced'])} module(s) nothing imports")
    return ". ".join(bits) + "."


def save(folder: str, result: dict) -> str:
    from . import config
    d = config.AGENT_HOME / "codemap"
    d.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", Path(folder).name.lower()).strip("-")
    p = d / f"{slug or 'map'}.json"
    p.write_text(json.dumps(result, indent=2), "utf-8")
    return str(p)


# =========================================================================== #
#  Drawing it
#
#  Ninety modules and three hundred arrows drawn at once is a hairball: it
#  looks impressive and tells you nothing. Two decisions make it readable.
#
#  LAYERS. Modules are placed by how deep they sit in the dependency stack —
#  things nothing depends on at the top, foundations at the bottom. That turns
#  an arbitrary tangle into the shape people draw by hand when explaining an
#  architecture, and arrows mostly point one way.
#
#  FOCUS. You rarely want the whole graph. Pick a module and you get its
#  neighbourhood: what it uses, what uses it, one hop out unless you ask for
#  more. That's the question you actually had.
# =========================================================================== #
def _depths(nodes: list, out: dict) -> dict:
    """How deep each module sits. Foundations are deepest.

    Cycles have to be collapsed first. A longest-path relaxation over a graph
    containing one keeps finding a longer path round the loop, so it runs to
    its iteration bound and reports a depth of nearly N — which drew 45
    modules across 92 columns and 24,000 pixels. Members of a cycle share a
    depth because, for the purpose of "what sits beneath what", they are one
    thing.
    """
    # Tarjan, iteratively: a recursive version blows the stack on a big repo
    idx, low, on, stack, comp, order, counter = {}, {}, set(), [], {}, [], [0]
    for root in nodes:
        if root in idx:
            continue
        work = [(root, iter(sorted(out.get(root, ()))))]
        idx[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in idx:
                    idx[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(out.get(w, ())))))
                    advanced = True
                    break
                if w in on:
                    low[v] = min(low[v], idx[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == idx[v]:
                members = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp[w] = v          # v represents this cycle
                    members.append(w)
                    if w == v:
                        break
                order.append(v)

    # depth over the collapsed graph, which has no cycles by construction
    cout = {}
    for n in nodes:
        for m in out.get(n, ()):
            if m in comp and comp[m] != comp[n]:
                cout.setdefault(comp[n], set()).add(comp[m])
    cdepth = {c: 0 for c in set(comp.values())}
    for _ in range(len(cdepth) + 1):
        changed = False
        for c in cdepth:
            for d in cout.get(c, ()):
                if cdepth[d] < cdepth[c] + 1:
                    cdepth[d] = cdepth[c] + 1
                    changed = True
        if not changed:
            break
    return {n: cdepth.get(comp.get(n, n), 0) for n in nodes}


def neighbourhood(graph: dict, focus: str, hops: int = 1) -> set:
    out, inc = _adj(graph["edges"])
    seen = {focus}
    edge = {focus}
    for _ in range(max(1, hops)):
        nxt = set()
        for n in edge:
            nxt |= out.get(n, set()) | inc.get(n, set())
        nxt -= seen
        seen |= nxt
        edge = nxt
        if not edge:
            break
    return seen


def layout(graph: dict, focus: str = "", hops: int = 1,
           limit: int = 45) -> dict:
    """Positions for a drawing: layered, and small enough to read."""
    out, inc = _adj(graph["edges"])
    all_nodes = list(graph["modules"])

    if focus and focus in graph["modules"]:
        keep = neighbourhood(graph, focus, hops)
        why = (f"{focus} and everything within {hops} hop(s) — "
               f"{len(keep)} module(s)")
    else:
        # the most connected, because those are the ones worth seeing
        ranked = sorted(all_nodes,
                        key=lambda n: -(len(inc.get(n, ())) * 2
                                        + len(out.get(n, ()))))
        keep = set(ranked[:limit])
        why = (f"the {len(keep)} most connected of {len(all_nodes)} modules"
               if len(all_nodes) > limit else "every module")

    edges = [e for e in graph["edges"]
             if e["from"] in keep and e["to"] in keep]
    sub_out = {}
    for e in edges:
        sub_out.setdefault(e["from"], set()).add(e["to"])
    depth = _depths(sorted(keep), sub_out)

    rows = {}
    for n in sorted(keep, key=lambda m: (depth[m], m)):
        rows.setdefault(depth[n], []).append(n)

    # Left to right: a dependency chain reads as a sentence that way, and a
    # wide diagram fits a wide screen. Top-down made deep graphs into a
    # narrow column you had to scroll.
    nodes, W, H, GAP = [], 180, 44, 18
    COL = W + 90                       # room for the arrows between columns
    for d, row in sorted(rows.items()):
        for i, n in enumerate(row):
            nodes.append({
                "id": n,
                "label": n.split(".")[-1] or n,
                "full": n,
                "x": d * COL, "y": i * (H + GAP),
                "w": W, "h": H,
                "layer": d,
                "lines": graph["modules"][n]["lines"],
                "used_by": len(inc.get(n, ())),
                "uses": len(out.get(n, ())),
                "kind": graph["modules"][n]["kind"],
                "focus": n == focus,
            })
    width = max([n["x"] + W for n in nodes] or [W])
    height = max([n["y"] + H for n in nodes] or [H])
    # centre each COLUMN vertically, so the diagram reads as a spine rather
    # than everything hanging off the top edge
    for d, row in sorted(rows.items()):
        span = len(row) * (H + GAP) - GAP
        shift = (height - span) / 2
        for n in nodes:
            if n["layer"] == d:
                n["y"] += shift
    return {"nodes": nodes, "edges": edges, "width": width + 40,
            "height": height + 40, "layers": len(rows),
            "showing": why,
            "direction": "LR",
            "note": ("Arrows point right, towards what a module depends on. "
                     "The left edge is where work starts; the right edge is "
                     "what everything rests on.")}


# --------------------------------------------------------------------------- #
#  exports, for tools that draw better than a browser
# --------------------------------------------------------------------------- #
def to_mermaid(graph: dict, focus: str = "", limit: int = 45) -> str:
    """Mermaid renders inline on GitHub, so the map goes in the README."""
    lay = layout(graph, focus, limit=limit)
    safe = {}
    lines = ["graph LR"]
    for i, n in enumerate(lay["nodes"]):
        key = f"n{i}"
        safe[n["id"]] = key
        lines.append(f'  {key}["{n["label"]}"]')
    for e in lay["edges"]:
        if e["from"] in safe and e["to"] in safe:
            lines.append(f'  {safe[e["from"]]} --> {safe[e["to"]]}')
    return "\n".join(lines)


def to_dot(graph: dict, focus: str = "", limit: int = 45) -> str:
    """Graphviz. `dot -Tsvg map.dot -o map.svg` gives a proper drawing."""
    lay = layout(graph, focus, limit=limit)
    lines = ["digraph codemap {", "  rankdir=LR;",
             '  node [shape=box style="rounded,filled" fillcolor="#f4f4f5" '
             'fontname="Segoe UI" fontsize=10];',
             '  edge [color="#94a3b8" arrowsize=0.7];']
    for n in lay["nodes"]:
        fill = "#fde68a" if n["focus"] else "#f4f4f5"
        lines.append(f'  "{n["id"]}" [label="{n["label"]}\\n{n["lines"]} lines"'
                     f' fillcolor="{fill}"];')
    for e in lay["edges"]:
        lines.append(f'  "{e["from"]}" -> "{e["to"]}";')
    return "\n".join(lines) + "\n}\n"


def to_csv(graph: dict) -> str:
    """An edge list. Visio's Data Visualizer builds a diagram from exactly
    this, which is the shortest route from here to something you can edit by
    hand and put in a document."""
    rows = ["Source,Target,SourceLines,TargetUsedBy"]
    _, inc = _adj(graph["edges"])
    for e in graph["edges"]:
        rows.append(f'{e["from"]},{e["to"]},'
                    f'{graph["modules"][e["from"]]["lines"]},'
                    f'{len(inc.get(e["to"], ()))}')
    return "\n".join(rows) + "\n"


def export(folder: str, fmt: str = "mermaid", focus: str = "") -> dict:
    g = scan(folder)
    if not g.get("ok"):
        return g
    fmt = (fmt or "mermaid").lower()
    if fmt == "dot":
        text, ext = to_dot(g, focus), "dot"
    elif fmt == "csv":
        text, ext = to_csv(g), "csv"
    elif fmt == "mermaid":
        text, ext = to_mermaid(g, focus), "mmd"
    else:
        return {"ok": False,
                "error": f"'{fmt}' isn't a format I write. Try mermaid, dot "
                         f"or csv."}
    from . import config
    d = config.AGENT_HOME / "codemap"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{Path(folder).name or 'map'}.{ext}"
    p.write_text(text, "utf-8")
    hint = {"mermaid": "paste into a README — GitHub draws it inline",
            "dot": "dot -Tsvg this.dot -o map.svg",
            "csv": "Visio → Data Visualizer → import this as an edge list"}
    return {"ok": True, "format": fmt, "path": str(p), "text": text[:4000],
            "how": hint[fmt]}
