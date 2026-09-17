"""Open-weight models — choosing them, fitting them, and making your own.

Three separate jobs that get muddled together, so they're kept apart here.

**Choosing.** Which open-weight models are worth running, what they're good
at, and what their licence actually permits. That last one matters if
anything you build gets used commercially, and it varies more than people
assume — Apache-2.0 and Llama's community licence are not the same offer.

**Fitting.** A model either fits in your VRAM or it swaps to system RAM and
crawls. The arithmetic is simple and worth doing before a 20GB download
rather than after: parameters × bits-per-weight, plus room for context. This
gives a straight answer for the card you actually have.

**Deriving.** Two honest routes to "my own engine", and they are not
equivalent:

  A *Modelfile* variant takes an existing model and fixes its system prompt,
  temperature and context window under a new name. No training, no GPU time,
  works immediately, and it is a real engine you can pin per feature. For most
  of what people mean by "my own model", this is it.

  A *LoRA fine-tune* actually changes the weights. It needs a dataset, hours
  of GPU time, and a training stack. This module writes a script sized to your
  hardware and tells you plainly what it will cost — it does not pretend to
  have run it.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config

OLLAMA = "http://localhost:11434"

# Bits per weight for the common quantisations, and what you give up.
QUANTS = {
    "q4_K_M": {"bits": 4.8, "note": "the usual choice — small loss, big saving"},
    "q5_K_M": {"bits": 5.7, "note": "a little better, a little larger"},
    "q6_K": {"bits": 6.6, "note": "close to full quality"},
    "q8_0": {"bits": 8.5, "note": "near-lossless, twice the size of q4"},
    "f16": {"bits": 16.0, "note": "unquantised; rarely worth it locally"},
}

# Open-weight families worth running locally. Licence is stated because it
# changes what you may do with what you build.
CATALOGUE = [
    {"name": "qwen3", "sizes": [4, 8, 14, 32], "licence": "Apache-2.0",
     "good_at": ["general", "code", "reasoning", "multilingual"],
     "note": "Strong all-rounder; the 14B is the sweet spot on 24GB"},
    {"name": "llama3.3", "sizes": [8, 70], "licence": "Llama Community",
     "good_at": ["general", "instruction following"],
     "note": "Licence has conditions above 700M monthly users"},
    {"name": "gemma3", "sizes": [4, 12, 27], "licence": "Gemma Terms",
     "good_at": ["general", "summarising"],
     "note": "Google's terms attach a use policy"},
    {"name": "deepseek-r1", "sizes": [7, 14, 32], "licence": "MIT",
     "good_at": ["reasoning", "maths", "code"],
     "note": "Thinks before answering; slower per token, better on hard tasks"},
    {"name": "mistral", "sizes": [7], "licence": "Apache-2.0",
     "good_at": ["general", "fast"],
     "note": "Old but quick and permissive"},
    {"name": "codellama", "sizes": [7, 13, 34], "licence": "Llama Community",
     "good_at": ["code"], "note": "Code completion rather than chat"},
    {"name": "phi4", "sizes": [14], "licence": "MIT",
     "good_at": ["reasoning", "small"],
     "note": "Punches above its size on reasoning"},
    {"name": "nomic-embed-text", "sizes": [0.14], "licence": "Apache-2.0",
     "good_at": ["embeddings"],
     "note": "For search over your own documents, not chat"},
]


def _dir() -> Path:
    d = config.AGENT_HOME / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# --------------------------------------------------------------------------- #
#  fitting
# --------------------------------------------------------------------------- #
def vram_needed(params_b: float, quant: str = "q4_K_M",
                context: int = 8192) -> dict:
    """How much card a model wants.

    Weights are the bulk; the KV cache grows with context and is what
    surprises people when a model that loaded fine dies on a long document."""
    bits = QUANTS.get(quant, QUANTS["q4_K_M"])["bits"]
    weights = params_b * 1e9 * bits / 8 / 1e9          # GB
    # KV cache: roughly 0.5 MB per 1k tokens per billion params at fp16,
    # a rule of thumb rather than an exact figure
    kv = (context / 1000.0) * params_b * 0.0005 * 1000 / 1000
    overhead = 0.8                                      # runtime, buffers
    total = weights + kv + overhead
    return {"weights_gb": round(weights, 1), "context_gb": round(kv, 1),
            "overhead_gb": overhead, "total_gb": round(total, 1),
            "quant": quant, "context": context}


# Windows, the display driver and whatever else is on screen hold VRAM
# before your model asks for any. Ignoring that recommended a 34B on a 24GB
# card with "2.7 GB spare", which is how you get an out-of-memory error on a
# model the app told you would be comfortable.
DISPLAY_RESERVE_GB = 1.5


def fits(params_b: float, vram_gb: float, quant: str = "q4_K_M",
         context: int = 8192, reserve_gb: float = None) -> dict:
    """Straight answer, with the reason."""
    reserve = DISPLAY_RESERVE_GB if reserve_gb is None else reserve_gb
    usable = max(0.0, vram_gb - reserve)
    need = vram_needed(params_b, quant, context)
    headroom = round(usable - need["total_gb"], 1)
    if headroom >= 3:
        verdict, why = "comfortable", f"{headroom} GB spare"
    elif headroom >= 0:
        verdict, why = "tight", (f"only {headroom} GB spare after the display "
                                 f"takes its {reserve} GB — it will load, but "
                                 f"a long document may not")
    else:
        verdict, why = "too big", (f"{abs(headroom)} GB over — it would spill "
                                   f"into system RAM and crawl")
    return {**need, "vram_gb": vram_gb, "usable_gb": round(usable, 1),
            "reserved_gb": reserve, "verdict": verdict,
            "headroom_gb": headroom, "why": why}


def recommend(vram_gb: float, wants: list = None) -> list:
    """What's worth pulling for this card, largest that still fits first."""
    wants = [w.lower() for w in (wants or [])]
    out = []
    for fam in CATALOGUE:
        if wants and not any(w in " ".join(fam["good_at"]).lower()
                             for w in wants):
            continue
        best = None
        for size in sorted(fam["sizes"], reverse=True):
            f = fits(size, vram_gb)
            if f["verdict"] in ("comfortable", "tight"):
                best = (size, f)
                break
        if best:
            size, f = best
            out.append({"model": f"{fam['name']}:{size}b",
                        "family": fam["name"], "params_b": size,
                        "licence": fam["licence"], "good_at": fam["good_at"],
                        "note": fam["note"], "fit": f["verdict"],
                        "needs_gb": f["total_gb"], "why": f["why"]})
    out.sort(key=lambda m: -m["params_b"])
    return out


