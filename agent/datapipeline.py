"""Data pipelines — agentic Medallion builds with AS-IS/TO-BE regression proof.

Implements the core mechanics of the 'Autonomous Agentic Data Pipelines'
design locally: the agent authors a pipeline spec (Bronze ingest is
mechanical; Silver/Gold transformations are SQL the Engineering-agent role
writes), then this module provides the deterministic harness the design
demands:

  • SOURCE ALIGNER   — input files are frozen (copied + hashed) so the AS-IS
                       and TO-BE variants provably run on identical data.
  • PARALLEL RUNS    — each variant executes into its own ephemeral SQLite
                       database: bronze_* (append-only + _load_ts /
                       _source_file / _process_id lineage columns), then the
                       variant's silver/gold SQL.
  • CHECKSUM-BISECTION DIFF — output tables are compared by hashing key-range
                       segments and recursively bisecting only mismatching
                       segments until the exact rows and columns that differ
                       are isolated; value-level report with float tolerance.
  • SELF-CORRECTION LOOP — on mismatch, the diff report (machine-readable) is
                       handed to the model to rewrite the TO-BE SQL;
                       execution-grounded, capped at MAX_ITERS, escalating
                       with a full report when it can't converge. Convergence
                       and escalations land in memory (episodic) and the
                       audit trail.

Local-scale, dependency-free by design: SQLite stands in for the design's
DuckDB sandbox (same ephemeral role). Segment hashes are computed client-side
because SQLite has no in-engine MD5 — locally there is no network to save, so
bisection's win here is comparison-work and row isolation, not transfer.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config

MAX_ITERS = 5
SEGMENTS = 16                 # initial bisection fan-out
DIFF_ROW_CAP = 50             # modified-row detail cap in reports


def _root() -> Path:
    d = config.AGENT_HOME / "pipelines"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pdir(name: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", name)[:40]
    d = _root() / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# --------------------------------------------------------------------------- #
#  spec
# --------------------------------------------------------------------------- #
REQUIRED = ("name", "sources", "primary_key", "silver_sql", "gold_sql",
            "compare")


def validate_spec(spec: dict) -> str:
    """'' when valid, else a human-readable problem."""
    for k in REQUIRED:
        if k not in spec:
            return f"spec is missing '{k}'"
    if not isinstance(spec["sources"], list) or not spec["sources"]:
        return "sources must be a non-empty list of {path, table}"
    for s in spec["sources"]:
        if not Path(str(s.get("path", ""))).expanduser().exists():
            return f"source file not found: {s.get('path')}"
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", str(s.get("table", ""))):
            return f"bad source table name: {s.get('table')}"
    for variant in ("asis", "tobe"):
        if variant not in spec["silver_sql"]:
            return f"silver_sql needs both variants; missing '{variant}'"
        if variant not in spec["gold_sql"]:
            return f"gold_sql needs both variants; missing '{variant}'"
    if not isinstance(spec["compare"], list) or not spec["compare"]:
        return "compare must list output tables to regression-test"
    return ""


def save(spec: dict) -> dict:
    err = validate_spec(spec)
    if err:
        return {"ok": False, "error": err}
    (_pdir(spec["name"]) / "pipeline.json").write_text(
        json.dumps(spec, indent=2), "utf-8")
    return {"ok": True, "dir": str(_pdir(spec["name"]))}


def load(name: str) -> dict | None:
    try:
        return json.loads((_pdir(name) / "pipeline.json").read_text("utf-8"))
    except Exception:
        return None


def list_pipelines() -> list:
    out = []
    for d in sorted(_root().iterdir()):
        if (d / "pipeline.json").exists():
            entry = {"name": d.name}
            try:
                entry["last"] = json.loads(
                    (d / "last_regression.json").read_text("utf-8"))
            except Exception:
                entry["last"] = None
            out.append(entry)
    return out


# --------------------------------------------------------------------------- #
#  source aligner — freeze inputs so both variants see identical data
# --------------------------------------------------------------------------- #
def freeze_sources(name: str) -> dict:
    spec = load(name)
    fdir = _pdir(name) / "frozen"
    shutil.rmtree(fdir, ignore_errors=True)
    fdir.mkdir(parents=True)
    manifest = []
    for s in spec["sources"]:
        src = Path(s["path"]).expanduser()
        dst = fdir / f"{s['table']}{src.suffix.lower()}"
        shutil.copyfile(src, dst)
        manifest.append({"table": s["table"], "file": dst.name,
                         "sha256": hashlib.sha256(
                             dst.read_bytes()).hexdigest()[:16],
                         "bytes": dst.stat().st_size})
    (fdir / "manifest.json").write_text(json.dumps(
        {"frozen_at": _iso(), "files": manifest}), "utf-8")
    return {"ok": True, "files": manifest}


# --------------------------------------------------------------------------- #
#  medallion runner — bronze (mechanical) then the variant's silver/gold SQL
# --------------------------------------------------------------------------- #
def _load_bronze(conn: sqlite3.Connection, table: str, path: Path,
                 process_id: str) -> int:
    import csv
    # Normalise: a source table named "bronze_x" (a natural name to pick,
    # since the tool's own vocabulary is "bronze tables") must NOT become
    # "bronze_bronze_x" — that silent double-prefix broke every SQL statement
    # written against the name the user actually gave.
    bronze_name = table if table.startswith("bronze_") else f"bronze_{table}"
    rows = []
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text("utf-8"))
        rows = data if isinstance(data, list) else [data]
        cols = sorted({k for r in rows for k in r})
        rows = [[r.get(c) for c in cols] for r in rows]
    else:
        with open(path, newline="", encoding="utf-8") as fh:
            rdr = csv.reader(fh)
            cols = next(rdr)
            rows = [r for r in rdr]
    colnames = [re.sub(r"[^a-zA-Z0-9_]", "_", c) for c in cols]
    coldef = ", ".join(f'"{c}" TEXT' for c in colnames)
    conn.execute(f'CREATE TABLE IF NOT EXISTS {bronze_name} ({coldef}, '
                 f'_load_ts TEXT, _source_file TEXT, _process_id TEXT)')
    ph = ",".join("?" * (len(colnames) + 3))
    now = _iso()
    conn.executemany(
        f'INSERT INTO {bronze_name} VALUES ({ph})',       # append-only
        [list(r) + [now, path.name, process_id] for r in rows])
    return len(rows)


def run_variant(name: str, variant: str) -> dict:
    """Execute one variant (asis|tobe) on the FROZEN sources into its own
    ephemeral database. Returns table row-counts or the execution error —
    errors are data for the loop, not exceptions."""
    spec = load(name)
    if spec is None:
        return {"ok": False, "error": "unknown pipeline"}
    fdir = _pdir(name) / "frozen"
    if not (fdir / "manifest.json").exists():
        freeze_sources(name)
    dbp = _pdir(name) / f"{variant}.db"
    dbp.unlink(missing_ok=True)                       # ephemeral, per design
    conn = sqlite3.connect(dbp)
    pid = uuid.uuid4().hex[:8]
    try:
        for s in spec["sources"]:
            files = list(fdir.glob(f"{s['table']}.*"))
            _load_bronze(conn, s["table"], files[0], pid)
        conn.executescript(spec["silver_sql"][variant])
        gold = spec["gold_sql"][variant]
        if isinstance(gold, dict):
            for tname, sql in gold.items():
                conn.execute(f'CREATE TABLE {tname} AS {sql}')
        else:
            conn.executescript(str(gold))
        conn.commit()
        counts = {}
        for (t,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"):
            counts[t] = conn.execute(
                f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        return {"ok": True, "tables": counts}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "variant": variant}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  checksum-bisection data diff
# --------------------------------------------------------------------------- #
def _cols(conn, table):
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _seg_hash(conn, table, cols, key, lo, hi, tol) -> tuple:
    """(hash, rowcount) for key range [lo, hi]. Floats rounded to tolerance
    so engine precision noise doesn't flag false diffs."""
    q = (f'SELECT {", ".join(chr(34)+c+chr(34) for c in cols)} '
         f'FROM "{table}" WHERE "{key}" >= ? AND "{key}" <= ? '
         f'ORDER BY "{key}"')
    h = hashlib.md5()
    n = 0
    for row in conn.execute(q, (lo, hi)):
        vals = []
        for v in row:
            if isinstance(v, float) or (isinstance(v, str)
                                        and re.fullmatch(r"-?\d+\.\d+", v)):
                try:
                    v = f"{round(float(v) / tol) * tol:.6f}" if tol else v
                except Exception:
                    pass
            vals.append(repr(v))
        h.update(("|".join(vals) + "\n").encode())
        n += 1
    return h.hexdigest(), n


