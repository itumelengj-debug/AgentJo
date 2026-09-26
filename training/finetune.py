"""Fine-tune a local open-weights model on your agent's data (LoRA/QLoRA).

This is the weight-update step. It does NOT touch Claude (API models can't
be fine-tuned by you); it trains a student model you own, which the agent
can then run via  --backend ollama.

Why LoRA instead of full fine-tuning:
  - trains ~1% of parameters → fits on consumer hardware
  - the output is a small adapter (~30–60MB) saved like any checkpoint —
    keep dated versions, diff them, roll back, exactly as with .pt files
  - the base model stays pristine; a bad run costs you nothing

Hardware guide (rough):
  GPU ≥ 8GB VRAM      → 7B model, QLoRA 4-bit          (the sweet spot)
  GPU 4–8GB / Apple   → 1.5B–3B model, fp16 LoRA
  CPU only            → 0.5B–1.5B model, slow but works

Usage:
    pip install -r requirements-training.txt
    python training/finetune.py --dataset data/distilled.jsonl --out runs/v1
    python training/finetune.py --dataset data/best.jsonl --out runs/v2 \
        --base-model Qwen/Qwen2.5-7B-Instruct --epochs 3 --merge

Then test:   python training/chat_test.py --adapter runs/v1/adapter
Then deploy: bash training/deploy_ollama.sh runs/v1/merged atlas-tuned
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True, help="chat-format .jsonl")
    ap.add_argument("--out", required=True, help="output dir for this run")
    ap.add_argument("--base-model", default="Qwen/Qwen2.5-1.5B-Instruct",
                    help="HF model id; scale up to Qwen/Qwen2.5-7B-Instruct on a GPU")
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument("--grad-accum", type=int, default=8)
    ap.add_argument("--max-seq-len", type=int, default=2048)
    ap.add_argument("--lora-r", type=int, default=16)
    ap.add_argument("--no-4bit", action="store_true",
                    help="disable QLoRA 4-bit even on CUDA")
    ap.add_argument("--merge", action="store_true",
                    help="also save a merged full model (needed for Ollama export)")
    args = ap.parse_args()

    # Heavy imports after argparse so --help is instant.
    import torch
    from datasets import load_dataset
    from peft import LoraConfig, PeftModel, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from trl import SFTTrainer
    try:
        from trl import SFTConfig
    except ImportError:
        SFTConfig = None  # older trl: fall back to TrainingArguments
        from transformers import TrainingArguments

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- data --------------------------------------------------------- #
    ds = load_dataset("json", data_files=args.dataset, split="train")
    if len(ds) < 20:
        print(f"WARNING: only {len(ds)} examples — expect minimal effect. "
              f"50+ is a sensible floor, a few hundred is better.")

    tokenizer = AutoTokenizer.from_pretrained(args.base_model)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    def to_text(example):
        return {"text": tokenizer.apply_chat_template(
            example["messages"], tokenize=False)}

    ds = ds.map(to_text, remove_columns=[c for c in ds.column_names if c != "text"])
    print(f"dataset: {len(ds)} examples | base: {args.base_model}")

    # ---- model (+ optional 4-bit quantisation on CUDA) ----------------- #
    use_cuda = torch.cuda.is_available()
    quant_config = None
    if use_cuda and not args.no_4bit:
        try:
            from transformers import BitsAndBytesConfig
            quant_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_use_double_quant=True,
            )
            print("QLoRA: loading base in 4-bit (bitsandbytes)")
        except ImportError:
            print("bitsandbytes unavailable — continuing without 4-bit")

    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        quantization_config=quant_config,
        torch_dtype="auto",
        device_map="auto" if use_cuda else None,
    )
    model.config.use_cache = False  # incompatible with gradient checkpointing

    lora = LoraConfig(
        r=args.lora_r, lora_alpha=args.lora_r * 2, lora_dropout=0.05,
        bias="none", task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
    )

    # ---- training config (handles trl API differences) ----------------- #
    common = dict(
        output_dir=str(out_dir / "checkpoints"),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        warmup_ratio=0.05,
        logging_steps=5,
        save_strategy="epoch",
        bf16=use_cuda and torch.cuda.is_bf16_supported(),
        fp16=use_cuda and not torch.cuda.is_bf16_supported(),
        gradient_checkpointing=use_cuda,
        report_to=[],
    )
    if SFTConfig is not None:
        try:
            train_cfg = SFTConfig(**common, max_seq_length=args.max_seq_len,
                                  dataset_text_field="text", packing=False)
            trainer = SFTTrainer(model=model, args=train_cfg,
                                 train_dataset=ds, peft_config=lora,
                                 processing_class=tokenizer)
        except TypeError:  # trl moved/renamed kwargs across versions
            train_cfg = SFTConfig(**common)
            trainer = SFTTrainer(model=model, args=train_cfg, train_dataset=ds,
                                 peft_config=lora, tokenizer=tokenizer,
                                 dataset_text_field="text",
                                 max_seq_length=args.max_seq_len)
    else:
        train_cfg = TrainingArguments(**common)
        trainer = SFTTrainer(model=model, args=train_cfg, train_dataset=ds,
                             peft_config=lora, tokenizer=tokenizer,
                             dataset_text_field="text",
                             max_seq_length=args.max_seq_len)

    # ---- train + save adapter checkpoint -------------------------------- #
    trainer.train()
    adapter_dir = out_dir / "adapter"
    trainer.save_model(str(adapter_dir))
    tokenizer.save_pretrained(str(adapter_dir))
    (out_dir / "run.json").write_text(json.dumps({
        "base_model": args.base_model, "dataset": args.dataset,
        "examples": len(ds), "epochs": args.epochs, "lora_r": args.lora_r,
        "finished": datetime.now().isoformat(timespec="seconds"),
    }, indent=2))
    print(f"\nadapter checkpoint saved → {adapter_dir}")

    # ---- optional merge (base + adapter → standalone model) ------------- #
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
              f"deploy with:  bash training/deploy_ollama.sh {merged_dir} atlas-tuned")
    else:
        print("tip: rerun with --merge when you're ready to deploy to Ollama, "
              "or test the adapter first:\n"
              f"  python training/chat_test.py --base-model {args.base_model} "
              f"--adapter {adapter_dir}")


if __name__ == "__main__":
    main()
