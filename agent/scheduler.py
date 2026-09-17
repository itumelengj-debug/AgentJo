"""Schedule specs for Agent Jo scheduled tasks.

Pure logic only (no threads, no I/O): build/parse a small JSON spec and
compute the next run time. The runner loop lives in the app, which calls
into this module — keeping the date math deterministic and testable.

Spec kinds:
  minutes   every N minutes                {"kind": "minutes", "n": 30}
  hourly    at the top of every hour       {"kind": "hourly"}
  daily     every day at HH:MM             {"kind": "daily", "time": "07:30"}
  weekdays  Mon-Fri at HH:MM               {"kind": "weekdays", "time": "07:30"}
  weekly    one weekday at HH:MM           {"kind": "weekly", "time": "09:00", "dow": 0}
All times are LOCAL time. dow: 0=Monday ... 6=Sunday.
"""

import json
from datetime import datetime, timedelta

KINDS = ("minutes", "hourly", "daily", "weekdays", "weekly")
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday"]


def _parse_time(time_str: str) -> tuple[int, int]:
    try:
        hh, mm = time_str.strip().split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        raise ValueError(f"Time must be HH:MM (24h), got '{time_str}'")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Time out of range: '{time_str}'")
    return h, m


def make_spec(kind: str, time_str: str = "07:00", n: int = 30,
              dow: int = 0) -> str:
    """Validate inputs and return the spec as a JSON string."""
    if kind not in KINDS:
        raise ValueError(f"Unknown schedule kind '{kind}'")
    spec: dict = {"kind": kind}
    if kind == "minutes":
        n = int(n)
        if not (1 <= n <= 24 * 60):
            raise ValueError("Repeat interval must be 1-1440 minutes")
        spec["n"] = n
    elif kind in ("daily", "weekdays", "weekly"):
        _parse_time(time_str)            # validate
        spec["time"] = time_str.strip()
        if kind == "weekly":
            dow = int(dow)
            if not (0 <= dow <= 6):
                raise ValueError("Weekday must be 0 (Monday) to 6 (Sunday)")
            spec["dow"] = dow
    return json.dumps(spec)


def parse_spec(spec_json: str) -> dict:
    spec = json.loads(spec_json)
    if spec.get("kind") not in KINDS:
        raise ValueError(f"Unknown schedule kind in spec: {spec_json}")
    return spec


def describe_spec(spec: dict) -> str:
    kind = spec["kind"]
    if kind == "minutes":
        return f"every {spec.get('n', 30)} min"
    if kind == "hourly":
        return "hourly (on the hour)"
    if kind == "daily":
        return f"daily at {spec.get('time', '07:00')}"
    if kind == "weekdays":
        return f"weekdays at {spec.get('time', '07:00')}"
    if kind == "weekly":
        day = WEEKDAYS[spec.get("dow", 0)]
        return f"{day}s at {spec.get('time', '09:00')}"
    return kind


def next_run(spec: dict, now: datetime | None = None) -> float:
    """Unix timestamp of the next occurrence strictly after `now` (local)."""
    now = now or datetime.now()
    kind = spec["kind"]

    if kind == "minutes":
        return (now + timedelta(minutes=int(spec.get("n", 30)))).timestamp()

    if kind == "hourly":
        nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return nxt.timestamp()

    h, m = _parse_time(spec.get("time", "07:00"))
    candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)

    if kind == "daily":
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate.timestamp()

    if kind == "weekdays":
        if candidate <= now:
            candidate += timedelta(days=1)
        while candidate.weekday() >= 5:          # 5=Sat, 6=Sun
            candidate += timedelta(days=1)
        return candidate.timestamp()

    if kind == "weekly":
        dow = int(spec.get("dow", 0))
        ahead = (dow - now.weekday()) % 7
        candidate += timedelta(days=ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate.timestamp()

    raise ValueError(f"Unknown schedule kind '{kind}'")
