"""Knowledge distillation: use Claude (teacher) to build a high-quality
training set for your local model (student).

Two modes, combinable:

  --from-log      Take every logged exchange and have Claude rewrite the
                  reply into an ideal "gold" answer. Your real questions,
                  teacher-quality answers — the best signal for style and
                  procedure.

  --from-memory   Generate synthetic Q→A pairs grounded in the agent's
                  memories and taught skills, so its accumulated knowledge
                  gets practised into the student's weights.

Caveat (be honest with yourself): weights are great at absorbing STYLE,
FORMAT, and PROCEDURES, but unreliable at storing FACTS — models trained
on a few hundred examples will still misremember specifics. That's why
the agent keeps memory retrieval ON even when running a tuned model.
Facts live in the database; behaviour lives in the weights.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python training/distill.py --from-log --from-memory --out data/distilled.jsonl
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import config  # noqa: E402
from agent.memory import MemoryStore  # noqa: E402
from training.export_dataset import DEFAULT_SYSTEM, load_pairs  # noqa: E402

REWRITE_SYSTEM = f"""You are creating gold-standard training data for a small \
local assistant named {config.AGENT_NAME} that runs on a user's personal computer.

You will see a real user message and the assistant's original reply. Rewrite the \
reply into an ideal version: factually careful, directly useful, well structured, \
and concise (usually under 200 words). Preserve the original intent and any \
correct specifics. Fix errors, padding, and rambling.

Output ONLY the rewritten reply — no preamble, no commentary."""

SYNTH_SYSTEM = f"""You are creating training data for {config.AGENT_NAME}, a \
personal local assistant. Below are the user's stored memories and taught skills.

Generate {{n}} DIVERSE training examples that exercise this knowledge: questions \
or requests this user would plausibly make, each with an ideal assistant answer \
that correctly uses the memories/skills. Vary phrasing, length, and topic. \
Answers must stay consistent with the stored information.

Output a JSON array only — no markdown fences, no commentary:
[{{{{"user": "...", "assistant": "..."}}}}, ...]"""


def get_client():
    try:
        from anthropic import Anthropic
    except ImportError:
        sys.exit("pip install anthropic   (see requirements.txt)")
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY first — the teacher model needs it.")
    return Anthropic()


def teacher_call(client, system: str, user: str, max_tokens: int = 1500) -> str:
    resp = client.messages.create(
        model=config.MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def distill_log(client, db: Path, limit: int) -> list[dict]:
    pairs = [p for p in load_pairs(db) if p["rating"] >= 0][:limit]
    out = []
    for i, p in enumerate(pairs, 1):
        prompt = (f"USER MESSAGE:\n{p['user'][:3000]}\n\n"
                  f"ORIGINAL REPLY:\n{p['assistant'][:3000]}")
        try:
            gold = teacher_call(client, REWRITE_SYSTEM, prompt)
        except Exception as exc:
            print(f"  [{i}/{len(pairs)}] skipped ({type(exc).__name__})")
            continue
        if len(gold) < 10:
            continue
        out.append({"messages": [
            {"role": "system", "content": DEFAULT_SYSTEM},
            {"role": "user", "content": p["user"]},
            {"role": "assistant", "content": gold},
        ]})
        print(f"  [{i}/{len(pairs)}] distilled")
    return out


def distill_memory(client, db: Path, n_pairs: int) -> list[dict]:
    store = MemoryStore(db)
    mems = [f"- [{m['category']}] {m['content']}" for m in store.all_memories()]
    skills = [f"- {s['name']}: when {s['description']} → {s['instructions']}"
              for s in store.get_skills()]
    store.close()
    if not mems and not skills:
        print("  no memories or skills yet — nothing to synthesise from")
        return []
    knowledge = "MEMORIES:\n" + ("\n".join(mems) or "(none)") + \
                "\n\nSKILLS:\n" + ("\n".join(skills) or "(none)")
    out, batch = [], 10
    for start in range(0, n_pairs, batch):
        want = min(batch, n_pairs - start)
        try:
            raw = teacher_call(client, SYNTH_SYSTEM.format(n=want),
                               knowledge[:12000], max_tokens=3000)
            raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
            items = json.loads(raw)
        except Exception as exc:
            print(f"  batch failed ({type(exc).__name__}), continuing")
            continue
        for it in items:
            if isinstance(it, dict) and it.get("user") and it.get("assistant"):
                out.append({"messages": [
                    {"role": "system", "content": DEFAULT_SYSTEM},
                    {"role": "user", "content": str(it["user"])},
                    {"role": "assistant", "content": str(it["assistant"])},
                ]})
        print(f"  synthesised {len(out)}/{n_pairs}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--out", required=True, help="output .jsonl")
    ap.add_argument("--from-log", action="store_true")
    ap.add_argument("--from-memory", action="store_true")
    ap.add_argument("--limit", type=int, default=300, help="max log exchanges to distill")
    ap.add_argument("--synthetic-pairs", type=int, default=60)
    args = ap.parse_args()
    if not (args.from_log or args.from_memory):
        ap.error("choose --from-log and/or --from-memory")

    client = get_client()
    examples: list[dict] = []
    if args.from_log:
        print("Distilling conversation log (teacher rewrite)...")
        examples += distill_log(client, Path(args.db), args.limit)
    if args.from_memory:
        print("Synthesising from memories + skills...")
        examples += distill_memory(client, Path(args.db), args.synthetic_pairs)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")
    print(f"{len(examples)} gold examples → {out}")


if __name__ == "__main__":
    main()
