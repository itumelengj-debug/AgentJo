"""Preference fine-tuning from your ratings (KTO).

Every /good and /bad you give is logged. export_dataset.py --kto turns those
into a labelled dataset; this script trains the local model to produce more
of what you upvoted and less of what you downvoted.

Why KTO and not DPO: DPO needs *pairs* — a better and worse answer to the
SAME prompt. Your thumbs land on different prompts, so you'd have almost no
pairs. KTO (Kahneman-Tversky Optimization) learns from UNPAIRED binary
feedback — exactly the signal /good and /bad produce. (If you later add a
"regenerate and pick the better one" feature, you'd get true DPO pairs as a
bonus; KTO needs nothing extra.)

This trains the same open-weights student as finetune.py and is fully local;
it does not and cannot fine-tune the Claude API model. Run an SFT pass first
(finetune.py) to set style, then KTO to refine toward your preferences —
either from the base model or, better, from a previous merged checkpoint.

Usage:
    pip install -r requirements-training.txt
    python training/export_dataset.py --kto --out data/prefs.jsonl
    python training/finetune_kto.py --dataset data/prefs.jsonl --out runs/kto-v1 \
        --base-model runs/2026-06-09/merged --merge
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True, help="KTO .jsonl from export_dataset.py --kto")
    ap.add_argument("--out", required=True, help="output dir for this run")
    ap.add_argument("--base-model", default="Qwen/Qwen2.5-1.5B-Instruct",
                    help="HF id or a previous merged/ checkpoint to refine")
    ap.add_argument("--epochs", type=float, default=1.0)
    ap.add_argument("--lr", type=float, default=5e-6)   # KTO likes a low LR
    ap.add_argument("--beta", type=float, default=0.1)
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument("--grad-accum", type=int, default=8)
    ap.add_argument("--max-seq-len", type=int, default=2048)
    ap.add_argument("--lora-r", type=int, default=16)
    ap.add_argument("--no-4bit", action="store_true")
    ap.add_argument("--merge", action="store_true",
                    help="also save a merged model (for Ollama deployment)")
    args = ap.parse_args()

    import torch
    from datasets import load_dataset
    from peft import LoraConfig, PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer
    try:
        from trl import KTOConfig, KTOTrainer
    except ImportError:
        sys.exit("This TRL version lacks KTOTrainer. Upgrade:  "
                 "pip install -U 'trl>=0.12'")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    ds = load_dataset("json", data_files=args.dataset, split="train")
    labels = [bool(x) for x in ds["label"]]
    pos, neg = sum(labels), len(labels) - sum(labels)
    print(f"dataset: {len(ds)} rated examples ({pos} desirable / {neg} undesirable)")
    if pos == 0 or neg == 0:
        print("WARNING: KTO strongly prefers BOTH classes present. Collect a "
              "mix of /good and /bad first, or results will be unstable.")
    if len(ds) < 30:
        print(f"WARNING: only {len(ds)} examples — preference tuning needs "
              f"signal; gather more ratings before expecting an effect.")

    tokenizer = AutoTokenizer.from_pretrained(args.base_model)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    use_cuda = torch.cuda.is_available()
    quant_config = None
    if use_cuda and not args.no_4bit:
        try:
            from transformers import BitsAndBytesConfig
            quant_config = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_use_double_quant=True)
            print("QLoRA: loading base in 4-bit")
        except ImportError:
            print("bitsandbytes unavailable — continuing without 4-bit")

    model = AutoModelForCausalLM.from_pretrained(
        args.base_model, quantization_config=quant_config, torch_dtype="auto",
        device_map="auto" if use_cuda else None)
    model.config.use_cache = False

    lora = LoraConfig(
        r=args.lora_r, lora_alpha=args.lora_r * 2, lora_dropout=0.05,
        bias="none", task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"])

    cfg = KTOConfig(
        output_dir=str(out_dir / "checkpoints"),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        beta=args.beta,
        lr_scheduler_type="cosine",
        warmup_ratio=0.05,
        logging_steps=5,
        save_strategy="epoch",
        max_length=args.max_seq_len,
        max_prompt_length=args.max_seq_len // 2,
        bf16=use_cuda and torch.cuda.is_bf16_supported(),
        fp16=use_cuda and not torch.cuda.is_bf16_supported(),
        gradient_checkpointing=use_cuda,
        report_to=[])

    trainer = KTOTrainer(model=model, args=cfg, train_dataset=ds,
                         processing_class=tokenizer, peft_config=lora)
    trainer.train()

    adapter_dir = out_dir / "adapter"
    trainer.save_model(str(adapter_dir))
    tokenizer.save_pretrained(str(adapter_dir))
    (out_dir / "run.json").write_text(json.dumps({
        "method": "KTO", "base_model": args.base_model, "dataset": args.dataset,
        "examples": len(ds), "desirable": pos, "undesirable": neg,
        "epochs": args.epochs, "beta": args.beta, "lr": args.lr,
        "finished": datetime.now().isoformat(timespec="seconds"),
    }, indent=2))
    print(f"\nKTO adapter saved → {adapter_dir}")

    if args.merge:
        print("merging adapter into base weights...")
        del model, trainer
        if use_cuda:
            torch.cuda.empty_cache()
        base = AutoModelForCausalLM.from_pretrained(
            args.base_model, torch_dtype=torch.float16,
            device_map="auto" if use_cuda else None)
        merged = PeftModel.from_pretrained(base, str(adapter_dir)).merge_and_unload()
        merged_dir = out_dir / "merged"
        merged.save_pretrained(str(merged_dir))
        tokenizer.save_pretrained(str(merged_dir))
        print(f"merged model → {merged_dir}\n"
              f"deploy:  bash training/deploy_ollama.sh {merged_dir} atlas-pref")


if __name__ == "__main__":
    main()
