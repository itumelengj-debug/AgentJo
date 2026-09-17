"""Evals — can this cheaper engine actually do the job?

Feature 28 lets you pin any feature to any engine. That's only useful if you
know which features a small local model can actually handle, and until now
that was a guess — an expensive one in both directions: pin too aggressively
and the trend digest returns garbage, pin too little and you pay Claude prices
to reformat JSON.

So this measures it, against the contracts the app genuinely depends on.

The cases aren't generic benchmarks. They're the things that have actually
broken here: the trend digest demanding strict JSON, the job scorer returning
a schema with reasons-against, the watcher healer proposing CSS selectors, the
pipeline healer rewriting SQL. Every one of those failed on a real engine at
some point in this app's life, and each failure looked like a different bug
until the cause turned out to be "this model can't hold a JSON contract".

Grading is DETERMINISTIC — shape checks, key checks, parseability, substring
and numeric rules. No model grades another model: an LLM judge would add cost,
latency and its own failure modes to the very thing meant to measure failure.
The trade is that these cases test *contract compliance*, not eloquence. That
happens to be exactly what the routing decision hinges on.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config

CASE_TIMEOUT_NOTE = ("Slow local models can take a while — a full run is a "
                     "few minutes, not seconds.")


def _dir() -> Path:
    d = config.AGENT_HOME / "evals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# --------------------------------------------------------------------------- #
#  graders — deterministic, explaining themselves on failure
# --------------------------------------------------------------------------- #
def _extract_json(text: str):
    text = re.sub(r"^```(json)?|```$", "", (text or "").strip(),
                  flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in the reply")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("JSON object never closed")


def grade_json_shape(reply: str, spec: dict) -> tuple[bool, str]:
    """Checks the reply parses and carries the keys the feature relies on."""
    try:
        data = _extract_json(reply)
    except Exception as exc:
        return False, f"not usable JSON: {exc}"
    if not isinstance(data, dict):
        return False, f"expected an object, got {type(data).__name__}"
    for key in spec.get("keys", []):
        if key not in data:
            return False, f"missing key '{key}'"
    for key, kind in (spec.get("types") or {}).items():
        want = {"list": list, "str": str, "int": (int, float),
                "dict": dict}.get(kind, object)
        if key in data and not isinstance(data[key], want):
            return False, (f"'{key}' should be {kind}, got "
                           f"{type(data[key]).__name__}")
    path = spec.get("nested_list_keys")
    if path:
        outer, keys = path["in"], path["keys"]
        items = data.get(outer) or []
        if not isinstance(items, list) or not items:
            return False, f"'{outer}' should be a non-empty list"
        for k in keys:
            if k not in (items[0] or {}):
                return False, f"items in '{outer}' are missing '{k}'"
    if spec.get("clean_json") and not (reply or "").strip().startswith("{"):
        return True, "valid, but wrapped in prose/fences (tolerated)"
    return True, "ok"


def grade_contains(reply: str, spec: dict) -> tuple[bool, str]:
    low = (reply or "").lower()
    for needle in spec.get("all", []):
        if needle.lower() not in low:
            return False, f"missing '{needle}'"
    for needle in spec.get("none", []):
        if needle.lower() in low:
            return False, f"should not mention '{needle}'"
    return True, "ok"


GRADERS = {"json_shape": grade_json_shape, "contains": grade_contains}


# --------------------------------------------------------------------------- #
#  the cases — every one drawn from a contract this app actually depends on
# --------------------------------------------------------------------------- #
CASES = [
    {
        "id": "trends.digest_json",
        "feature": "trends",
        "why": "The trend digest is unusable unless the engine returns strict "
               "JSON. This is the exact contract that failed in the field.",
        "system": ("Cluster the items into trends. Return ONLY raw JSON: "
                   "{\"trends\": [{\"title\": str, \"why\": str, "
                   "\"sources\": [str], \"learnable\": {\"kind\": \"skill\", "
                   "\"name\": str, \"description\": str, \"instructions\": "
                   "str}}]}. No prose, no fences."),
        "user": json.dumps({"items": [
            {"title": "agent-memory-kit", "detail": "long-term memory"},
            {"title": "mcp-everywhere", "detail": "tool standard"}]}),
        "grader": "json_shape",
        "spec": {"keys": ["trends"], "types": {"trends": "list"},
                 "nested_list_keys": {"in": "trends",
                                      "keys": ["title", "learnable"]}},
    },
    {
        "id": "jobs.fit_score_json",
        "feature": "jobs",
        "why": "Job scoring must return a number AND honest reasons against; "
               "models that skip 'against' produce a scout that likes "
               "everything.",
        "system": ("Assess candidate fit. Return ONLY raw JSON: {\"score\": "
                   "0-100, \"verdict\": \"strong\"|\"possible\"|\"weak\", "
                   "\"for\": [str], \"against\": [str], \"missing\": [str]}."),
        "user": json.dumps({
            "profile": {"skills": ["Python", "SQL", "Power BI"],
                        "years": {"BI": 8}},
            "role": {"title": "Senior Kubernetes Platform Engineer",
                     "requirements": ["Kubernetes", "Go", "Terraform"]}}),
        "grader": "json_shape",
        "spec": {"keys": ["score", "verdict", "against"],
                 "types": {"score": "int", "against": "list"}},
    },
    {
        "id": "watchers.selector_heal_json",
        "feature": "watchers",
        "why": "Self-healing selectors need a precise JSON reply naming real "
               "classes from the page.",
        "system": ("A scraper's selectors broke. From the page STRUCTURE, "
                   "propose new ones. Return ONLY raw JSON: "
                   "{\"item_selector\": str, \"fields\": {name: "
                   "{\"selector\": str, \"attr\": str or null}}}."),
        "user": json.dumps({
            "FIELDS": ["title", "price"],
            "STRUCTURE": "div.results\ndiv.listing-card\n"
                         "span.listing-price\na.listing-title"}),
        "grader": "json_shape",
        "spec": {"keys": ["item_selector", "fields"],
                 "types": {"item_selector": "str", "fields": "dict"}},
    },
    {
        "id": "pipelines.sql_heal_json",
        "feature": "pipelines",
        "why": "The pipeline self-healer must return runnable SQL inside "
               "JSON — two contracts at once, which is where small models "
               "usually break.",
        "system": ("Fix the TO-BE SQL so it dedupes by id keeping the latest "
                   "ts. Return ONLY raw JSON: {\"silver_sql\": str, "
                   "\"gold_sql\": {name: str}}."),
        "user": json.dumps({
            "tobe_silver_sql": "CREATE TABLE silver_orders AS SELECT * FROM "
                               "bronze_orders;",
            "problem": "duplicate ids inflate the totals"}),
        "grader": "json_shape",
        "spec": {"keys": ["silver_sql"], "types": {"silver_sql": "str"}},
    },
    {
        "id": "general.instruction_following",
        "feature": "chat",
        "why": "A basic check that the engine obeys a negative instruction — "
               "models that can't will leak preambles into every draft.",
        "system": ("Reply with exactly the word DONE. No punctuation, no "
                   "explanation, nothing else."),
        "user": "Acknowledge.",
        "grader": "contains",
        "spec": {"all": ["done"], "none": ["sure", "here", "certainly",
                                           "as an ai"]},
    },
]


def cases_for(feature: str = "") -> list:
    return [c for c in CASES if not feature or c["feature"] == feature]


# --------------------------------------------------------------------------- #
#  running
# --------------------------------------------------------------------------- #
def run(brain, engine: str = "", feature: str = "") -> dict:
    """Run the suite against one engine. Never raises — a case that errors is
    a failed case, which is itself the finding."""
    from . import costs
    selected = cases_for(feature)
    results, passed = [], 0
    t0 = time.time()
    for case in selected:
        started = time.time()
        try:
            kw = {"model": engine} if engine and engine != "Auto" else {}
            resp = brain.chat([{"role": "user", "content": case["user"]}],
                              [case["system"]], None, **kw)
            reply = "".join(b.text for b in resp.content
                            if getattr(b, "type", "") == "text").strip()
            ok, detail = GRADERS[case["grader"]](reply, case["spec"])
            err = ""
        except Exception as exc:
            ok, detail, reply = False, f"{type(exc).__name__}: {exc}", ""
            err = detail
        passed += 1 if ok else 0
        results.append({"id": case["id"], "feature": case["feature"],
                        "ok": ok, "detail": detail,
                        "seconds": round(time.time() - started, 1),
                        "why": case["why"],
                        "reply_head": (reply or "")[:160], "error": err})
    total = len(selected) or 1
    report = {"at": _iso(), "engine": engine or "default",
              "passed": passed, "total": len(selected),
              "rate": round(100.0 * passed / total, 1),
              "seconds": round(time.time() - t0, 1),
              "results": results,
              "unpriced": costs.unpriced(engine) if engine else False}
    report["safe_features"] = sorted({
        r["feature"] for r in results if r["ok"]}
        - {r["feature"] for r in results if not r["ok"]})
    report["unsafe_features"] = sorted({
        r["feature"] for r in results if not r["ok"]})
    _save(report)
    _audit(engine, f"{passed}/{len(selected)} passed")
    return report


def _save(report: dict) -> None:
    try:
        path = _dir() / "history.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({k: v for k, v in report.items()
                                 if k != "results"}) + "\n")
        (_dir() / f"last-{re.sub(r'[^a-zA-Z0-9]+', '-', report['engine'])}"
                  f".json").write_text(json.dumps(report, indent=2), "utf-8")
    except Exception:
        pass


def history(n: int = 20) -> list:
    try:
        lines = (_dir() / "history.jsonl").read_text("utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
        if len(out) >= n:
            break
    return out


def last_for(engine: str) -> dict:
    try:
        key = re.sub(r"[^a-zA-Z0-9]+", "-", engine or "default")
        return json.loads((_dir() / f"last-{key}.json").read_text("utf-8"))
    except Exception:
        return {}


def recommendation(reports: list) -> list:
    """Turn results into the sentence you actually want: which features can
    move to which engine."""
    advice = []
    for rep in reports:
        eng = rep.get("engine", "?")
        safe = rep.get("safe_features") or []
        unsafe = rep.get("unsafe_features") or []
        if safe:
            advice.append(f"{eng} handled: {', '.join(safe)} — safe to pin "
                          f"those to it.")
        if unsafe:
            advice.append(f"{eng} failed: {', '.join(unsafe)} — keep those on "
                          f"a stronger engine.")
        if not safe and not unsafe:
            advice.append(f"{eng}: no cases run.")
    return advice


def _audit(engine: str, summary: str) -> None:
    try:
        from . import audit
        audit.record("eval", name=engine or "default", summary=summary[:200])
    except Exception:
        pass
