"""Export the agent's conversation log as a fine-tuning dataset.

Reads ~/.local_agent/agent.db (the log every conversation is written to)
and emits chat-format JSONL, one training example per line:

    {"messages": [{"role":"system",...},{"role":"user",...},{"role":"assistant",...}]}

Curation:
  --only-rated      keep only exchanges you marked with /good
  (replies you marked /bad are always excluded)
  --context-turns   how many prior exchanges to include for context
  --min-chars       drop trivially short replies

Usage:
    python training/export_dataset.py --out data/raw.jsonl
    python training/export_dataset.py --out data/best.jsonl --only-rated
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import config  # noqa: E402

DEFAULT_SYSTEM = (
    f"You are {config.AGENT_NAME}, a personal AI assistant running locally "
    f"on the user's computer. Be concise, practical, and direct."
)


def load_pairs(db_path: Path) -> list[dict]:
    """Return user→assistant exchanges grouped per session, in order."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT session_id, role, content, rating FROM messages ORDER BY id"
    ).fetchall()
    conn.close()

    pairs, pending_user = [], {}
    for r in rows:
        sid = r["session_id"]
        if r["role"] == "user":
            pending_user[sid] = r["content"]
        elif r["role"] == "assistant" and sid in pending_user:
            pairs.append({
                "session": sid,
                "user": pending_user.pop(sid),
                "assistant": r["content"],
                "rating": r["rating"],
            })
    return pairs


def build_kto_examples(pairs: list[dict], system: str,
                       context_turns: int) -> list[dict]:
    """KTO format: one example per RATED exchange, with a binary label.
    Unlike DPO, KTO needs no chosen/rejected pairing — your /good and /bad
    thumbs on different prompts are exactly the unpaired signal it wants.
      {"prompt": [...messages...], "completion": [{assistant}], "label": bool}
    """
    by_session: dict[str, list[dict]] = {}
    for p in pairs:
        by_session.setdefault(p["session"], []).append(p)

    examples = []
    for session_pairs in by_session.values():
        for i, p in enumerate(session_pairs):
            if p["rating"] == 0:
                continue                       # only thumbed exchanges train KTO
            prompt = [{"role": "system", "content": system}]
            for prev in session_pairs[max(0, i - context_turns):i]:
                prompt.append({"role": "user", "content": prev["user"]})
                prompt.append({"role": "assistant", "content": prev["assistant"]})
            prompt.append({"role": "user", "content": p["user"]})
            examples.append({
                "prompt": prompt,
                "completion": [{"role": "assistant", "content": p["assistant"]}],
                "label": p["rating"] > 0,
            })
    return examples


def build_examples(pairs: list[dict], system: str, context_turns: int,
                   only_rated: bool, min_chars: int) -> list[dict]:
    by_session: dict[str, list[dict]] = {}
    for p in pairs:
        by_session.setdefault(p["session"], []).append(p)

    examples = []
    for session_pairs in by_session.values():
        for i, p in enumerate(session_pairs):
            if p["rating"] < 0:
                continue                       # /bad — never train on it
            if only_rated and p["rating"] <= 0:
                continue
            if len(p["assistant"].strip()) < min_chars:
                continue
            msgs = [{"role": "system", "content": system}]
            for prev in session_pairs[max(0, i - context_turns):i]:
                msgs.append({"role": "user", "content": prev["user"]})
                msgs.append({"role": "assistant", "content": prev["assistant"]})
            msgs.append({"role": "user", "content": p["user"]})
            msgs.append({"role": "assistant", "content": p["assistant"]})
            examples.append({"messages": msgs})
    return examples


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(config.DB_PATH), help="path to agent.db")
    ap.add_argument("--out", required=True, help="output .jsonl path")
    ap.add_argument("--system", default=DEFAULT_SYSTEM)
    ap.add_argument("--context-turns", type=int, default=2)
    ap.add_argument("--only-rated", action="store_true",
                    help="keep only /good-rated exchanges")
    ap.add_argument("--min-chars", type=int, default=20)
    ap.add_argument("--kto", action="store_true",
                    help="emit KTO format (rated exchanges + binary labels) "
                         "for preference fine-tuning instead of plain SFT")
    args = ap.parse_args()

    pairs = load_pairs(Path(args.db))
    if args.kto:
        examples = build_kto_examples(pairs, args.system, args.context_turns)
        pos = sum(1 for e in examples if e["label"])
        neg = len(examples) - pos
        print(f"KTO: {len(examples)} rated examples ({pos} 👍 / {neg} 👎)")
        if pos == 0 or neg == 0:
            print("Note: KTO works best with BOTH thumbs-up and thumbs-down "
                  "examples. Rate a mix with /good and /bad.")
    else:
        examples = build_examples(pairs, args.system, args.context_turns,
                                  args.only_rated, args.min_chars)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")
    if not args.kto:
        print(f"{len(examples)} examples (from {len(pairs)} logged exchanges) → {out}")
    else:
        print(f"wrote {len(examples)} KTO examples → {out}")
    if not args.kto and len(examples) < 50:
        print("Note: under ~50 examples a fine-tune barely moves the needle. "
              "Keep chatting, rate good answers with /good, or generate more "
              "with training/distill.py.")


if __name__ == "__main__":
    main()
