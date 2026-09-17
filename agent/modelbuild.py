"""Building a predictive model from a table — and knowing whether to trust it.

Training a model is the easy part; four lines of scikit-learn does it. What
makes the number at the end mean anything is everything around it, and that is
what this module is mostly made of.

**A baseline, always.** 94% accuracy sounds excellent and is worthless on a
dataset that is 94% one class — a model that always says "no" scores the same.
Every result here is reported next to the dumbest possible predictor, and if
the model doesn't beat it, that is the headline rather than a footnote.

**The test set is touched once.** Train on one part, tune on a second, and
report on a third that nothing has seen. Tune against the test set and its
number stops being a prediction of anything; it becomes a description of that
particular sample. So it is held back and used at the end, once.

**Leakage is looked for.** In real business data the commonest way to get a
99% model is a column that already contains the answer — `closed_date` in a
churn table, `invoice_paid` in a collections table. It looks like brilliance
and it is a bug. Any single feature that nearly predicts the target on its own
is flagged before you celebrate.

**Small data says so.** Two hundred rows with forty features will fit
beautifully and predict nothing. That gets said plainly rather than buried in
a confidence interval.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config

MIN_ROWS = 50
LEAK_THRESHOLD = 0.97          # a single feature this predictive is suspicious


def _dir() -> Path:
    d = config.AGENT_HOME / "models_tabular"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40]


def _load(path: str):
    import pandas as pd
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"{p} isn't there")
    if p.suffix.lower() in (".csv", ".txt"):
        return pd.read_csv(p)
    if p.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(p)
    if p.suffix.lower() == ".parquet":
        return pd.read_parquet(p)
    if p.suffix.lower() in (".json", ".jsonl"):
        return pd.read_json(p, lines=p.suffix.lower() == ".jsonl")
    raise ValueError(f"I can read CSV, Excel, Parquet and JSON — not "
                     f"{p.suffix}")


# --------------------------------------------------------------------------- #
#  1. look at the data before modelling it
# --------------------------------------------------------------------------- #
def profile(path: str, target: str = "") -> dict:
    """What's in the table, and what could be predicted from it."""
    import pandas as pd
    try:
        df = _load(path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    cols = []
    for c in df.columns:
        s = df[c]
        nunique = int(s.nunique(dropna=True))
        missing = float(s.isna().mean())
        kind = ("number" if pd.api.types.is_numeric_dtype(s)
                else "date" if pd.api.types.is_datetime64_any_dtype(s)
                else "text")
        cols.append({
            "name": str(c), "kind": kind, "distinct": nunique,
            "missing_pct": round(missing * 100, 1),
            "example": _example(s),
            # a column with one value teaches nothing; one with a different
            # value in every row is an identifier, not a feature
            "useless": ("only one value" if nunique <= 1 else
                        "looks like an id — a different value in every row"
                        if nunique == len(df) and kind != "number" else ""),
        })

    out = {"ok": True, "path": str(Path(path).expanduser()),
           "rows": int(len(df)), "columns": len(df.columns),
           "fields": cols, "at": _iso()}

    if len(df) < MIN_ROWS:
        out["warning"] = (
            f"{len(df)} rows is too few to learn anything that will hold up. "
            f"Below about {MIN_ROWS} the model memorises rather than "
            f"generalises, and the test score will flatter it.")

    if target:
        if target not in df.columns:
            out["error"] = f"There's no column called '{target}'."
            out["ok"] = False
            return out
        out["target"] = _target_summary(df, target)
    else:
        out["suggested_targets"] = _suggest_targets(df, cols)
    return out


def _example(s):
    try:
        v = s.dropna().iloc[0]
        return str(v)[:40]
    except Exception:
        return ""


def _target_summary(df, target: str) -> dict:
    import pandas as pd
    s = df[target]
    if pd.api.types.is_numeric_dtype(s) and s.nunique() > 12:
        return {"name": target, "task": "regression",
                "why": "a number with many values, so predict the value",
                "mean": round(float(s.mean()), 4),
                "spread": round(float(s.std()), 4),
                "missing": int(s.isna().sum())}
    counts = s.value_counts(dropna=True)
    top = counts.index[0] if len(counts) else None
    share = float(counts.iloc[0] / counts.sum()) if len(counts) else 0
    return {
        "name": target, "task": "classification",
        "why": f"{s.nunique()} distinct values, so predict which one",
        "classes": {str(k): int(v) for k, v in counts.head(12).items()},
        "biggest_class": str(top), "biggest_share": round(share * 100, 1),
        "balance_note": (
            f"'{top}' is {share * 100:.0f}% of the rows. Always guessing it "
            f"scores {share * 100:.0f}% — any model has to beat that to be "
            f"worth anything." if share >= 0.6 else ""),
        "missing": int(s.isna().sum()),
    }


def _suggest_targets(df, cols: list) -> list:
    out = []
    for c in cols:
        if c["useless"] or c["missing_pct"] > 40:
            continue
        name = c["name"].lower()
        score = 0
        if any(w in name for w in ("target", "label", "outcome", "result",
                                   "churn", "default", "fraud", "converted",
                                   "won", "status", "class")):
            score += 3
        if c["kind"] == "number" and c["distinct"] > 12:
            score += 1
        if 2 <= c["distinct"] <= 10:
            score += 2
        if score:
            out.append({"column": c["name"], "distinct": c["distinct"],
                        "why": ("the name suggests an outcome" if score >= 3
                                else "few distinct values, so a likely label"
                                if c["distinct"] <= 10
                                else "a number worth predicting")})
    return sorted(out, key=lambda x: -x["distinct"])[:6]


# --------------------------------------------------------------------------- #
#  2. leakage — the commonest reason a model looks brilliant
# --------------------------------------------------------------------------- #
def find_leaks(path: str, target: str) -> dict:
    """Columns that already contain the answer.

    In real business data this is the usual explanation for a 99% model:
    `closed_date` in a churn table, `invoice_paid` in collections. It looks
    like brilliance and it is a bug — the column won't exist at the moment
    you actually need a prediction."""
    import pandas as pd
    from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor
    from sklearn.model_selection import cross_val_score
    try:
        df = _load(path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if target not in df.columns:
        return {"ok": False, "error": f"no column '{target}'"}

    df = df.dropna(subset=[target])
    y = df[target]
    classification = not (pd.api.types.is_numeric_dtype(y)
                          and y.nunique() > 12)
    suspects, unchecked = [], []
    for c in df.columns:
        if c == target:
            continue
        # `dtype == object` was a pandas 2 idiom; in pandas 3 a text column
        # is `str`, so the conversion never fired, sklearn got raw strings,
        # and the failure was swallowed — reporting "nothing looks like
        # leakage" having checked nothing. Ask what it IS, not what it isn't.
        s = df[c]
        if pd.api.types.is_numeric_dtype(s):
            x = s.fillna(-999999).to_frame()
        else:
            x = s.astype("category").cat.codes.to_frame()
        try:
            model = (DecisionTreeClassifier(max_depth=3, random_state=0)
                     if classification
                     else DecisionTreeRegressor(max_depth=3, random_state=0))
            score = float(cross_val_score(model, x, y, cv=3).mean())
        except Exception as exc:
            # a column that couldn't be checked is not a column that's clean
            unchecked.append({"column": str(c),
                              "why": f"{type(exc).__name__}"})
            continue
        if score >= LEAK_THRESHOLD:
            suspects.append({
                "column": str(c), "alone_predicts": round(score * 100, 1),
                "why": (f"'{c}' predicts '{target}' {score * 100:.0f}% of the "
                        f"time on its own. Either it is a restatement of the "
                        f"answer, or it is recorded after the fact — in which "
                        f"case it won't be available when you need a "
                        f"prediction.")})
    return {"ok": True, "suspects": suspects, "unchecked": unchecked,
            "verdict": ("Nothing looks like leakage."
                        + (f" ({len(unchecked)} column(s) couldn't be "
                           f"checked — that isn't the same as clean.)"
                           if unchecked else "")
                        if not suspects else
                        f"{len(suspects)} column(s) may already contain the "
                        f"answer. Drop them and retrain — a model that scores "
                        f"worse without them is telling you the truth."),
            "note": ("This finds the obvious cases. A combination of columns "
                     "can leak without any single one showing up.")}


# --------------------------------------------------------------------------- #
#  3. train, validate, and test once
# --------------------------------------------------------------------------- #
def train(path: str, target: str, name: str = "", drop: list = None,
          test_share: float = 0.2, seed: int = 42) -> dict:
    """Fit several candidates, pick on validation, report on test.

    Three splits, not two. Choosing a model on the test set makes its score a
    description of that sample rather than a prediction about new data."""
    import pandas as pd
    import numpy as np
    from sklearn.model_selection import train_test_split
    from sklearn.pipeline import Pipeline
    from sklearn.compose import ColumnTransformer
    from sklearn.preprocessing import OneHotEncoder, StandardScaler
    from sklearn.impute import SimpleImputer
    from sklearn.dummy import DummyClassifier, DummyRegressor
    from sklearn.linear_model import LogisticRegression, Ridge
    from sklearn.ensemble import (RandomForestClassifier,
                                  RandomForestRegressor,
                                  HistGradientBoostingClassifier,
                                  HistGradientBoostingRegressor)
    from sklearn import metrics
    import joblib

    try:
        df = _load(path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if target not in df.columns:
        return {"ok": False, "error": f"There's no column called '{target}'."}

    df = df.dropna(subset=[target])
    if len(df) < MIN_ROWS:
        return {"ok": False,
                "error": (f"Only {len(df)} usable rows. Below about "
                          f"{MIN_ROWS} a model memorises rather than learns, "
                          f"and any score would be misleading.")}

    for c in (drop or []):
        if c in df.columns:
            df = df.drop(columns=[c])

    y = df[target]
    X = df.drop(columns=[target])
    # identifiers teach nothing and make the model look good on training data
    # `dtype == object` is a pandas 2 idiom — in pandas 3 a text column is
    # `str`, so this silently matched nothing and every id column stayed in.
    # Ask what a column IS rather than what it isn't.
    dropped_auto = [c for c in X.columns
                    if X[c].nunique() == len(X)
                    and not pd.api.types.is_numeric_dtype(X[c])]
    X = X.drop(columns=dropped_auto)
    if X.empty:
        return {"ok": False,
                "error": "Nothing left to learn from once ids and the target "
                         "are removed."}

    classification = not (pd.api.types.is_numeric_dtype(y)
                          and y.nunique() > 12)
    num = [c for c in X.columns if pd.api.types.is_numeric_dtype(X[c])]
    cat = [c for c in X.columns if c not in num]

    pre = ColumnTransformer([
        ("num", Pipeline([("fill", SimpleImputer(strategy="median",
                                                 add_indicator=True)),
                          ("scale", StandardScaler())]), num),
        # Missing is marked, not guessed. Filling a text column with its
        # commonest value destroyed a column that was blank for one class and
        # filled for the other — the missingness WAS the signal, and the
        # pipeline quietly made it constant. That is a common way to train a
        # model that learns nothing and nobody notices.
        ("cat", Pipeline([("fill", SimpleImputer(strategy="constant",
                                                 fill_value="(missing)")),
                          ("hot", OneHotEncoder(handle_unknown="ignore",
                                                max_categories=40))]), cat),
    ], remainder="drop")

    strat = y if classification and y.value_counts().min() >= 2 else None
    X_rest, X_test, y_rest, y_test = train_test_split(
        X, y, test_size=test_share, random_state=seed, stratify=strat)
    strat2 = y_rest if strat is not None and \
        y_rest.value_counts().min() >= 2 else None
    X_tr, X_val, y_tr, y_val = train_test_split(
        X_rest, y_rest, test_size=0.25, random_state=seed, stratify=strat2)

    if classification:
        candidates = {
            "always the commonest": DummyClassifier(strategy="most_frequent"),
            "logistic regression": LogisticRegression(max_iter=1000),
            "random forest": RandomForestClassifier(n_estimators=200,
                                                    random_state=seed),
            "gradient boosting": HistGradientBoostingClassifier(
                random_state=seed),
        }
        scorer = lambda yt, yp: float(metrics.accuracy_score(yt, yp))
        score_name = "accuracy"
    else:
        candidates = {
            "always the average": DummyRegressor(strategy="mean"),
            "ridge regression": Ridge(),
            "random forest": RandomForestRegressor(n_estimators=200,
                                                   random_state=seed),
            "gradient boosting": HistGradientBoostingRegressor(
                random_state=seed),
        }
        scorer = lambda yt, yp: float(metrics.r2_score(yt, yp))
        score_name = "r²"

    # Cross-validation, not one split. On a few hundred rows a single
    # validation score moves several points between random seeds — so a 3%
    # gap between two models can be nothing at all. Folding gives a spread,
    # and the spread is what tells you whether a difference is real.
    from sklearn.model_selection import cross_val_score, StratifiedKFold, KFold
    import numpy as _np
    n_folds = 5 if len(X_rest) >= 200 else 3
    if classification and y_rest.value_counts().min() >= n_folds:
        cv = StratifiedKFold(n_splits=n_folds, shuffle=True,
                             random_state=seed)
    else:
        cv = KFold(n_splits=n_folds, shuffle=True, random_state=seed)

    results, fitted = [], {}
    for label, est in candidates.items():
        pipe = Pipeline([("pre", pre), ("model", est)])
        try:
            # sklearn warns to the console when folds fail and returns NaN,
            # which is a silent failure wearing a warning. Catch it and say
            # so in the result, where someone will actually read it.
            import warnings as _w
            with _w.catch_warnings():
                _w.simplefilter("ignore")
                folds = cross_val_score(pipe, X_rest, y_rest, cv=cv,
                                        scoring=("accuracy" if classification
                                                 else "r2"),
                                        error_score=_np.nan)
            if _np.isnan(folds).any():
                bad = int(_np.isnan(folds).sum())
                results.append({
                    "model": label,
                    "error": (f"{bad} of {n_folds} folds couldn't be fitted "
                              f"— usually too few rows, or a class that "
                              f"doesn't appear in every fold.")})
                continue
            mean, spread = float(_np.mean(folds)), float(_np.std(folds))
            # fit on everything but the test set for the final model
            pipe.fit(X_rest, y_rest)
        except Exception as exc:
            results.append({"model": label, "error": str(exc)[:120]})
            continue
        fitted[label] = pipe
        results.append({"model": label, "validation": round(mean, 4),
                        "spread": round(spread, 4),
                        "folds": [round(float(f), 4) for f in folds],
                        "reading": (f"{mean:.3f} give or take {spread:.3f} "
                                    f"across {n_folds} folds")})

    real = [r for r in results
            if "validation" in r and not r["model"].startswith("always")]
    baseline = next((r for r in results if r["model"].startswith("always")),
                    {"validation": 0.0})
    if not real:
        return {"ok": False, "error": "Nothing trained successfully.",
                "results": results}

    best = max(real, key=lambda r: r["validation"])
    pipe = fitted[best["model"]]
    # is the winner actually ahead, or inside the noise?
    others = [r for r in real if r["model"] != best["model"]]
    runner_up = max(others, key=lambda r: r["validation"]) if others else None
    margin = (best["validation"] - runner_up["validation"]
              if runner_up else None)
    noise = max(best.get("spread", 0),
                (runner_up or {}).get("spread", 0)) or 0
    tie = bool(runner_up and margin is not None and margin < noise)

    # the test set, once, at the end
    y_pred = pipe.predict(X_test)
    test_score = scorer(y_test, y_pred)
    base_test = scorer(y_test, fitted[baseline_name(results)].predict(X_test)) \
        if baseline_name(results) in fitted else 0.0

    detail = {}
    if classification:
        try:
            detail["per_class"] = metrics.classification_report(
                y_test, y_pred, output_dict=True, zero_division=0)
            detail["confusion"] = metrics.confusion_matrix(
                y_test, y_pred).tolist()
            detail["labels"] = [str(c) for c in sorted(set(y_test))]
        except Exception:
            pass
    else:
        detail["mean_error"] = round(float(
            metrics.mean_absolute_error(y_test, y_pred)), 4)
        detail["spread_of_target"] = round(float(np.std(y_test)), 4)

    lift = test_score - base_test
    beats = lift > 0.01
    slug = _slug(name or f"{Path(path).stem}-{target}")
    out_dir = _dir() / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipe, out_dir / "model.joblib")
    card = {
        "ok": True, "name": slug, "at": _iso(),
        "data": str(Path(path).expanduser()), "target": target,
        "task": "classification" if classification else "regression",
        "rows": {"train": len(X_tr), "validation": len(X_val),
                 "test": len(X_test)},
        "features": {"numeric": num, "categorical": cat,
                     "dropped_as_ids": dropped_auto,
                     "dropped_by_you": drop or []},
        "chose": best["model"], "score_name": score_name,
        "validation_scores": results,
        "choice_was_close": tie,
        "choice_note": (
            (f"{best['model']} came top at {best['validation']:.3f}, but "
             f"{runner_up['model']} scored {runner_up['validation']:.3f} and "
             f"the fold-to-fold spread is {noise:.3f}. That gap is inside the "
             f"noise — treat them as equal and prefer whichever is simpler to "
             f"explain.")
            if tie else
            (f"{best['model']} was ahead by {margin:.3f}, wider than the "
             f"{noise:.3f} spread between folds, so the choice is real."
             if runner_up else
             "Only one candidate trained successfully.")),
        "folds": n_folds,
        "test_score": round(test_score, 4),
        "baseline_test_score": round(base_test, 4),
        "beats_baseline": beats,
        "detail": detail,
        "verdict": _verdict(best["model"], score_name, test_score, base_test,
                            beats, classification),
        "honest": [
            "The test rows were never used for training or for choosing the "
            "model, so this score is a fair estimate — once. Tune anything "
            "and re-test, and it stops being one.",
            f"Chosen by {n_folds}-fold cross-validation on "
            f"{len(X_rest)} rows, so the choice doesn't rest on one lucky "
            f"split. The final model is fitted on all of them.",
        ],
    }
    (out_dir / "model-card.json").write_text(
        json.dumps(card, indent=2, default=str), "utf-8")
    return card


def baseline_name(results: list) -> str:
    for r in results:
        if r["model"].startswith("always"):
            return r["model"]
    return ""


def _verdict(model: str, score_name: str, test: float, base: float,
             beats: bool, classification: bool) -> str:
    pct = (lambda v: f"{v * 100:.1f}%") if classification else (
        lambda v: f"{v:.3f}")
    if not beats:
        return (f"{model} scored {pct(test)} on the held-out data — the "
                f"do-nothing baseline scores {pct(base)}. It has not learned "
                f"anything useful. Either the features don't carry the "
                f"signal, or there isn't one to find.")
    return (f"{model}: {score_name} {pct(test)} on data it had never seen, "
            f"against {pct(base)} for always guessing. That is a real gain of "
            f"{pct(test - base)} — worth keeping, and worth checking against "
            f"a few rows you know the answer to.")


# --------------------------------------------------------------------------- #
#  4. inference — using it on data it has never seen
# --------------------------------------------------------------------------- #
def predict(name: str, rows, explain: bool = True) -> dict:
    """Run the saved model on new rows.

    Returns the confidence alongside the prediction, because a model that is
    51% sure and one that is 99% sure look identical otherwise, and the
    difference is usually what you'd act on."""
    import pandas as pd
    import joblib
    d = _dir() / _slug(name)
    p = d / "model.joblib"
    if not p.exists():
        return {"ok": False,
                "error": f"No model called '{name}'. Train one first."}
    try:
        card = json.loads((d / "model-card.json").read_text("utf-8"))
    except Exception:
        card = {}
    pipe = joblib.load(p)

    if isinstance(rows, str):
        try:
            df = _load(rows)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    elif isinstance(rows, dict):
        df = pd.DataFrame([rows])
    else:
        df = pd.DataFrame(list(rows))

    target = card.get("target", "")
    truth = df[target] if target and target in df.columns else None
    X = df.drop(columns=[target]) if truth is not None else df

    # a column the model was trained on and isn't here is a real problem;
    # silently filling it with nothing produces a confident wrong answer
    expected = set((card.get("features") or {}).get("numeric", [])) | \
        set((card.get("features") or {}).get("categorical", []))
    missing = sorted(expected - set(X.columns))
    if missing:
        return {"ok": False,
                "error": (f"These columns are missing: {', '.join(missing)}. "
                          f"The model needs them — guessing them would give "
                          f"you a confident answer built on nothing.")}

    try:
        preds = pipe.predict(X)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    out = []
    proba = None
    if explain and hasattr(pipe, "predict_proba"):
        try:
            proba = pipe.predict_proba(X)
            classes = list(pipe.classes_)
        except Exception:
            proba = None
    for i, pred in enumerate(preds):
        row = {"prediction": _plain(pred)}
        if proba is not None:
            conf = float(max(proba[i]))
            row["confidence"] = round(conf * 100, 1)
            row["reading"] = (
                "confident" if conf >= 0.8 else
                "leaning that way" if conf >= 0.6 else
                "barely more than a coin toss — treat as unknown")
            row["alternatives"] = {
                str(classes[j]): round(float(proba[i][j]) * 100, 1)
                for j in sorted(range(len(classes)),
                                key=lambda j: -proba[i][j])[:3]}
        if truth is not None:
            row["actual"] = _plain(truth.iloc[i])
            row["right"] = bool(row["prediction"] == row["actual"])
        out.append(row)

    result = {"ok": True, "model": _slug(name), "predictions": out[:200],
              "count": len(out)}
    if truth is not None:
        right = sum(1 for r in out if r.get("right"))
        result["checked"] = {
            "right": right, "of": len(out),
            "rate": round(right / len(out) * 100, 1) if out else 0,
            "note": ("You supplied the answers, so this is a real check — "
                     "but on rows the model may have trained on, if this is "
                     "the same file. Use data from after it was trained.")}
    return result


def _plain(v):
    try:
        import numpy as np
        if isinstance(v, (np.integer,)):
            return int(v)
        if isinstance(v, (np.floating,)):
            return round(float(v), 4)
        if isinstance(v, (np.bool_,)):
            return bool(v)
    except Exception:
        pass
    return v if isinstance(v, (int, float, bool, str)) else str(v)


def saved() -> list:
    """Models built here, with what each was measured at."""
    out = []
    for d in sorted(_dir().glob("*")):
        if not (d / "model.joblib").exists():
            continue
        try:
            card = json.loads((d / "model-card.json").read_text("utf-8"))
        except Exception:
            card = {}
        out.append({
            "name": d.name, "target": card.get("target", ""),
            "task": card.get("task", ""), "chose": card.get("chose", ""),
            "test_score": card.get("test_score"),
            "baseline": card.get("baseline_test_score"),
            "beats_baseline": card.get("beats_baseline"),
            "at": card.get("at", ""),
        })
    return out


def card(name: str) -> dict:
    p = _dir() / _slug(name) / "model-card.json"
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return {"ok": False, "error": f"No model called '{name}'."}


# =========================================================================== #
#  Any data, and the right tool for it
#
#  The request was "make it state of the art and use CUDA". The honest answer
#  has a part people don't expect:
#
#  **Deep learning is the wrong tool for most business tables.** On typical
#  tabular data — a few thousand rows, mixed numeric and categorical —
#  gradient boosting beats neural networks, trains in seconds on a CPU, and
#  needs no tuning. Putting a 600-row churn table on a 3090 would be slower
#  AND worse. Saying so is worth more than a GPU switch that flatters the
#  hardware.
#
#  Where the GPU genuinely earns its place is text and images, and even there
#  the state of the art is usually NOT training a network from scratch: a
#  pretrained model turns your data into embeddings, and a simple classifier
#  on top of those wins on small datasets and trains in under a minute.
#  Fine-tuning the whole network only pays once you have thousands of
#  examples.
#
#  So: work out what the data is, pick what actually wins for that shape and
#  size, use the card where it helps, and say plainly when it wouldn't.
# =========================================================================== #
def hardware() -> dict:
    """What's available, and what it's worth using."""
    info = {"cuda": False, "device": "cpu", "name": "", "vram_gb": 0.0,
            "torch": False}
    try:
        import torch
        info["torch"] = True
        info["torch_version"] = torch.__version__
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            info.update({"cuda": True, "device": "cuda",
                         "name": props.name,
                         "vram_gb": round(props.total_memory / 1e9, 1),
                         "count": torch.cuda.device_count()})
    except ImportError:
        info["note"] = ("PyTorch isn't installed, so text and image models "
                        "would run on the CPU or not at all. "
                        "`pip install torch --index-url "
                        "https://download.pytorch.org/whl/cu121` for CUDA.")
    return info


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}


def detect(path: str) -> dict:
    """What kind of problem is this, before deciding how to solve it."""
    import pandas as pd
    p = Path(path).expanduser()
    if not p.exists():
        return {"ok": False, "error": f"{p} isn't there"}

    if p.is_dir():
        # a folder of folders of images is the usual shape for image work
        subs = [d for d in p.iterdir() if d.is_dir()]
        imgs = [f for f in p.rglob("*") if f.suffix.lower() in IMAGE_EXTS]
        if imgs and subs:
            per = {d.name: len([f for f in d.rglob("*")
                                if f.suffix.lower() in IMAGE_EXTS])
                   for d in subs}
            per = {k: v for k, v in per.items() if v}
            return {"ok": True, "kind": "images", "classes": per,
                    "total": sum(per.values()),
                    "why": (f"{len(per)} folder(s) of images — each folder "
                            f"name is a label")}
        if imgs:
            return {"ok": False, "kind": "images",
                    "error": ("Images, but all in one folder. Put each class "
                              "in its own sub-folder so there's something to "
                              "learn.")}
        return {"ok": False, "error": "A folder with nothing I can model in it."}

    try:
        df = _load(str(p))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    # a long free-text column is a different problem from a table of numbers
    text_cols = []
    for c in df.columns:
        s = df[c]
        if pd.api.types.is_numeric_dtype(s):
            continue
        try:
            avg = s.dropna().astype(str).str.len().mean()
            words = s.dropna().astype(str).str.split().str.len().mean()
        except Exception:
            continue
        if avg and avg > 60 and words and words > 8:
            text_cols.append({"column": str(c), "avg_chars": round(avg),
                              "avg_words": round(words)})

    date_cols = [str(c) for c in df.columns
                 if pd.api.types.is_datetime64_any_dtype(df[c])
                 or re.search(r"date|time|month|day|period", str(c), re.I)]

    if text_cols:
        return {"ok": True, "kind": "text", "rows": len(df),
                "text_columns": text_cols,
                "why": (f"'{text_cols[0]['column']}' holds free text "
                        f"(~{text_cols[0]['avg_words']} words a row), so the "
                        f"meaning is in the writing, not in columns")}
    if date_cols and len(df.columns) <= 4:
        return {"ok": True, "kind": "timeseries", "rows": len(df),
                "date_columns": date_cols,
                "why": "a date column and little else — this looks like a "
                       "series over time"}
    return {"ok": True, "kind": "tabular", "rows": len(df),
            "columns": len(df.columns),
            "why": "columns of numbers and categories"}


def plan(path: str, target: str = "") -> dict:
    """What to use, and whether the GPU would help.

    The part worth reading is where it says the card won't help."""
    d = detect(path)
    if not d.get("ok"):
        return d
    hw = hardware()
    kind = d["kind"]

    if kind == "tabular":
        rows = d.get("rows", 0)
        return {**d, "hardware": hw, "approach": "gradient boosting",
                "use_gpu": False,
                "why_this": (
                    "Gradient boosting is the state of the art for tabular "
                    "data and has been for years. Neural networks lose to it "
                    "on tables of this shape, and they need far more tuning "
                    "to lose."),
                "why_not_gpu": (
                    f"A {rows:,}-row table trains in seconds on the CPU. "
                    f"Moving it to the card would be slower — the transfer "
                    f"costs more than the arithmetic saves — and no more "
                    f"accurate. Your 3090 earns its keep on text and images, "
                    f"not here."
                    if not hw["cuda"] else
                    f"You have a {hw['name']}, and this is the one job it "
                    f"won't speed up: a {rows:,}-row table is seconds of CPU "
                    f"work, and the transfer costs more than the arithmetic "
                    f"saves."),
                "next": "train"}

    if kind == "text":
        rows = d.get("rows", 0)
        big = rows >= 2000
        return {**d, "hardware": hw,
                "approach": ("fine-tune a small transformer" if big
                             else "sentence embeddings + a classifier"),
                "use_gpu": True,
                "why_this": (
                    "With this many examples, fine-tuning the whole network "
                    "pays off." if big else
                    f"{rows} examples is too few to fine-tune a network "
                    f"without it memorising them. A pretrained model turns "
                    f"each row into an embedding and a simple classifier "
                    f"learns on top — this wins on small data and trains in "
                    f"under a minute."),
                "why_gpu": (
                    f"Embedding {rows:,} rows is the slow part, and the card "
                    f"does it in a fraction of the time."
                    if hw["cuda"] else
                    "This is where a GPU genuinely helps, and PyTorch isn't "
                    "installed. It will still run on the CPU, just slower."),
                "next": "train"}

    if kind == "images":
        total = d.get("total", 0)
        return {**d, "hardware": hw,
                "approach": "pretrained features + a classifier",
                "use_gpu": True,
                "why_this": (
                    f"{total} images is far too few to train a vision model "
                    f"from nothing. A network trained on millions of images "
                    f"already knows edges, textures and shapes; using it as "
                    f"a feature extractor and learning only the last step "
                    f"needs a fraction of the data and gets better results."),
                "why_gpu": ("Feature extraction is the whole cost here, and "
                            "it's exactly what the card is for."),
                "next": "train"}

    return {**d, "hardware": hw, "approach": "not supported yet",
            "use_gpu": False,
            "why_this": ("Time series needs different validation — you can't "
                         "shuffle rows that have an order without leaking "
                         "the future into the past. Treat it as tabular by "
                         "adding lag columns, or ask and I'll build the "
                         "proper split."),
            "next": ""}


# --------------------------------------------------------------------------- #
#  text and images — where the card is worth using
# --------------------------------------------------------------------------- #
def _embedder(prefer_gpu: bool = True):
    """A pretrained model that turns things into vectors.

    Tried in order of quality. Each is a real dependency, so the failure says
    what to install rather than falling over."""
    hw = hardware()
    device = "cuda" if (prefer_gpu and hw["cuda"]) else "cpu"
    try:
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer("all-MiniLM-L6-v2", device=device)
        return ("sentence-transformers/all-MiniLM-L6-v2", device,
                lambda xs: m.encode(list(xs), batch_size=64,
                                    show_progress_bar=False))
    except ImportError:
        pass
    try:
        import torch
        from transformers import AutoTokenizer, AutoModel
        name = "sentence-transformers/all-MiniLM-L6-v2"
        tok = AutoTokenizer.from_pretrained(name)
        mod = AutoModel.from_pretrained(name).to(device).eval()

        def encode(xs):
            out = []
            xs = list(xs)
            for i in range(0, len(xs), 32):
                b = tok(xs[i:i + 32], padding=True, truncation=True,
                        max_length=256, return_tensors="pt").to(device)
                with torch.no_grad():
                    h = mod(**b).last_hidden_state
                    mask = b["attention_mask"].unsqueeze(-1)
                    v = (h * mask).sum(1) / mask.sum(1)
                out.append(v.cpu().numpy())
            import numpy as np
            return np.vstack(out)
        return name, device, encode
    except ImportError:
        return None, device, None


def train_text(path: str, text_column: str, target: str, name: str = "",
               seed: int = 42) -> dict:
    """Embeddings from a pretrained model, then a classifier on top.

    Not a transformer fine-tune: on a few hundred labelled rows that
    memorises rather than learns. This is the approach that actually wins at
    this size, and it trains in under a minute."""
    import pandas as pd
    from sklearn.model_selection import train_test_split
    from sklearn.linear_model import LogisticRegression
    from sklearn.dummy import DummyClassifier
    from sklearn import metrics
    import joblib

    try:
        df = _load(path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    for c in (text_column, target):
        if c not in df.columns:
            return {"ok": False, "error": f"There's no column called '{c}'."}
    df = df.dropna(subset=[text_column, target])
    if len(df) < MIN_ROWS:
        return {"ok": False,
                "error": f"Only {len(df)} rows with both a text and a label."}

    model_name, device, encode = _embedder()
    if encode is None:
        return {"ok": False,
                "error": ("Text models need sentence-transformers:\\n"
                          "  pip install sentence-transformers\\n"
                          "For CUDA install torch first:\\n"
                          "  pip install torch --index-url "
                          "https://download.pytorch.org/whl/cu121")}

    X_txt = df[text_column].astype(str).tolist()
    y = df[target]
    strat = y if y.value_counts().min() >= 2 else None
    i_rest, i_test = train_test_split(range(len(df)), test_size=0.2,
                                      random_state=seed, stratify=strat)
    strat2 = y.iloc[i_rest] if strat is not None and \
        y.iloc[i_rest].value_counts().min() >= 2 else None
    i_tr, i_val = train_test_split(i_rest, test_size=0.25, random_state=seed,
                                   stratify=strat2)

    import time
    t0 = time.time()
    V = encode(X_txt)
    took = round(time.time() - t0, 1)

    clf = LogisticRegression(max_iter=2000)
    clf.fit(V[i_tr], y.iloc[i_tr])
    val = float(metrics.accuracy_score(y.iloc[i_val], clf.predict(V[i_val])))

    dummy = DummyClassifier(strategy="most_frequent").fit(V[i_tr], y.iloc[i_tr])
    test = float(metrics.accuracy_score(y.iloc[i_test], clf.predict(V[i_test])))
    base = float(metrics.accuracy_score(y.iloc[i_test],
                                        dummy.predict(V[i_test])))

    slug = _slug(name or f"{Path(path).stem}-{target}-text")
    d = _dir() / slug
    d.mkdir(parents=True, exist_ok=True)
    joblib.dump({"clf": clf, "embedder": model_name,
                 "text_column": text_column}, d / "model.joblib")
    card = {
        "ok": True, "name": slug, "at": _iso(), "task": "text classification",
        "data": str(Path(path).expanduser()), "target": target,
        "text_column": text_column,
        "approach": "pretrained embeddings + logistic regression",
        "embedder": model_name, "device": device,
        "embedding_seconds": took,
        "rows": {"train": len(i_tr), "validation": len(i_val),
                 "test": len(i_test)},
        "score_name": "accuracy",
        "validation_score": round(val, 4),
        "test_score": round(test, 4),
        "baseline_test_score": round(base, 4),
        "beats_baseline": test - base > 0.01,
        "verdict": _verdict("embeddings + logistic regression", "accuracy",
                            test, base, test - base > 0.01, True),
        "honest": [
            f"Embedded on {device}"
            + (f" in {took}s." if device == "cuda" else
               f" in {took}s — a GPU would do this in a fraction of the "
               f"time, and it is the only slow part."),
            "The text rows in the test split were never used for training or "
            "for choosing anything.",
            "This learns which wording goes with which label. It does not "
            "learn facts, so it will confidently mislabel anything phrased "
            "unlike the training set.",
        ],
    }
    (d / "model-card.json").write_text(json.dumps(card, indent=2,
                                                  default=str), "utf-8")
    return card


def train_images(folder: str, name: str = "", seed: int = 42) -> dict:
    """Pretrained vision features, then a classifier.

    A folder per class. Training a vision model from scratch needs hundreds
    of thousands of images; a network that already knows edges and textures
    needs a few dozen per class."""
    from sklearn.model_selection import train_test_split
    from sklearn.linear_model import LogisticRegression
    from sklearn.dummy import DummyClassifier
    from sklearn import metrics
    import joblib
    import numpy as np

    root = Path(folder).expanduser()
    classes = {d.name: sorted(f for f in d.rglob("*")
                              if f.suffix.lower() in IMAGE_EXTS)
               for d in sorted(root.iterdir()) if d.is_dir()}
    classes = {k: v for k, v in classes.items() if v}
    if len(classes) < 2:
        return {"ok": False,
                "error": "Needs at least two folders, one per class."}
    thin = {k: len(v) for k, v in classes.items() if len(v) < 20}
    hw = hardware()
    try:
        import torch
        from torchvision import models, transforms
    except ImportError:
        return {"ok": False,
                "error": ("Image models need torch and torchvision:\\n"
                          "  pip install torch torchvision --index-url "
                          "https://download.pytorch.org/whl/cu121\\n"
                          "(that index gives you the CUDA build for your "
                          "card)")}

    device = "cuda" if hw["cuda"] else "cpu"
    net = models.resnet50(weights=models.ResNet50_Weights.DEFAULT)
    net.fc = torch.nn.Identity()
    net = net.to(device).eval()
    prep = transforms.Compose([
        transforms.Resize(232), transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])])

    from PIL import Image
    import time
    paths, labels = [], []
    for cls, files in classes.items():
        paths += files
        labels += [cls] * len(files)
    t0 = time.time()
    feats = []
    for i in range(0, len(paths), 32):
        batch = []
        for p in paths[i:i + 32]:
            try:
                batch.append(prep(Image.open(p).convert("RGB")))
            except Exception:
                batch.append(torch.zeros(3, 224, 224))
        with torch.no_grad():
            feats.append(net(torch.stack(batch).to(device)).cpu().numpy())
    V = np.vstack(feats)
    took = round(time.time() - t0, 1)

    y = np.array(labels)
    i_rest, i_test = train_test_split(range(len(y)), test_size=0.2,
                                      random_state=seed, stratify=y)
    clf = LogisticRegression(max_iter=2000).fit(V[i_rest], y[i_rest])
    dummy = DummyClassifier(strategy="most_frequent").fit(V[i_rest], y[i_rest])
    test = float(metrics.accuracy_score(y[i_test], clf.predict(V[i_test])))
    base = float(metrics.accuracy_score(y[i_test], dummy.predict(V[i_test])))

    slug = _slug(name or root.name)
    d = _dir() / slug
    d.mkdir(parents=True, exist_ok=True)
    joblib.dump({"clf": clf, "backbone": "resnet50"}, d / "model.joblib")
    card = {
        "ok": True, "name": slug, "at": _iso(),
        "task": "image classification", "data": str(root),
        "approach": "pretrained ResNet-50 features + logistic regression",
        "device": device, "feature_seconds": took,
        "classes": {k: len(v) for k, v in classes.items()},
        "rows": {"train": len(i_rest), "test": len(i_test)},
        "score_name": "accuracy",
        "test_score": round(test, 4),
        "baseline_test_score": round(base, 4),
        "beats_baseline": test - base > 0.01,
        "verdict": _verdict("pretrained features + logistic regression",
                            "accuracy", test, base, test - base > 0.01, True),
        "honest": [
            f"Features extracted on {device} in {took}s."
            + ("" if device == "cuda" else
               " A GPU would make this several times faster, and it is where "
               "essentially all the time goes."),
            (f"Thin classes: {thin}. Fewer than about 20 images of a class "
             f"and its score is close to guesswork." if thin else
             "Every class has enough examples to mean something."),
            "Held-out images were never used for training.",
        ],
    }
    (d / "model-card.json").write_text(json.dumps(card, indent=2,
                                                  default=str), "utf-8")
    return card