def _keys_in(conn, table, key, lo, hi):
    return [r[0] for r in conn.execute(
        f'SELECT "{key}" FROM "{table}" WHERE "{key}" >= ? AND "{key}" <= ? '
        f'ORDER BY "{key}"', (lo, hi))]


def data_diff(name: str, table: str, tolerance: float = 0.0) -> dict:
    """Value-level diff of `table` between the AS-IS and TO-BE databases via
    key-range segment hashing with recursive bisection into mismatches."""
    spec = load(name)
    key = spec["primary_key"]
    ca = sqlite3.connect(_pdir(name) / "asis.db")
    cb = sqlite3.connect(_pdir(name) / "tobe.db")
    stats = {"segments_hashed": 0, "rows_row_compared": 0}
    try:
        cols_a, cols_b = _cols(ca, table), _cols(cb, table)
        if not cols_a or not cols_b:
            return {"ok": False,
                    "error": f"table '{table}' missing in "
                             f"{'AS-IS' if not cols_a else 'TO-BE'} output"}
        if set(cols_a) != set(cols_b):
            return {"ok": True, "match": False, "schema_mismatch": True,
                    "asis_cols": cols_a, "tobe_cols": cols_b,
                    "note": "column sets differ — schema-level change; "
                            "map or fix before value diffing"}
        cols = [c for c in cols_a if not c.startswith("_")]  # skip lineage
        kmin = min(x for x in (
            ca.execute(f'SELECT MIN("{key}") FROM "{table}"').fetchone()[0],
            cb.execute(f'SELECT MIN("{key}") FROM "{table}"').fetchone()[0])
            if x is not None) if True else None
        kmax = max(x for x in (
            ca.execute(f'SELECT MAX("{key}") FROM "{table}"').fetchone()[0],
            cb.execute(f'SELECT MAX("{key}") FROM "{table}"').fetchone()[0])
            if x is not None)
        added, removed, modified = [], [], []

        def bisect(lo, hi, width_rows):
            stats["segments_hashed"] += 2
            ha, na = _seg_hash(ca, table, cols, key, lo, hi, tolerance)
            hb, nb = _seg_hash(cb, table, cols, key, lo, hi, tolerance)
            if ha == hb and na == nb:
                return                                   # segment proven equal
            if max(na, nb) <= max(2, width_rows):        # leaf: row compare
                ka = _keys_in(ca, table, key, lo, hi)
                kb = _keys_in(cb, table, key, lo, hi)
                sa, sb = set(ka), set(kb)
                removed.extend(sorted(sa - sb))
                added.extend(sorted(sb - sa))
                for k in sorted(sa & sb):
                    ra = ca.execute(
                        f'SELECT {", ".join(chr(34)+c+chr(34) for c in cols)}'
                        f' FROM "{table}" WHERE "{key}"=?', (k,)).fetchone()
                    rb = cb.execute(
                        f'SELECT {", ".join(chr(34)+c+chr(34) for c in cols)}'
                        f' FROM "{table}" WHERE "{key}"=?', (k,)).fetchone()
                    stats["rows_row_compared"] += 1
                    changed = {}
                    for c, va, vb in zip(cols, ra, rb):
                        if _neq(va, vb, tolerance):
                            changed[c] = [va, vb]
                    if changed and len(modified) < DIFF_ROW_CAP:
                        modified.append({"key": k, "cols": changed})
                    elif changed:
                        modified.append(None)            # counted, uncapped
                return
            # recurse: split the key range
            mids = _split_range(lo, hi)
            for slo, shi in mids:
                bisect(slo, shi, width_rows)

        if kmin is not None:
            total = max(
                ca.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0],
                cb.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            bisect(kmin, kmax, max(2, total // SEGMENTS))
        modified_n = len(modified)
        modified = [m for m in modified if m]
        ta = ca.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        tb = cb.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        bad = len(added) + len(removed) + modified_n
        return {"ok": True, "table": table,
                "match": bad == 0 and ta == tb,
                "asis_rows": ta, "tobe_rows": tb,
                "added": added[:DIFF_ROW_CAP], "removed": removed[:DIFF_ROW_CAP],
                "modified": modified, "discrepancies": bad,
                "pct_match": round(100.0 * max(0, ta - bad) / ta, 3)
                if ta else 100.0,
                "tolerance": tolerance, **stats}
    finally:
        ca.close(); cb.close()


def _split_range(lo, hi):
    try:
        flo, fhi = float(lo), float(hi)
        if fhi > flo:
            mid = (flo + fhi) / 2
            cast = int if float(lo).is_integer() and float(hi).is_integer() \
                else float
            return [(lo, cast(mid)), (cast(mid + (1 if cast is int else 0)),
                                      hi)]
    except (TypeError, ValueError):
        pass
    return [(lo, lo), (hi, hi)] if lo != hi else [(lo, hi)]


def _neq(a, b, tol):
    if a == b:
        return False
    if tol:
        try:
            return abs(float(a) - float(b)) > tol
        except (TypeError, ValueError):
            pass
    return True


# --------------------------------------------------------------------------- #
#  execution-grounded self-correction loop
# --------------------------------------------------------------------------- #
_HEAL_SYSTEM = (
    "You are the Self-Healing agent of a data-pipeline system. You receive a "
    "pipeline spec's TO-BE SQL plus a machine-readable regression report "
    "(execution errors or a value-level data diff vs the AS-IS baseline). "
    "Reason from the EVIDENCE — the diff, not intuition — and return ONLY a "
    "JSON object {\"silver_sql\": \"...\", \"gold_sql\": {name: select}} "
    "with the corrected TO-BE SQL. Bronze tables are bronze_<source>; your "
    "silver SQL must create the silver_* tables; gold selects read from "
    "silver_*. No prose, no markdown fences — raw JSON only.")


def regression(name: str, brain=None, max_iters: int = MAX_ITERS) -> dict:
    """Freeze → run AS-IS → loop(run TO-BE → diff → heal) until convergence,
    an execution cap, or no healer. Every iteration is audited; the outcome
    is written to episodic memory by the caller (tool layer)."""
    spec = load(name)
    if spec is None:
        return {"status": "error", "error": "unknown pipeline"}
    freeze_sources(name)
    asis = run_variant(name, "asis")
    if not asis.get("ok"):
        _err = asis.get("error", "")
        _hint = ""
        if "no such table: bronze_" in _err:
            _hint = (" — check your SQL references the bronze table exactly "
                    "as named: source tables are auto-prefixed with 'bronze_' "
                    "UNLESS the source table name you gave already starts "
                    "with 'bronze_' (in which case no extra prefix is added).")
        return {"status": "error",
                "error": f"AS-IS baseline failed: {_err}{_hint}"}
    iterations = []
    for i in range(1, max_iters + 1):
        tobe = run_variant(name, "tobe")
        if tobe.get("ok"):
            diffs = [data_diff(name, t, float(spec.get("tolerance", 0)))
                     for t in spec["compare"]]
            bad = [d for d in diffs
                   if not d.get("ok") or not d.get("match")]
            summary = "; ".join(
                f"{d.get('table', '?')}: "
                + ("MATCH" if d.get("match") else
                   f"{d.get('discrepancies', d.get('error', '?'))} "
                   f"discrepancies") for d in diffs)
        else:
            diffs, bad = [tobe], [tobe]
            summary = f"TO-BE execution failed: {tobe['error']}"
        _audit(name, i, summary)
        iterations.append({"iteration": i, "summary": summary,
                           "converged": not bad})
        if not bad:
            result = {"status": "converged", "iterations": iterations,
                      "diffs": diffs, "at": _iso()}
            _save_last(name, result)
            return result
        if brain is None or i == max_iters:
            break
        healed = _heal(brain, spec, diffs)
        if not healed:
            iterations.append({"iteration": i,
                               "summary": "healer returned unusable output"})
            break
        spec["silver_sql"]["tobe"] = healed.get(
            "silver_sql", spec["silver_sql"]["tobe"])
        if isinstance(healed.get("gold_sql"), dict):
            spec["gold_sql"]["tobe"] = healed["gold_sql"]
        save(spec)
    result = {"status": "escalated", "iterations": iterations,
              "unresolved": [d for d in diffs
                             if not d.get("match", False)][:5],
              "note": "could not converge — human review needed; the TO-BE "
                      "SQL of the last attempt is saved in the pipeline spec",
              "at": _iso()}
    _save_last(name, result)
    return result


def _heal(brain, spec, diffs):
    try:
        payload = json.dumps({
            "tobe_silver_sql": spec["silver_sql"]["tobe"],
            "tobe_gold_sql": spec["gold_sql"]["tobe"],
            "primary_key": spec["primary_key"],
            "regression_report": diffs}, default=str)[:12000]
        resp = brain.chat([{"role": "user", "content": payload}],
                          [_HEAL_SYSTEM], None)
        text = "\n".join(b.text for b in resp.content
                         if getattr(b, "type", "") == "text").strip()
        text = re.sub(r"^```(json)?|```$", "", text.strip(),
                      flags=re.MULTILINE).strip()
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except Exception:
        return None


def _audit(name, iteration, summary):
    try:
        from . import audit
        audit.record("pipeline", name=name, detail=f"iteration {iteration}",
                     summary=summary[:250])
    except Exception:
        pass


def _save_last(name, result):
    try:
        slim = {k: v for k, v in result.items() if k != "diffs"}
        (_pdir(name) / "last_regression.json").write_text(
            json.dumps(slim, default=str), "utf-8")
    except Exception:
        pass