# --------------------------------------------------------------------------- #
#  deriving your own engine, without training
# --------------------------------------------------------------------------- #
def build_modelfile(base: str, system: str = "", temperature: float = None,
                    context: int = None, stop: list = None) -> str:
    """The recipe for a derived model.

    This is what "pivoting from an existing one" usually means in practice:
    the same weights, fixed behaviour. It costs nothing and takes seconds."""
    lines = [f"FROM {base}"]
    if context:
        lines.append(f"PARAMETER num_ctx {int(context)}")
    if temperature is not None:
        lines.append(f"PARAMETER temperature {float(temperature)}")
    for s in (stop or [])[:6]:
        lines.append(f'PARAMETER stop "{s}"')
    if system:
        esc = system.replace('"""', '"')
        lines.append(f'SYSTEM """{esc}"""')
    return "\n".join(lines) + "\n"


def derive(name: str, base: str, system: str = "", temperature: float = None,
           context: int = None, stop: list = None, runner=None) -> dict:
    """Create a named variant of an existing model."""
    name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", (name or "").strip()).strip("-")
    if not name:
        return {"ok": False, "error": "give the new engine a name"}
    if not (base or "").strip():
        return {"ok": False, "error": "say which model to build on"}
    mf = build_modelfile(base, system, temperature, context, stop)
    path = _dir() / f"{name}.Modelfile"
    path.write_text(mf, "utf-8")
    if runner is None:
        runner = _ollama_create
    try:
        out = runner(name, mf)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "modelfile": str(path)}
    _remember(name, base, system, temperature, context)
    return {"ok": True, "name": name, "base": base,
            "modelfile": str(path), "detail": out,
            "note": (f"“{name}” is now a model on this machine. Add it as an "
                     f"engine in Engines, or pin it to a feature.")}


def _ollama_create(name: str, modelfile: str) -> str:
    import httpx
    with httpx.Client(timeout=600.0) as c:
        r = c.post(f"{OLLAMA}/api/create",
                   json={"model": name, "modelfile": modelfile})
        r.raise_for_status()
        return r.text[-400:]