def auto(path: str, target: str = "", name: str = "") -> dict:
    """Detect, plan, check for leakage, train. One call.

    The gates stay: a baseline to beat, a test set used once, and leakage
    reported before a high score is believed. "Just prompt it" should mean
    less typing, not fewer checks.
    """
    p = plan(path, target)
    if not p.get("ok"):
        return p
    kind = p["kind"]
    steps = [f"Looked at the data: {p['why']}.",
             f"Chose {p['approach']} — {p['why_this']}"]
    steps.append(p.get("why_gpu") or p.get("why_not_gpu", ""))

    if kind == "tabular":
        if not target:
            return {**p, "ok": False,
                    "error": "Say which column to predict.",
                    "steps": steps}
        leaks = find_leaks(path, target)
        drop = [s["column"] for s in leaks.get("suspects", [])]
        if drop:
            steps.append(f"Dropped {', '.join(drop)} — "
                         + leaks["suspects"][0]["why"])
        result = train(path, target, name=name, drop=drop)
        return {**result, "steps": steps, "plan": p, "leaks": leaks}

    if kind == "text":
        col = p["text_columns"][0]["column"]
        if not target:
            return {**p, "ok": False, "error": "Say which column to predict.",
                    "steps": steps}
        result = train_text(path, col, target, name=name)
        return {**result, "steps": steps, "plan": p}

    if kind == "images":
        result = train_images(path, name=name)
        return {**result, "steps": steps, "plan": p}

    return {**p, "ok": False, "steps": steps,
            "error": p.get("why_this", "Not supported yet.")}


