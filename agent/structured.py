"""Structured output — asking for a shape and getting one.

Eight modules in this app ask a model for JSON by *instruction*: "Return ONLY
raw JSON, no prose, no fences." Three of them then carry their own extractor
that strips code fences, hunts for the first `{`, counts braces, and gives up
if that fails. It works most of the time, which is the problem — the failures
are silent and uneven. A challenge brief comes back as prose and the scan
reports nothing found; an eval verdict loses a field and the score is wrong
rather than absent.

Every major provider now solves this properly: define a schema, force the
model to call a tool with that shape, and the API validates before you ever
see it. No parsing, no fences, no "please".

So this does that where the engine supports it, and falls back to
instruction-plus-parse where it doesn't — with one extractor rather than
three, a real validation pass, and **one repair attempt that shows the model
its own error**. A model told "field 'score' must be a number, you sent
'high'" fixes it far more reliably than one told to try again.

What it will not do is invent a value to fill a gap. A missing required field
comes back as a failure with the field named, because a plausible default is
how a wrong number ends up in a report nobody questions.
"""
from __future__ import annotations

import json
import re
from typing import Any

MAX_REPAIRS = 1


class StructureError(ValueError):
    """The model didn't produce the requested shape, and repair didn't fix it."""


# --------------------------------------------------------------------------- #
#  parsing, once, properly
# --------------------------------------------------------------------------- #
def extract(text: str) -> Any:
    """Pull JSON out of whatever came back.

    The single extractor. Three modules each had their own, which meant three
    slightly different ideas of what counts as valid — and a fix to one never
    reached the others."""
    s = (text or "").strip()
    if not s:
        raise StructureError("the engine returned nothing")
    # fenced blocks, with or without a language tag
    fence = re.search(r"```(?:json|JSON)?\s*\n(.*?)\n?```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # a JSON value embedded in prose: find the first bracket and match it
    for opener, closer in (("{", "}"), ("[", "]")):
        start = s.find(opener)
        if start == -1:
            continue
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except json.JSONDecodeError:
                        break
    raise StructureError("no valid JSON in the reply")


# --------------------------------------------------------------------------- #
#  validation — say what's wrong, don't guess what was meant
# --------------------------------------------------------------------------- #
_TYPES = {"string": str, "number": (int, float), "integer": int,
          "boolean": bool, "array": list, "object": dict}


def validate(value: Any, schema: dict, path: str = "") -> list:
    """Problems, in words a model can act on.

    Returns a list of plain-English faults rather than raising, because the
    repair attempt feeds them straight back — and "expected a number for
    'score', got the string 'high'" produces a fix where "invalid" does not.
    """
    out = []
    want = schema.get("type")
    if want and want in _TYPES and not isinstance(value, _TYPES[want]):
        # a bool is an int in Python, and almost never what a number field means
        if not (want in ("number", "integer") and isinstance(value, bool)):
            if isinstance(value, _TYPES[want]):
                pass
            else:
                out.append(f"{path or 'the value'} should be a {want}, but "
                           f"{_describe(value)} was sent")
                return out
        else:
            out.append(f"{path or 'the value'} should be a {want}, but a "
                       f"true/false was sent")
            return out

    if want == "object" or (isinstance(value, dict) and "properties" in schema):
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                out.append(f"{path + '.' if path else ''}{key} is required "
                           f"and was missing")
        for key, sub in props.items():
            if key in value:
                out += validate(value[key], sub,
                                f"{path + '.' if path else ''}{key}")
    elif want == "array" and isinstance(value, list):
        item = schema.get("items")
        if item:
            for i, v in enumerate(value[:40]):
                out += validate(v, item, f"{path}[{i}]")
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append(f"{path or 'the list'} needs at least "
                       f"{schema['minItems']} item(s), got {len(value)}")

    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path or 'the value'} must be one of "
                   f"{', '.join(map(str, schema['enum']))} — got "
                   f"{_describe(value)}")
    return out


def _describe(v: Any) -> str:
    if isinstance(v, str):
        return f"the text {v[:40]!r}"
    if isinstance(v, bool):
        return "a true/false"
    if v is None:
        return "nothing"
    return f"{type(v).__name__} {str(v)[:40]}"


# --------------------------------------------------------------------------- #
#  asking
# --------------------------------------------------------------------------- #
def ask(brain, system: str, user: str, schema: dict, model=None,
        tool_name: str = "answer", repairs: int = MAX_REPAIRS) -> dict:
    """Get a value matching `schema`, or a clear failure.

    Prefers a forced tool call, which is how providers validate the shape
    before it ever reaches you. Falls back to instruction-and-parse for
    engines without tool support — the same contract either way, so callers
    don't branch on which engine they're using.
    """
    tool = {
        "name": tool_name,
        "description": "Return the answer in this exact shape.",
        "input_schema": schema,
    }
    kw = {"model": model} if model else {}
    tried, last_problems, raw = 0, [], ""
    messages = [{"role": "user", "content": user}]

    while tried <= max(0, repairs):
        used_tool = False
        try:
            resp = brain.chat(messages, [system], [tool], **kw)
        except TypeError:
            resp = brain.chat(messages, [system], None, **kw)
        except Exception as exc:
            raise StructureError(f"{type(exc).__name__}: {exc}") from exc

        value = None
        for block in getattr(resp, "content", []) or []:
            if getattr(block, "type", "") == "tool_use":
                value = getattr(block, "input", None)
                used_tool = True
                break
        if value is None:
            raw = "\n".join(b.text for b in getattr(resp, "content", []) or []
                            if getattr(b, "type", "") == "text")
            try:
                value = extract(raw)
            except StructureError as exc:
                last_problems = [str(exc)]
                value = None

        if value is not None:
            problems = validate(value, schema)
            if not problems:
                return {"ok": True, "value": value, "via": (
                    "schema" if used_tool else "parsed"), "repairs": tried}
            last_problems = problems

        tried += 1
        if tried > max(0, repairs):
            break
        # show it the fault. "invalid" produces another invalid answer;
        # naming the field and what was wrong with it usually doesn't.
        messages = [
            {"role": "user", "content": user},
            {"role": "assistant",
             "content": raw or json.dumps(value, default=str)[:1500]},
            {"role": "user",
             "content": ("That wasn't the shape asked for:\n- "
                         + "\n- ".join(last_problems[:6])
                         + "\n\nSend it again, corrected. Don't invent values "
                           "to fill gaps — leave a field out rather than "
                           "guessing it.")},
        ]

    raise StructureError(
        "The engine didn't produce the requested shape"
        + (f" — {'; '.join(last_problems[:4])}" if last_problems else "")
        + f". Tried {tried} time(s).")


def ask_or_none(brain, system: str, user: str, schema: dict, model=None,
                **kw) -> dict | None:
    """For callers that would rather have nothing than an exception."""
    try:
        return ask(brain, system, user, schema, model=model, **kw)["value"]
    except StructureError:
        return None
