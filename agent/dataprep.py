"""Cleaning data and making features — visibly, and never in place.

Two things make this dangerous to automate, so both are designed around
rather than ignored.

**Silent cleaning is worse than dirty data.** If a tool quietly drops 400
rows, fills 2,000 blanks with a median and merges three spellings of a region,
you now have a dataset nobody can reason about and a model built on decisions
no one made. So nothing here changes the original file, every change is
counted before it happens, and the result carries a record of exactly what was
done.

**An outlier is often the most interesting row.** The fraud, the churn, the
one contract worth more than the rest of the book. Deleting outliers
automatically is how you train a model to predict everything except what you
care about. They are flagged, never removed by default.

**Generated features usually add noise.** Multiply every column by every other
column and you get five hundred features, a model that fits beautifully and
predicts nothing. So features are proposed for reasons — a date has a weekday
in it, a pair of amounts has a ratio in it — and then *measured*: kept only if
the held-out score actually improves. A feature that doesn't earn its place is
reported as not having earned it.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config

MAX_CATEGORY_MERGE = 40


def _dir() -> Path:
    d = config.AGENT_HOME / "dataprep"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# --------------------------------------------------------------------------- #
#  1. what's wrong with it
# --------------------------------------------------------------------------- #
def diagnose(path: str) -> dict:
    """Every problem found, with how much of the data it touches.

    Reports rather than fixes. A count is what tells you whether a problem is
    worth a decision — "3 duplicate rows" and "40% of your rows are
    duplicates" call for very different responses."""
    import pandas as pd
    from . import modelbuild
    try:
        df = modelbuild._load(path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    n = len(df)
    issues = []

    dupes = int(df.duplicated().sum())
    if dupes:
        issues.append({
            "kind": "duplicate rows", "count": dupes,
            "share": round(dupes / n * 100, 1),
            "what": f"{dupes} row(s) are exact copies of another row",
            "fix": "drop_duplicates",
            "risk": ("If the same event legitimately happens twice — two "
                     "identical orders, two identical readings — these aren't "
                     "duplicates and dropping them loses real data."),
        })

    for c in df.columns:
        s = df[c]
        miss = int(s.isna().sum())
        if miss:
            issues.append({
                "kind": "missing values", "column": str(c), "count": miss,
                "share": round(miss / n * 100, 1),
                "what": f"'{c}' is empty in {miss} row(s) "
                        f"({miss / n * 100:.0f}%)",
                "fix": ("drop_column" if miss / n > 0.6 else "fill"),
                "risk": ("More than half of it is missing — filling that in "
                         "invents most of the column."
                         if miss / n > 0.6 else
                         "Filling replaces an unknown with a guess. Whether "
                         "that's safe depends on why it's missing."),
            })
        if s.nunique(dropna=True) <= 1:
            issues.append({
                "kind": "constant column", "column": str(c), "count": n,
                "share": 100.0,
                "what": f"'{c}' has the same value in every row",
                "fix": "drop_column",
                "risk": "None — a column that never varies teaches nothing.",
            })
        # numbers stored as text: very common out of Excel and SQL exports
        if not pd.api.types.is_numeric_dtype(s):
            vals = s.dropna().astype(str).head(400)
            if len(vals) and _mostly_numeric(vals):
                issues.append({
                    "kind": "numbers stored as text", "column": str(c),
                    "count": len(vals), "share": 100.0,
                    "what": f"'{c}' looks numeric but is stored as text "
                            f"(e.g. {vals.iloc[0]!r})",
                    "fix": "to_number",
                    "risk": "Anything that won't convert becomes missing, "
                            "which is honest but worth seeing first.",
                })
            elif len(vals) and _mostly_dates(vals):
                issues.append({
                    "kind": "dates stored as text", "column": str(c),
                    "count": len(vals), "share": 100.0,
                    "what": f"'{c}' looks like dates but is text "
                            f"(e.g. {vals.iloc[0]!r})",
                    "fix": "to_date",
                    "risk": "Ambiguous formats (03/04 could be March or "
                            "April) are read one way — check a few.",
                })
            else:
                variants = _case_variants(s)
                if variants:
                    issues.append({
                        "kind": "same category, different spellings",
                        "column": str(c), "count": len(variants),
                        "share": round(len(variants) / max(1, s.nunique())
                                       * 100, 1),
                        "what": (f"'{c}' has values that differ only by case "
                                 f"or spacing: "
                                 + "; ".join(f"{' / '.join(v)}"
                                             for v in variants[:3])),
                        "fix": "normalise_text",
                        "risk": "Only merges case and whitespace. 'JHB' and "
                                "'Johannesburg' are left alone — that's a "
                                "judgement about your data, not a typo.",
                    })
        else:
            out = _outliers(s)
            if out["count"]:
                issues.append({
                    "kind": "extreme values", "column": str(c),
                    "count": out["count"],
                    "share": round(out["count"] / n * 100, 1),
                    "what": (f"'{c}' has {out['count']} value(s) far outside "
                             f"the rest (beyond {out['low']:.4g} to "
                             f"{out['high']:.4g})"),
                    "fix": "flag_only",
                    "risk": ("These are FLAGGED, not removed. An outlier is "
                             "often the most interesting row — the fraud, the "
                             "big contract. Deleting them trains a model to "
                             "predict everything except what matters."),
                })

    return {"ok": True, "path": str(Path(path).expanduser()),
            "rows": n, "columns": len(df.columns),
            "issues": sorted(issues, key=lambda i: -i["share"]),
            "clean": not issues,
            "summary": (f"{len(issues)} thing(s) worth a decision."
                        if issues else "Nothing obviously wrong."),
            "note": ("Nothing has been changed. Each fix is applied only when "
                     "you ask, and always to a copy."),
            "at": _iso()}


def _mostly_numeric(vals) -> bool:
    """Is this a number wearing a costume, or text that starts with a letter?

    Stripping a bare leading 'R' turned the id 'R0' into the number 0, so an
    identifier column looked like South African currency. Acting on that would
    have destroyed it. A currency symbol only counts when it's followed by a
    space or by a digit and a separator — 'R 8,784' is money, 'R0' is an id.
    """
    ok = 0
    for v in vals:
        t = str(v).strip()
        # unambiguous symbols, or 'R' where it's clearly a price
        t = re.sub(r"^[$€£]\s*", "", t)
        t = re.sub(r"^R(?=\s|\d{1,3}[,.]\d)", "", t)
        t = t.replace(",", "").replace(" ", "").rstrip("%")
        if re.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", t or "x"):
            ok += 1
    return ok / max(1, len(vals)) > 0.9


_DATE_RX = re.compile(
    r"^\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})")


def _mostly_dates(vals) -> bool:
    ok = sum(1 for v in vals if _DATE_RX.match(str(v)))
    return ok / max(1, len(vals)) > 0.9


def _case_variants(s) -> list:
    groups = {}
    for v in s.dropna().astype(str).unique()[:2000]:
        key = re.sub(r"\s+", " ", v).strip().lower()
        groups.setdefault(key, set()).add(v)
    return [sorted(v) for v in groups.values() if len(v) > 1]


def _outliers(s) -> dict:
    try:
        q1, q3 = float(s.quantile(0.25)), float(s.quantile(0.75))
    except Exception:
        return {"count": 0, "low": 0, "high": 0}
    iqr = q3 - q1
    if iqr <= 0:
        return {"count": 0, "low": q1, "high": q3}
    low, high = q1 - 3 * iqr, q3 + 3 * iqr
    return {"count": int(((s < low) | (s > high)).sum()),
            "low": low, "high": high}


# --------------------------------------------------------------------------- #
#  2. cleaning — to a copy, with a record
# --------------------------------------------------------------------------- #
def clean(path: str, fixes: list = None, out_name: str = "",
          apply_all_safe: bool = False) -> dict:
    """Apply chosen fixes to a COPY, and record every change.

    The original is never touched. That isn't caution for its own sake — a
    cleaning step you later decide was wrong is only recoverable if the thing
    it was applied to still exists.
    """
    import pandas as pd
    from . import modelbuild
    try:
        df = modelbuild._load(path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    before = {"rows": len(df), "columns": len(df.columns),
              "cells": int(df.size), "missing": int(df.isna().sum().sum())}
    diag = diagnose(path)
    if not diag.get("ok"):
        return diag

    chosen = fixes
    if chosen is None:
        # "safe" means reversible in meaning, not merely possible: dropping a
        # constant column loses nothing, filling a gap invents a value
        chosen = [i for i in diag["issues"]
                  if i["fix"] in ("drop_duplicates", "to_number", "to_date",
                                  "normalise_text")
                  or (i["fix"] == "drop_column" and
                      i["kind"] == "constant column")] \
            if apply_all_safe else []

    applied, skipped = [], []
    for issue in chosen:
        fix, col = issue.get("fix"), issue.get("column")
        try:
            if fix == "drop_duplicates":
                n0 = len(df)
                df = df.drop_duplicates()
                applied.append({"fix": fix, "removed_rows": n0 - len(df)})
            elif fix == "drop_column" and col in df.columns:
                df = df.drop(columns=[col])
                applied.append({"fix": fix, "column": col,
                                "why": issue["kind"]})
            elif fix == "to_number" and col in df.columns:
                raw = df[col].astype(str).str.replace(
                    r"[,\s]", "", regex=True).str.replace(
                    r"^[R$€£]", "", regex=True).str.rstrip("%")
                conv = pd.to_numeric(raw, errors="coerce")
                lost = int(conv.isna().sum() - df[col].isna().sum())
                df[col] = conv
                applied.append({"fix": fix, "column": col,
                                "became_missing": max(0, lost),
                                "note": ("values that wouldn't convert are "
                                         "now missing rather than wrong")})
            elif fix == "to_date" and col in df.columns:
                conv = pd.to_datetime(df[col], errors="coerce",
                                      format="mixed", dayfirst=True)
                lost = int(conv.isna().sum() - df[col].isna().sum())
                df[col] = conv
                applied.append({"fix": fix, "column": col,
                                "became_missing": max(0, lost),
                                "note": "read day-first (03/04 = 3 April)"})
            elif fix == "normalise_text" and col in df.columns:
                n0 = df[col].nunique()
                df[col] = (df[col].astype(str).str.strip()
                           .str.replace(r"\s+", " ", regex=True))
                applied.append({"fix": fix, "column": col,
                                "categories_before": int(n0),
                                "categories_after": int(df[col].nunique()),
                                "note": "case left alone — only whitespace "
                                        "was collapsed"})
            elif fix == "fill" and col in df.columns:
                if pd.api.types.is_numeric_dtype(df[col]):
                    val = df[col].median()
                    df[col + "_was_missing"] = df[col].isna().astype(int)
                    df[col] = df[col].fillna(val)
                    applied.append({
                        "fix": fix, "column": col, "filled_with": float(val),
                        "note": (f"a '{col}_was_missing' flag was added — "
                                 f"the fact it was absent is often itself "
                                 f"informative, and filling erases it")})
                else:
                    df[col] = df[col].fillna("(missing)")
                    applied.append({"fix": fix, "column": col,
                                    "filled_with": "(missing)",
                                    "note": "marked as missing rather than "
                                            "guessed"})
            elif fix == "flag_only" and col in df.columns:
                o = _outliers(df[col])
                df[col + "_extreme"] = (
                    (df[col] < o["low"]) | (df[col] > o["high"])).astype(int)
                applied.append({"fix": fix, "column": col,
                                "flagged": o["count"],
                                "note": "flagged, not removed"})
            else:
                skipped.append({"fix": fix, "column": col,
                                "why": "nothing to do"})
        except Exception as exc:
            skipped.append({"fix": fix, "column": col,
                            "why": f"{type(exc).__name__}: {exc}"})

    src = Path(path).expanduser()
    out = _dir() / (out_name or f"{src.stem}-clean.csv")
    df.to_csv(out, index=False)
    after = {"rows": len(df), "columns": len(df.columns),
             "cells": int(df.size), "missing": int(df.isna().sum().sum())}
    record = {
        "ok": True, "at": _iso(), "source": str(src), "output": str(out),
        "before": before, "after": after,
        "applied": applied, "skipped": skipped,
        "changed": {
            "rows_removed": before["rows"] - after["rows"],
            "columns_removed": max(0, before["columns"] - after["columns"]),
            "columns_added": max(0, after["columns"] - before["columns"]),
            "blanks_filled": max(0, before["missing"] - after["missing"]),
        },
        "note": (f"Written to a new file. {src.name} is untouched — a "
                 f"cleaning decision you later regret is only recoverable if "
                 f"the original still exists."),
    }
    (Path(str(out) + ".changes.json")).write_text(
        json.dumps(record, indent=2, default=str), "utf-8")
    return record


# --------------------------------------------------------------------------- #
#  3. features — proposed for a reason, kept only if they earn it
# --------------------------------------------------------------------------- #
def propose_features(path: str, target: str = "") -> dict:
    """Features with a reason attached.

    Not every column times every other column. That produces hundreds of
    features, a model that fits beautifully and predicts nothing, and no way
    to explain any of it."""
    import pandas as pd
    from . import modelbuild
    try:
        df = modelbuild._load(path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    ideas = []
    for c in df.columns:
        if c == target:
            continue
        s = df[c]
        if (pd.api.types.is_datetime64_any_dtype(s)
                or (not pd.api.types.is_numeric_dtype(s)
                    and _mostly_dates(s.dropna().astype(str).head(200)))):
            ideas.append({
                "kind": "date parts", "from": str(c),
                "makes": [f"{c}_year", f"{c}_month", f"{c}_weekday",
                          f"{c}_day_of_month"],
                "why": ("A date as a number is nearly useless to a model. "
                        "Its parts aren't: weekday carries the week's rhythm, "
                        "month carries the season.")})
            ideas.append({
                "kind": "age in days", "from": str(c),
                "makes": [f"{c}_days_ago"],
                "why": ("How long ago something happened is usually what "
                        "matters, not the calendar date it happened on.")})
        elif not pd.api.types.is_numeric_dtype(s):
            n = s.nunique(dropna=True)
            if n > MAX_CATEGORY_MERGE:
                ideas.append({
                    "kind": "group rare categories", "from": str(c),
                    "makes": [f"{c}_grouped"],
                    "why": (f"'{c}' has {n} distinct values. Ones that appear "
                            f"a handful of times can't be learned from and "
                            f"become noise; grouping them as 'other' keeps "
                            f"the common ones useful.")})

    nums = [c for c in df.columns
            if pd.api.types.is_numeric_dtype(df[c]) and c != target]
    # ratios only between plausibly related columns — every pair is noise
    for a in nums:
        for b in nums:
            if a >= b:
                continue
            if _related(a, b):
                ideas.append({
                    "kind": "ratio", "from": f"{a}, {b}",
                    "makes": [f"{a}_per_{b}"],
                    "why": (f"'{a}' and '{b}' look related, and a rate often "
                            f"carries what two totals don't — spend per month "
                            f"says more than spend and months separately.")})
    return {"ok": True, "ideas": ideas[:20],
            "note": ("Nothing is built yet. Building them is cheap; keeping "
                     "ones that don't help is not — each adds noise the model "
                     "has to see past."),
            "next": "build them and measure whether the score moves"}


_PAIRS = (("amount", "count"), ("total", "count"), ("spend", "month"),
          ("revenue", "unit"), ("value", "qty"), ("sum", "n"),
          ("tickets", "month"), ("score", "attempt"))


def _related(a: str, b: str) -> bool:
    x, y = a.lower(), b.lower()
    for p, q in _PAIRS:
        if (p in x and q in y) or (p in y and q in x):
            return True
    # same stem, different suffix: revenue_2024 / revenue_2025
    return (x.split("_")[0] == y.split("_")[0] and x != y)


def build_features(path: str, target: str, ideas: list = None,
                   out_name: str = "") -> dict:
    """Build the proposed features, then check they were worth building.

    This is the step most tools skip. Generating features is trivial;
    knowing whether they helped needs a before-and-after on held-out data,
    and without it you are adding noise on faith.
    """
    import pandas as pd
    import numpy as np
    from . import modelbuild
    try:
        df = modelbuild._load(path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if target not in df.columns:
        return {"ok": False, "error": f"There's no column called '{target}'."}

    if ideas is None:
        prop = propose_features(path, target)
        ideas = prop.get("ideas", [])
    if not ideas:
        return {"ok": False, "error": "No features worth building here."}

    made, failed = [], []
    for idea in ideas:
        kind = idea.get("kind")
        try:
            if kind == "date parts":
                c = idea["from"]
                d = pd.to_datetime(df[c], errors="coerce", format="mixed",
                                   dayfirst=True)
                df[f"{c}_year"] = d.dt.year
                df[f"{c}_month"] = d.dt.month
                df[f"{c}_weekday"] = d.dt.weekday
                df[f"{c}_day_of_month"] = d.dt.day
                made += idea["makes"]
            elif kind == "age in days":
                c = idea["from"]
                d = pd.to_datetime(df[c], errors="coerce", format="mixed",
                                   dayfirst=True)
                df[f"{c}_days_ago"] = (pd.Timestamp.now() - d).dt.days
                made += idea["makes"]
            elif kind == "group rare categories":
                c = idea["from"]
                counts = df[c].value_counts()
                common = set(counts[counts >= max(3, len(df) * 0.01)].index)
                df[f"{c}_grouped"] = df[c].where(df[c].isin(common), "other")
                made += idea["makes"]
            elif kind == "ratio":
                a, b = [x.strip() for x in idea["from"].split(",")]
                denom = df[b].replace(0, np.nan)
                df[f"{a}_per_{b}"] = df[a] / denom
                made += idea["makes"]
        except Exception as exc:
            failed.append({"idea": kind, "from": idea.get("from"),
                           "why": f"{type(exc).__name__}: {exc}"})

    src = Path(path).expanduser()
    out = _dir() / (out_name or f"{src.stem}-features.csv")
    df.to_csv(out, index=False)

    # the measurement: same target, same splits, with and without
    before = modelbuild.train(str(src), target, name="_fe_before")
    after = modelbuild.train(str(out), target, name="_fe_after")
    if not (before.get("ok") and after.get("ok")):
        return {"ok": True, "output": str(out), "made": made,
                "failed": failed,
                "warning": ("Built, but I couldn't measure whether they "
                            "helped — so treat them as unproven.")}

    lift = after["test_score"] - before["test_score"]
    helped = lift > 0.005
    return {
        "ok": True, "output": str(out), "made": made, "failed": failed,
        "before_score": before["test_score"],
        "after_score": after["test_score"],
        "lift": round(lift, 4),
        "helped": helped,
        "verdict": (
            f"{len(made)} feature(s) built. Held-out score went from "
            f"{before['test_score']} to {after['test_score']}"
            + (f" — a real gain of {lift:.3f}. Worth keeping."
               if helped else
               f", a change of {lift:+.3f}. That is not an improvement: the "
               f"new columns are adding noise the model has to see past. "
               f"Use the original file.")),
        "note": ("Measured on held-out data with the same target, so this "
                 "comparison means something. Generating features is easy; "
                 "knowing whether they helped is the part that isn't."),
    }


def auto_prep(path: str, target: str = "") -> dict:
    """Diagnose, clean the safe things, propose and measure features.

    One call — with every change counted and the measurement kept."""
    d = diagnose(path)
    if not d.get("ok"):
        return d
    steps = [d["summary"]]
    cleaned = clean(path, apply_all_safe=True)
    if cleaned.get("ok"):
        ch = cleaned["changed"]
        steps.append(
            f"Cleaned to a copy: {ch['rows_removed']} row(s) removed, "
            f"{ch['columns_removed']} column(s) dropped, "
            f"{ch['columns_added']} added. The original is untouched.")
    work = cleaned.get("output", path)
    out = {"ok": True, "diagnosis": d, "cleaning": cleaned, "steps": steps}
    if not target:
        out["note"] = ("Say what you want to predict and I'll build features "
                       "and measure whether they help.")
        return out
    feats = build_features(work, target)
    out["features"] = feats
    if feats.get("ok"):
        steps.append(feats.get("verdict", ""))
    out["steps"] = steps
    return out


# =========================================================================== #
#  Per-column work — because "clean it" is too blunt a request
#
#  Automatic cleaning applies a policy. Real data needs decisions: this column
#  is a category not a number, that one should be dropped, these rows are test
#  data that got mixed in. One action at a time, each recorded, always to a
#  copy.
# =========================================================================== #
COLUMN_ACTIONS = ("drop", "rename", "to_number", "to_date", "to_category",
                  "fill_median", "fill_value", "trim", "lower", "upper",
                  "clip_outliers", "flag_outliers", "bin", "log")


def columns(path: str) -> dict:
    """Every column with what you could do to it and what it would cost."""
    import pandas as pd
    from . import modelbuild
    try:
        df = modelbuild._load(path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    out = []
    for c in df.columns:
        s = df[c]
        numeric = pd.api.types.is_numeric_dtype(s)
        miss = int(s.isna().sum())
        can = ["drop", "rename"]
        if numeric:
            can += ["fill_median", "flag_outliers", "clip_outliers", "bin",
                    "log", "to_category"]
        else:
            can += ["trim", "lower", "upper", "fill_value", "to_category"]
            if _mostly_numeric(s.dropna().astype(str).head(300)):
                can.insert(2, "to_number")
            if _mostly_dates(s.dropna().astype(str).head(300)):
                can.insert(2, "to_date")
        out.append({
            "name": str(c),
            "kind": "number" if numeric else (
                "date" if pd.api.types.is_datetime64_any_dtype(s) else "text"),
            "distinct": int(s.nunique(dropna=True)),
            "missing": miss,
            "missing_pct": round(miss / max(1, len(df)) * 100, 1),
            "example": _example_of(s),
            "stats": (_num_stats(s) if numeric else _text_stats(s)),
            "can": can,
        })
    return {"ok": True, "rows": len(df), "columns": out,
            "note": "Nothing changes until you ask for an action."}


def _example_of(s):
    try:
        return str(s.dropna().iloc[0])[:50]
    except Exception:
        return ""


def _num_stats(s) -> dict:
    try:
        return {"min": round(float(s.min()), 4),
                "median": round(float(s.median()), 4),
                "max": round(float(s.max()), 4),
                "extremes": _outliers(s)["count"]}
    except Exception:
        return {}


def _text_stats(s) -> dict:
    try:
        vc = s.value_counts().head(4)
        return {"most_common": {str(k): int(v) for k, v in vc.items()}}
    except Exception:
        return {}


def apply_actions(path: str, actions: list, out_name: str = "") -> dict:
    """Do exactly what was asked, to a copy, and say what changed.

    Each action reports its own effect. "Cleaned successfully" tells you
    nothing; "converted, 14 values wouldn't parse and are now missing" tells
    you whether to look."""
    import pandas as pd
    import numpy as np
    from . import modelbuild
    try:
        df = modelbuild._load(path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    done, failed = [], []
    for a in actions or []:
        act, col = a.get("action"), a.get("column")
        try:
            if act not in COLUMN_ACTIONS:
                failed.append({**a, "why": f"'{act}' isn't an action I know"})
                continue
            if col not in df.columns and act != "rename":
                failed.append({**a, "why": f"no column '{col}'"})
                continue
            if act == "drop":
                df = df.drop(columns=[col])
                done.append({**a, "effect": "column removed"})
            elif act == "rename":
                new = a.get("to") or ""
                if col not in df.columns or not new:
                    failed.append({**a, "why": "needs a column and a new name"})
                    continue
                df = df.rename(columns={col: new})
                done.append({**a, "effect": f"renamed to {new}"})
            elif act == "to_number":
                raw = (df[col].astype(str).str.replace(r"[,\s]", "", regex=True)
                       .str.replace(r"^[R$€£]", "", regex=True).str.rstrip("%"))
                conv = pd.to_numeric(raw, errors="coerce")
                lost = int(conv.isna().sum() - df[col].isna().sum())
                df[col] = conv
                done.append({**a, "effect": f"converted; {max(0, lost)} "
                                            f"value(s) wouldn't parse and are "
                                            f"now missing"})
            elif act == "to_date":
                conv = pd.to_datetime(df[col], errors="coerce",
                                      format="mixed", dayfirst=True)
                lost = int(conv.isna().sum() - df[col].isna().sum())
                df[col] = conv
                done.append({**a, "effect": f"read day-first; "
                                            f"{max(0, lost)} wouldn't parse"})
            elif act == "to_category":
                df[col] = df[col].astype(str)
                done.append({**a, "effect": "treated as a category"})
            elif act == "fill_median":
                v = df[col].median()
                df[col + "_was_missing"] = df[col].isna().astype(int)
                n = int(df[col].isna().sum())
                df[col] = df[col].fillna(v)
                done.append({**a, "effect": f"{n} filled with {v:.4g}; a "
                                            f"'{col}_was_missing' flag kept "
                                            f"the fact they were absent"})
            elif act == "fill_value":
                v = a.get("value", "(missing)")
                n = int(df[col].isna().sum())
                df[col] = df[col].fillna(v)
                done.append({**a, "effect": f"{n} filled with {v!r}"})
            elif act == "trim":
                df[col] = (df[col].astype(str).str.strip()
                           .str.replace(r"\s+", " ", regex=True))
                done.append({**a, "effect": "whitespace collapsed"})
            elif act in ("lower", "upper"):
                before = int(df[col].nunique())
                df[col] = (df[col].astype(str).str.lower() if act == "lower"
                           else df[col].astype(str).str.upper())
                done.append({**a, "effect": f"{before} distinct values became "
                                            f"{int(df[col].nunique())}"})
            elif act == "flag_outliers":
                o = _outliers(df[col])
                df[col + "_extreme"] = (
                    (df[col] < o["low"]) | (df[col] > o["high"])).astype(int)
                done.append({**a, "effect": f"{o['count']} flagged, none "
                                            f"removed"})
            elif act == "clip_outliers":
                o = _outliers(df[col])
                n = o["count"]
                df[col] = df[col].clip(o["low"], o["high"])
                done.append({**a, "effect": (
                    f"{n} value(s) pulled to the edge of the normal range "
                    f"rather than deleted — the row survives, the extreme "
                    f"value doesn't distort the fit")})
            elif act == "bin":
                q = int(a.get("bins", 4))
                df[col + "_band"] = pd.qcut(df[col], q, duplicates="drop",
                                            labels=False)
                done.append({**a, "effect": f"split into {q} bands as "
                                            f"'{col}_band'"})
            elif act == "log":
                if (df[col] <= 0).any():
                    df[col + "_log"] = np.log1p(df[col].clip(lower=0))
                    done.append({**a, "effect": "log1p used — the column has "
                                                "zeros or negatives"})
                else:
                    df[col + "_log"] = np.log(df[col])
                    done.append({**a, "effect": f"'{col}_log' added"})
        except Exception as exc:
            failed.append({**a, "why": f"{type(exc).__name__}: {exc}"})

    src = Path(path).expanduser()
    out = _dir() / (out_name or f"{src.stem}-edited.csv")
    df.to_csv(out, index=False)
    return {"ok": True, "output": str(out), "applied": done, "failed": failed,
            "rows": len(df), "columns": len(df.columns),
            "note": f"Written to a new file. {src.name} is untouched."}


def feature_one(path: str, target: str, idea: dict) -> dict:
    """Build ONE feature and measure whether it helped.

    A batch tells you the set helped. One at a time tells you which — and
    that's the difference between knowing your data and having a bigger file.
    """
    from . import modelbuild
    before = modelbuild.train(path, target, name="_one_before")
    built = build_features(path, target, ideas=[idea],
                           out_name=f"_one_{_slug_feature(idea)}.csv")
    if not built.get("ok"):
        return built
    after = modelbuild.train(built["output"], target, name="_one_after")
    if not (before.get("ok") and after.get("ok")):
        return {**built, "warning": "built, but couldn't measure it"}
    lift = after["test_score"] - before["test_score"]
    return {"ok": True, "made": built["made"], "output": built["output"],
            "before": before["test_score"], "after": after["test_score"],
            "lift": round(lift, 4), "helped": lift > 0.005,
            "verdict": (f"{', '.join(built['made'])}: "
                        f"{before['test_score']} → {after['test_score']} "
                        + ("— worth keeping." if lift > 0.005 else
                           "— no real gain, so it's noise. Leave it out."))}


def _slug_feature(idea: dict) -> str:
    return re.sub(r"[^a-z0-9]+", "-",
                  f"{idea.get('kind','')}-{idea.get('from','')}".lower())[:30]
