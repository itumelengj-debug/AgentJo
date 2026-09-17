"""What actually happened — and what it's fair to conclude from it.

The pipeline acts and never looks back. After fifty applications you should
know which boards produce replies, whether a fit score of 90 really converts
better than 70, and whether email beats a portal form. Nothing here knew.

The hard part is not the counting. It is refusing to over-read it.

A job hunt produces tiny samples. Five applications to one board with two
replies is a 40% rate, and it is also completely consistent with a board that
converts at 8%. A tool that prints "LinkedIn: 40%" from five tries is worse
than one that prints nothing, because you will act on it — you will pour
effort into a board that was merely lucky, and drop one that was merely
unlucky.

So every rate here carries a Wilson interval, and a finding is only stated
when the interval is narrow enough to mean something. Below that, the honest
output is "not enough yet, here's how many more you'd need". That sentence is
the most useful thing this module produces, and it is the one most tools
leave out.

Nothing is adjusted automatically. Findings are shown with the evidence
attached, and you decide.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

# A reply is the first real signal. Interviews and offers are rarer still, so
# conversion is measured to "they responded at all" — waiting for offers means
# waiting forever to learn anything.
POSITIVE_STAGES = ("responded", "interview", "offer")
SENT_STAGES = ("applied",) + POSITIVE_STAGES

# Enough to compare at all. Below this a rate is noise wearing a percentage.
MIN_FOR_A_VERDICT = 12

# A CLAIM needs more than a sample size: the two intervals must not overlap.
# An earlier version also demanded the interval be narrower than 0.35, which
# sounds rigorous and is useless — a rate near 50% needs about eighty
# applications to get that tight, so the module stayed silent in exactly the
# situations it exists for. Non-overlapping intervals is the right test, and
# a conservative one.


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def wilson(hits: int, total: int, z: float = 1.96) -> tuple:
    """A confidence interval for a rate, honest on small samples.

    The naive rate (hits/total) says 100% from one success. Wilson says
    "somewhere between 21% and 100%", which is the truth."""
    if total <= 0:
        return 0.0, 0.0, 1.0
    p = hits / total
    d = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / d
    spread = (z / d) * math.sqrt(p * (1 - p) / total
                                 + z * z / (4 * total * total))
    return p, max(0.0, centre - spread), min(1.0, centre + spread)


def _bucket(rows: list, key) -> dict:
    out = {}
    for r in rows:
        k = key(r)
        if not k:
            continue
        b = out.setdefault(k, {"sent": 0, "replied": 0, "roles": []})
        b["sent"] += 1
        if r.get("stage") in POSITIVE_STAGES:
            b["replied"] += 1
        b["roles"].append(r.get("title", ""))
    return out


def _rate_rows(buckets: dict, label: str) -> list:
    rows = []
    for name, b in buckets.items():
        p, lo, hi = wilson(b["replied"], b["sent"])
        enough = b["sent"] >= MIN_FOR_A_VERDICT
        rows.append({
            label: name, "sent": b["sent"], "replied": b["replied"],
            "rate": round(p * 100, 1),
            "low": round(lo * 100, 1), "high": round(hi * 100, 1),
            "conclusive": enough,
            "reading": (f"{b['replied']} of {b['sent']}"
                        + (f" — somewhere between {lo * 100:.0f}% and "
                           f"{hi * 100:.0f}%" if b["sent"] else "")),
            "needed": (0 if enough else max(0, MIN_FOR_A_VERDICT - b["sent"])),
        })
    rows.sort(key=lambda r: (-r["sent"], -r["rate"]))
    return rows


def _score_band(r: dict) -> str:
    s = (r.get("fit") or {}).get("score")
    if s is None:
        return "not scored"
    if s >= 85:
        return "85+"
    if s >= 75:
        return "75–84"
    if s >= 65:
        return "65–74"
    return "under 65"


def _method(r: dict) -> str:
    how = (r.get("applied_how") or "").lower()
    if "portal" in how:
        return "portal form"
    if "email" in how:
        return "email"
    if how:
        return "by hand"
    return "email" if r.get("apply_email") else "portal form"


def _days_between(a: str, b: str) -> float | None:
    for fmt in ("%Y-%m-%d %H:%M UTC", "%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%d"):
        try:
            d1 = datetime.strptime(a, fmt).replace(tzinfo=timezone.utc)
            break
        except Exception:
            continue
    else:
        return None
    try:
        d2 = (datetime.strptime(b, fmt).replace(tzinfo=timezone.utc)
              if b else datetime.now(timezone.utc))
    except Exception:
        d2 = datetime.now(timezone.utc)
    return (d2 - d1).total_seconds() / 86400


def analyse(roles: list = None) -> dict:
    """Everything that happened, and what it's fair to conclude."""
    if roles is None:
        from . import jobscout
        roles = jobscout.roles() + [
            r for r in jobscout.archived()
            if r.get("stage") in SENT_STAGES]
    sent = [r for r in roles if r.get("stage") in SENT_STAGES]
    replied = [r for r in sent if r.get("stage") in POSITIVE_STAGES]

    waits = [d for d in (_days_between(r.get("applied_at", ""),
                                       r.get("replied_at", ""))
                         for r in replied) if d is not None]
    silent = [d for d in (_days_between(r.get("applied_at", ""), "")
                          for r in sent
                          if r.get("stage") == "applied") if d is not None]

    overall_p, overall_lo, overall_hi = wilson(len(replied), len(sent))
    return {
        "at": _iso(),
        "sent": len(sent), "replied": len(replied),
        "overall": {"rate": round(overall_p * 100, 1),
                    "low": round(overall_lo * 100, 1),
                    "high": round(overall_hi * 100, 1)},
        "by_source": _rate_rows(_bucket(sent, lambda r: r.get("source", "")),
                                "source"),
        "by_score": _rate_rows(_bucket(sent, _score_band), "band"),
        "by_method": _rate_rows(_bucket(sent, _method), "method"),
        "reply_days": {
            "typical": round(sorted(waits)[len(waits) // 2], 1) if waits
            else None,
            "longest": round(max(waits), 1) if waits else None,
            "count": len(waits),
        },
        "still_waiting": {
            "count": len(silent),
            "longest_days": round(max(silent), 1) if silent else None,
        },
        "note": ("Every rate carries the range it could really be. A rate "
                 "without one is a guess wearing a percentage sign."),
    }


def findings(data: dict = None) -> list:
    """Only what the numbers actually support.

    Each finding carries the evidence, so you can disagree with it."""
    d = data or analyse()
    out = []

    if d["sent"] < MIN_FOR_A_VERDICT:
        out.append({
            "kind": "not yet", "confident": False,
            "what": (f"{d['sent']} application(s) so far — too few to "
                     f"conclude anything."),
            "why": (f"At this size a difference between two boards is "
                    f"indistinguishable from luck. About "
                    f"{MIN_FOR_A_VERDICT - d['sent']} more and the numbers "
                    f"start to mean something."),
            "do": "Keep applying. Nothing here needs changing yet.",
        })
        return out

    # does the fit score predict anything? the most valuable question here,
    # because everything downstream is built on that number
    bands = {b["band"]: b for b in d["by_score"] if b["conclusive"]}
    if "85+" in bands and ("65–74" in bands or "under 65" in bands):
        high = bands["85+"]
        low = bands.get("65–74") or bands["under 65"]
        if high["low"] > low["high"]:
            out.append({
                "kind": "scoring works", "confident": True,
                "what": (f"Roles scored 85+ reply at {high['rate']:.0f}%, "
                         f"against {low['rate']:.0f}% for {low['band']}."),
                "why": ("The ranges don't overlap, so this isn't noise."),
                "do": ("Raising the minimum fit would concentrate effort "
                       "where it lands."),
            })
        elif high["high"] < low["low"]:
            out.append({
                "kind": "scoring is backwards", "confident": True,
                "what": (f"Roles you score HIGHER reply less "
                         f"({high['rate']:.0f}% vs {low['rate']:.0f}%)."),
                "why": ("That's worth knowing: the score is measuring "
                        "something other than what gets replies — probably "
                        "how well the advert matches your profile's wording "
                        "rather than whether you'd get the job."),
                "do": ("Read three high-scoring rejections next to three "
                       "low-scoring replies before trusting the number."),
            })
        else:
            out.append({
                "kind": "scoring unproven", "confident": False,
                "what": ("High and low scoring roles reply at rates that "
                         "overlap."),
                "why": ("So the score isn't yet shown to predict anything. "
                        "It may still — there just isn't the evidence."),
                "do": "Don't raise the minimum fit on the strength of it.",
            })

    conclusive_sources = [s for s in d["by_source"] if s["conclusive"]]
    if len(conclusive_sources) >= 2:
        best, worst = conclusive_sources[0], conclusive_sources[-1]
        for s in conclusive_sources:
            if s["rate"] > best["rate"]:
                best = s
            if s["rate"] < worst["rate"]:
                worst = s
        if best["low"] > worst["high"]:
            out.append({
                "kind": "a source is better", "confident": True,
                "what": (f"{best['source']} replies at {best['rate']:.0f}%; "
                         f"{worst['source']} at {worst['rate']:.0f}%."),
                "why": f"{best['reading']} against {worst['reading']}.",
                "do": (f"Worth more time on {best['source']}. Before dropping "
                       f"{worst['source']}, check it isn't advertising "
                       f"different roles rather than being worse."),
            })

    methods = [m for m in d["by_method"] if m["conclusive"]]
    if len(methods) >= 2:
        a, b = methods[0], methods[1]
        if a["low"] > b["high"] or b["low"] > a["high"]:
            better, worse = (a, b) if a["rate"] > b["rate"] else (b, a)
            out.append({
                "kind": "one route works better", "confident": True,
                "what": (f"{better['method']} replies at "
                         f"{better['rate']:.0f}%, {worse['method']} at "
                         f"{worse['rate']:.0f}%."),
                "why": f"{better['reading']} against {worse['reading']}.",
                "do": (f"Prefer {better['method']} where a role offers both."),
            })

    rd = d["reply_days"]
    if rd["count"] >= 5 and rd["longest"]:
        out.append({
            "kind": "when to stop waiting", "confident": rd["count"] >= 10,
            "what": (f"Replies usually come in {rd['typical']:.0f} days; the "
                     f"slowest took {rd['longest']:.0f}."),
            "why": f"Based on {rd['count']} repl(ies).",
            "do": (f"Anything silent past {rd['longest']:.0f} days is "
                   f"realistically closed — archive it and stop counting on "
                   f"it."),
        })

    if not out:
        out.append({
            "kind": "nothing conclusive", "confident": False,
            "what": ("Enough applications to count, but no difference big "
                     "enough to act on."),
            "why": ("Every comparison so far is within the range you'd "
                    "expect from chance."),
            "do": "Carry on as you are; this will sharpen as it grows.",
        })
    return out


def summary() -> dict:
    d = analyse()
    return {**d, "findings": findings(d)}