def _remember(name, base, system, temperature, context) -> None:
    p = _dir() / "derived.json"
    try:
        items = json.loads(p.read_text("utf-8"))
    except Exception:
        items = []
    items = [i for i in items if i.get("name") != name]
    items.append({"name": name, "base": base, "system": (system or "")[:400],
                  "temperature": temperature, "context": context,
                  "at": _iso()})
    p.write_text(json.dumps(items, indent=2), "utf-8")


def derived() -> list:
    try:
        return json.loads((_dir() / "derived.json").read_text("utf-8"))
    except Exception:
        return []


# --------------------------------------------------------------------------- #
#  the training route, described honestly
# --------------------------------------------------------------------------- #
def training_plan(base: str, params_b: float, vram_gb: float,
                  examples: int = 500) -> dict:
    """What a LoRA fine-tune would actually take on this machine.

    Written as a plan rather than a button because it is hours of GPU time on
    a dataset you have to build, and because this module cannot verify any of
    it from here — that claim would be worth nothing."""
    # QLoRA in 4-bit: weights + optimiser states for the adapters only
    weights = params_b * 1e9 * 4.5 / 8 / 1e9
    training_overhead = max(4.0, params_b * 0.45)
    need = round(weights + training_overhead, 1)
    ok = need <= vram_gb
    hours = round(max(0.4, examples / 1000 * params_b * 0.35), 1)
    return {
        "base": base, "params_b": params_b,
        "needs_gb": need, "vram_gb": vram_gb, "feasible": ok,
        "verdict": (f"QLoRA on {params_b}B needs about {need} GB; you have "
                    f"{vram_gb} GB — "
                    + ("that works." if ok else
                       "that doesn't fit. Drop to a smaller base.")),
        "rough_hours": hours,
        "dataset": {
            "format": "JSONL, one object per line: "
                      '{"messages":[{"role":"user","content":"..."},'
                      '{"role":"assistant","content":"..."}]}',
            "how_many": "500–2000 examples is the usual range. Below ~200 it "
                        "learns the style and not much else.",
            "warning": ("Fine-tuning teaches tone and format far more "
                        "reliably than it teaches facts. If you want it to "
                        "know things, retrieval over your documents beats "
                        "training, costs nothing, and can be corrected."),
        },
        "steps": [
            "Build the dataset as JSONL — this is most of the work.",
            "pip install unsloth peft trl bitsandbytes",
            "Run the generated script; watch loss stop falling.",
            "Export to GGUF, then `ollama create` it as an engine.",
            "Run Engine evals against the feature you built it for — a "
            "fine-tune that scores worse than the base is a common outcome "
            "and worth catching before you rely on it.",
        ],
        "honest_note": ("Most people asking for a fine-tune want a Modelfile "
                        "variant: same weights, fixed system prompt and "
                        "settings, ready in seconds. Try that first — if it "
                        "does the job, the training run was never needed."),
    }


def training_script(base: str, out_name: str, dataset_path: str,
                    params_b: float = 8) -> str:
    """A runnable QLoRA script, sized for a 24GB card."""
    return f'''# QLoRA fine-tune — generated for {base} ({params_b}B)
# Run on the machine with the GPU. This was NOT executed or verified here.
#   pip install unsloth peft trl bitsandbytes datasets
from unsloth import FastLanguageModel
from trl import SFTTrainer
from transformers import TrainingArguments
from datasets import load_dataset

MAX_SEQ = 2048
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name="{base}",
    max_seq_length=MAX_SEQ,
    load_in_4bit=True,          # QLoRA: the whole point of fitting on 24GB
)
model = FastLanguageModel.get_peft_model(
    model, r=16, lora_alpha=16, lora_dropout=0,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                    "gate_proj", "up_proj", "down_proj"],
    use_gradient_checkpointing="unsloth",
)

data = load_dataset("json", data_files={dataset_path!r}, split="train")

trainer = SFTTrainer(
    model=model, tokenizer=tokenizer, train_dataset=data,
    max_seq_length=MAX_SEQ,
    args=TrainingArguments(
        per_device_train_batch_size=2,
        gradient_accumulation_steps=4,      # effective batch 8
        warmup_steps=10,
        num_train_epochs=2,                 # more than 3 usually overfits
        learning_rate=2e-4,
        fp16=True,
        logging_steps=5,
        optim="adamw_8bit",
        output_dir="{out_name}-run",
    ),
)
trainer.train()

# Save, then convert for Ollama:
model.save_pretrained_gguf("{out_name}", tokenizer, quantization_method="q4_k_m")
print("Now run:  ollama create {out_name} -f ./{out_name}/Modelfile")
'''
