# Fine-tuning your agent (actual weight updates)

The agent now has two brains. The Claude API brain cannot be fine-tuned — its weights live on Anthropic's servers. The Ollama brain runs an open-weights model on your machine, and **that one you can train**. The architecture is teacher–student distillation:

```
  daily use                    weekly-ish batch job
┌─────────────────┐   ┌──────────────────────────────────────────┐
│ Claude (teacher) │   │ 1. export_dataset.py  ← conversation log │
│ + memory/skills  │──►│ 2. distill.py         ← Claude rewrites  │
│ logs everything  │   │    replies into gold answers + synthesises│
└─────────────────┘   │    Q→A from memories & skills             │
                      │ 3. finetune.py        ← LoRA weight update│
                      │ 4. chat_test.py       ← eyeball it        │
                      │ 5. deploy_ollama.sh   ← GGUF → ollama     │
                      └──────────────────┬───────────────────────┘
                                         ▼
                      python run.py --backend ollama   (student brain)
```

## Two kinds of training

**SFT (`finetune.py`)** teaches *what good looks like* — style, format, procedures — from distilled gold answers. Do this first.

**KTO (`finetune_kto.py`)** refines toward *your* preferences using the `/good` and `/bad` thumbs you've given. KTO learns from unpaired binary feedback, which is exactly what thumbs produce — DPO would need a better-and-worse pair for the same prompt, which you rarely have. Run KTO after SFT, ideally starting from the SFT `merged/` checkpoint:

```bash
python training/export_dataset.py --kto --out data/prefs.jsonl
python training/finetune_kto.py --dataset data/prefs.jsonl \
    --out runs/kto-v1 --base-model runs/2026-06-09/merged --merge
```

KTO wants both classes present — rate a mix of good and bad answers, not only good ones. A low learning rate (default 5e-6) and one epoch is the right starting point; preference tuning destabilises fast if overdone.

## The complete (SFT) workflow

```bash
# 0. one-time
pip install -r requirements-training.txt          # torch, transformers, peft, trl...
# install Ollama from https://ollama.com, then:  ollama pull qwen2.5:7b

# 1. collect data — just use the agent normally. Rate good answers with /good.

# 2. build the training set (pick one or both)
python training/export_dataset.py --out data/best.jsonl --only-rated
python training/distill.py --from-log --from-memory --out data/distilled.jsonl

# 3. train (LoRA — updates ~1% of weights, saves a small adapter)
python training/finetune.py --dataset data/distilled.jsonl --out runs/2026-06-09 \
    --base-model Qwen/Qwen2.5-7B-Instruct --epochs 2 --merge

# 4. sanity-check before deploying
python training/chat_test.py --merged runs/2026-06-09/merged

# 5. deploy to Ollama and run the agent on it
bash training/deploy_ollama.sh runs/2026-06-09/merged atlas-tuned
AGENT_OLLAMA_MODEL=atlas-tuned python run.py --backend ollama
```

## Set expectations honestly

**What fine-tuning is good at:** your tone and format, your terminology, recurring procedures, how you like answers structured, domain framing. After a few hundred distilled examples the student stops sounding generic and starts sounding like *your* assistant.

**What it is bad at:** storing facts. A model trained on 300 examples will still misremember the specific deadline, the exact counterparty name, the current limit. This is why memory retrieval stays ON regardless of backend — facts live in the database where they're exact and editable, behaviour lives in the weights. Don't fight this; it's how every serious deployment splits the problem.

**What it will never do:** make a 7B student reason like the Claude teacher. Fine-tuning transfers style and procedure, not raw capability. The realistic outcome is a private, fast, increasingly *you-shaped* local model — excellent for routine asks, with the API brain a `--backend` flag away for hard ones.

## Hardware

| You have | Base model | Mode | Feel |
|---|---|---|---|
| NVIDIA ≥ 12GB VRAM | Qwen2.5-7B-Instruct | QLoRA 4-bit (auto) | minutes–an hour |
| NVIDIA 6–8GB | Qwen2.5-3B / 1.5B | QLoRA 4-bit | fast |
| Apple Silicon ≥ 16GB | Qwen2.5-1.5B/3B | fp16 LoRA (auto) | acceptable |
| CPU only | Qwen2.5-0.5B/1.5B | LoRA | slow; start tiny |

The script auto-detects CUDA/Mac/CPU and degrades gracefully. Qwen2.5 Instruct models are Apache-2.0 and tool-capable, which the agent's Ollama backend uses. Swap in any chat model on the HF hub via `--base-model`.

## Cadence and versioning (the checkpoint discipline)

Do **not** train after every conversation. Online updates on tiny batches cause catastrophic forgetting — the model overwrites general ability with your last ten chats. The sane pattern is batch: accumulate a week or month of rated/distilled data, train once, evaluate, deploy.

Each run directory is a versioned checkpoint, the same mental model as your `.pt` files: `adapter/` (the LoRA weights, ~30–60MB — this is the thing you version), `merged/` (base+adapter flattened for deployment), and `run.json` (base model, dataset, hyperparams, timestamp). Keep dated runs (`runs/2026-06-09`, `runs/2026-07-01`...); rolling back is just deploying an older directory. To resume or stack training, point `--base-model` at a previous `merged/` dir.

A minimal eval beats vibes: keep ~20 fixed prompts that matter to you, run them through old and new models (`chat_test.py`), and compare — or have Claude judge the pairs. Only promote a run that wins.

## Troubleshooting

CUDA OOM → smaller `--base-model`, `--max-seq-len 1024`, or `--batch-size 1 --grad-accum 16`. bitsandbytes errors on Mac/CPU → expected; it auto-falls back to fp16 LoRA. Student outputs garbage → almost always too little data (<50 examples) or too many epochs on a tiny set (memorisation); distill more, train 1–2 epochs. Ollama ignores tools → base model must be tool-capable (Qwen2.5/Qwen3, Llama 3.1+).