# =========================================================================== #
#  A real builder: your own test set, the right metric, and a moveable line
#
#  The first version reported accuracy from one random split. Three things
#  were wrong with that, and each one changes the answer rather than
#  decorating it.
#
#  ONE SPLIT IS NOISE. On a few hundred rows the score moves several points
#  between random seeds. Cross-validation reports the spread, so you can see
#  whether a 3% difference between two models is real.
#
#  ACCURACY IS THE WRONG HEADLINE when classes are uneven. A churn model at
#  76% accuracy that never once predicts "churn" is useless and scores well.
#  Balanced accuracy and per-class recall say what actually happened.
#
#  0.5 IS AN ARBITRARY LINE. For churn you would rather catch 80% of leavers
#  and be wrong sometimes than catch 30% and be always right. That is a
#  business decision, and it needs a dial rather than a default.
# =========================================================================== #
def _metrics_for(y_true, y_pred, proba=None, classes=None) -> dict:
    """Everything worth knowing, not just the flattering number."""
    from sklearn import metrics as M
    import numpy as np
    out = {"accuracy": round(float(M.accuracy_score(y_true, y_pred)), 4)}
    try:
        out["balanced_accuracy"] = round(
            float(M.balanced_accuracy_score(y_true, y_pred)), 4)
    except Exception:
        pass
    try:
        rep = M.classification_report(y_true, y_pred, output_dict=True,
                                      zero_division=0)
        out["per_class"] = {
            str(k): {"precision": round(v["precision"], 3),
                     "recall": round(v["recall"], 3),
                     "f1": round(v["f1-score"], 3),
                     "rows": int(v["support"])}
            for k, v in rep.items() if isinstance(v, dict)}
    except Exception:
        pass
    try:
        out["confusion"] = M.confusion_matrix(y_true, y_pred).tolist()
        out["labels"] = [str(c) for c in sorted(set(y_true))]
    except Exception:
        pass
    if proba is not None and classes is not None and len(classes) == 2:
        try:
            pos = list(classes)[1]
            yb = (np.array(y_true) == pos).astype(int)
            out["roc_auc"] = round(float(M.roc_auc_score(yb, proba[:, 1])), 4)
            out["auc_note"] = (
                "ROC-AUC ignores where you put the cut-off, so it measures "
                "whether the model RANKS well — often the fairer number when "
                "classes are uneven.")
        except Exception:
            pass
    return out


