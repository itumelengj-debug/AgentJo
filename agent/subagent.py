"""A nested agent loop — one that doesn't know whose tools it's running.

This lived in `tools.py`, which meant `crew` imported `tools` for it while
`tools` imported `crew` for the crew tools: a cycle, so neither could be
read, tested or changed without the other.

Moving the function alone would have relocated the cycle rather than removed
it, because it calls the dispatcher. So the dispatcher and the tool list are
now ARGUMENTS. A sub-agent loop has no business knowing which tool registry
it is driving, and once it doesn't, nothing here imports anything that
imports it back.
"""
from __future__ import annotations


def _truncate(text: str, limit: int) -> str:
    """Four lines, pure, no dependencies. Importing it from tools would put
    this module back inside the cycle it was extracted from, which is a poor
    trade for avoiding four lines."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"


def run_subagent(brain, memory, console, objective: str,
                 context: str, auto_approve: bool, session_id: str,
                 model=None, tier_label: str = "",
                 execute=None, tool_defs=None) -> str:
    """Run an isolated agent loop and return only its final report.

    `execute(name, input, memory, console, ...)` dispatches a tool call and
    `tool_defs` is the list it may choose from. Both are passed in rather
    than imported, which is what keeps this module free of the cycle it came
    from.

    `model` pins the worker to a specific engine token (e.g. the free local
    model under teamwork mode); None uses the brain's own default."""
    import agent.config as _cfg
    system = (
        f"You are a focused sub-agent spawned by {_cfg.AGENT_NAME} to complete "
        "ONE objective and report back. You have a fresh context and cannot ask "
        "questions, so work from what you're given. Use tools to investigate or "
        "act, then end with a clear, self-contained report of findings/results. "
        "Be thorough in work but concise in your final answer."
    )
    prompt = f"OBJECTIVE:\n{objective}"
    if context.strip():
        prompt += f"\n\nCONTEXT PROVIDED:\n{context}"
    messages = [{"role": "user", "content": prompt}]
    console.print(f"[dim]  ⮑ sub-agent started"
                  f"{(' (' + tier_label + ')') if tier_label else ''}: "
                  f"{objective[:70]}[/dim]")

    final = ""
    for _ in range(_cfg.SUBAGENT_MAX_ROUNDS):
        try:
            response = brain.chat(messages, system, tool_defs,
                                  model=model)
        except Exception as exc:
            return f"Sub-agent error: {type(exc).__name__}: {exc}"
        text = "\n".join(b.text for b in response.content if b.type == "text")
        if text:
            final = text
        if response.stop_reason != "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            break
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            console.print(f"[dim]    · {block.name}[/dim]")
            out = execute(block.name, block.input, memory, console,
                               auto_approve, session_id, brain=brain, depth=1)
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": out})
        messages.append({"role": "user", "content": results})
    else:
        final += "\n(sub-agent stopped: round limit reached)"
    console.print("[dim]  ⮑ sub-agent finished[/dim]")
    return _truncate(final or "(sub-agent produced no report)", 8000)