def _headline(y, metrics: dict) -> dict:
    """Which number to lead with, and why that one."""
    import pandas as pd
    counts = pd.Series(y).value_counts(normalize=True)
    top = float(counts.iloc[0]) if len(counts) else 0
    if top >= 0.65 and "balanced_accuracy" in metrics:
        return {"name": "balanced accuracy",
                "value": metrics["balanced_accuracy"],
                "why": (f"The commonest class is {top * 100:.0f}% of the "
                        f"rows, so plain accuracy flatters a model that "
                        f"mostly guesses it. Balanced accuracy averages the "
                        f"recall of each class, which doesn't.")}
    return {"name": "accuracy", "value": metrics["accuracy"],
            "why": "The classes are reasonably even, so accuracy is honest."}


def evaluate_on(name: str, path: str, threshold: float = None) -> dict:
    """Score a saved model on a file YOU held back.

    This is the only genuinely fair test. Everything else — even a held-out
    split — was carved from data the model's author could see. A file you set
    aside before any of this started cannot have leaked into the training in
    any way, including through your own choices about what to clean.
    """
    import pandas as pd
    import numpy as np
    import joblib
    d = _dir() / _slug(name)
    if not (d / "model.joblib").exists():
        return {"ok": False, "error": f"No model called '{name}'."}
    try:
        card = json.loads((d / "model-card.json").read_text("utf-8"))
    except Exception:
        card = {}
    target = card.get("target", "")
    try:
        df = _load(path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if target not in df.columns:
        return {"ok": False,
                "error": (f"Your test file needs the '{target}' column — "
                          f"the real answers. Without them there is nothing "
                          f"to check the predictions against.")}

    pipe = joblib.load(d / "model.joblib")
    df = df.dropna(subset=[target])
    y = df[target]
    X = df.drop(columns=[target])
    expected = set((card.get("features") or {}).get("numeric", [])) | \
        set((card.get("features") or {}).get("categorical", []))
    missing = sorted(expected - set(X.columns))
    if missing:
        return {"ok": False,
                "error": (f"Missing columns the model needs: "
                          f"{', '.join(missing)}.")}

    proba, classes = None, None
    if hasattr(pipe, "predict_proba"):
        try:
            proba = pipe.predict_proba(X)
            classes = list(pipe.classes_)
        except Exception:
            proba = None
    if threshold is not None and proba is not None and len(classes) == 2:
        pred = np.where(proba[:, 1] >= threshold, classes[1], classes[0])
    else:
        pred = pipe.predict(X)

    mets = _metrics_for(y, pred, proba, classes)
    head = _headline(y, mets)
    train_score = card.get("test_score")
    drift = (round(mets["accuracy"] - train_score, 4)
             if isinstance(train_score, (int, float)) else None)
    return {
        "ok": True, "model": _slug(name), "rows": len(df),
        "file": str(Path(path).expanduser()),
        "threshold": threshold,
        "metrics": mets, "headline": head,
        "compared_to_training": {
            "its_own_test_score": train_score, "difference": drift,
            "reading": _drift_reading(drift)},
        "note": ("This is data the model has never seen and nobody tuned "
                 "against. If it scores much worse here than on its own test "
                 "split, that split was optimistic."),
    }


def _drift_reading(d) -> str:
    if d is None:
        return ""
    if d >= -0.03:
        return ("It holds up on your data — which is the result you want and "
                "the one people most often don't get.")
    if d >= -0.10:
        return (f"It is {abs(d) * 100:.0f} points worse on your data than on "
                f"its own test split. Some of that is normal variation; more "
                f"than a few points suggests the split was flattering.")
    return (f"It is {abs(d) * 100:.0f} points worse on your data. That is a "
            f"real gap — either your file differs from the training data in "
            f"some way that matters, or the original test split leaked.")


def threshold_curve(name: str, path: str = "", points: int = 19) -> dict:
    """What you gain and lose by moving the cut-off.

    0.5 is a default, not a decision. For churn you would usually rather
    catch 80% of leavers and be wrong sometimes than catch 30% and be right
    every time — but that is a judgement about the cost of each mistake, and
    only you know it."""
    import numpy as np
    import joblib
    d = _dir() / _slug(name)
    if not (d / "model.joblib").exists():
        return {"ok": False, "error": f"No model called '{name}'."}
    card = json.loads((d / "model-card.json").read_text("utf-8"))
    target = card.get("target", "")
    src = path or card.get("data", "")
    try:
        df = _load(src).dropna(subset=[target])
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    pipe = joblib.load(d / "model.joblib")
    if not hasattr(pipe, "predict_proba"):
        return {"ok": False,
                "error": "This model doesn't give probabilities, so there is "
                         "no threshold to move."}
    y = df[target]
    X = df.drop(columns=[target])
    proba = pipe.predict_proba(X)
    classes = list(pipe.classes_)
    if len(classes) != 2:
        return {"ok": False,
                "error": "Thresholds apply to two-class problems."}
    from sklearn import metrics as M
    pos = classes[1]
    yb = (np.array(y) == pos).astype(int)
    rows = []
    for t in np.linspace(0.05, 0.95, points):
        pred = (proba[:, 1] >= t).astype(int)
        rows.append({
            "threshold": round(float(t), 2),
            "caught": round(float(M.recall_score(yb, pred, zero_division=0)),
                            3),
            "right_when_flagged": round(
                float(M.precision_score(yb, pred, zero_division=0)), 3),
            "flagged": int(pred.sum()),
        })
    best = max(rows, key=lambda r: (2 * r["caught"] * r["right_when_flagged"])
               / max(1e-9, r["caught"] + r["right_when_flagged"]))
    return {"ok": True, "positive_class": str(pos), "curve": rows,
            "balanced_choice": best,
            "note": (f"At {best['threshold']} it catches "
                     f"{best['caught'] * 100:.0f}% of '{pos}' and is right "
                     f"{best['right_when_flagged'] * 100:.0f}% of the times "
                     f"it says so. Move it up to be surer and catch fewer, "
                     f"down to catch more and be wrong more often — which "
                     f"way depends on what each mistake costs you.")}


def importance(name: str, top: int = 15) -> dict:
    """Which columns the model actually leans on."""
    import numpy as np
    import joblib
    d = _dir() / _slug(name)
    if not (d / "model.joblib").exists():
        return {"ok": False, "error": f"No model called '{name}'."}
    pipe = joblib.load(d / "model.joblib")
    try:
        pre = pipe.named_steps["pre"]
        model = pipe.named_steps["model"]
        names = list(pre.get_feature_names_out())
    except Exception:
        return {"ok": False,
                "error": "Can't read importances from this model."}
    if hasattr(model, "feature_importances_"):
        vals = model.feature_importances_
        how = "how often the trees split on it"
    elif hasattr(model, "coef_"):
        vals = np.abs(np.ravel(model.coef_))
        how = "the size of its coefficient"
    else:
        # Not every model exposes weights — gradient boosting doesn't. So
        # measure it instead: shuffle one column, see how much the score
        # drops. That's slower and truer, because it says what the model
        # actually relies on rather than how it happens to be built.
        return _permutation_importance(name, pipe, top)
    order = np.argsort(vals)[::-1][:top]
    total = float(np.sum(vals)) or 1.0
    return {
        "ok": True, "measured_by": how,
        "features": [{"feature": _readable(names[i]),
                      "weight": round(float(vals[i]) / total, 4)}
                     for i in order],
        "note": ("Importance is not causation. A column can matter to the "
                 "model because it stands in for something else — and if that "
                 "something changes, the model quietly stops working."),
    }


def _permutation_importance(name: str, pipe, top: int) -> dict:
    """Shuffle a column and see what breaks."""
    import numpy as np
    from sklearn.inspection import permutation_importance as PI
    d = _dir() / _slug(name)
    try:
        card = json.loads((d / "model-card.json").read_text("utf-8"))
        df = _load(card["data"]).dropna(subset=[card["target"]])
    except Exception as exc:
        return {"ok": False, "error": f"Can't re-read the data: {exc}"}
    target = card["target"]
    y = df[target]
    X = df.drop(columns=[target])
    for c in (card.get("features") or {}).get("dropped_by_you", []):
        if c in X.columns:
            X = X.drop(columns=[c])
    try:
        r = PI(pipe, X, y, n_repeats=5, random_state=0, n_jobs=1)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    order = np.argsort(r.importances_mean)[::-1][:top]
    total = float(np.sum(np.clip(r.importances_mean, 0, None))) or 1.0
    return {
        "ok": True,
        "measured_by": ("how much the score drops when that column is "
                        "shuffled"),
        "features": [{"feature": str(X.columns[i]),
                      "weight": round(float(max(0.0, r.importances_mean[i]))
                                      / total, 4),
                      "score_drop": round(float(r.importances_mean[i]), 4)}
                     for i in order],
        "note": ("Measured, not inferred: each column was shuffled and the "
                 "score re-checked. A column near zero isn't being used — "
                 "which is worth knowing before you spend effort collecting "
                 "it. Importance is still not causation."),
    }


def _readable(n: str) -> str:
    return (n.replace("num__", "").replace("cat__", "")
            .replace("remainder__", ""))
