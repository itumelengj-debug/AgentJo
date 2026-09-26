"""Quick sanity-check REPL for a fine-tuned model — before deploying it.

Loads the base model plus your LoRA adapter via transformers (no Ollama
needed) so you can eyeball whether the fine-tune actually picked up your
style, procedures, and terminology.

Usage:
    python training/chat_test.py --base-model Qwen/Qwen2.5-1.5B-Instruct \
        --adapter runs/v1/adapter
    python training/chat_test.py --merged runs/v1/merged
"""

import argparse


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-model", default="Qwen/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--adapter", help="path to LoRA adapter dir")
    ap.add_argument("--merged", help="path to merged model dir (overrides the above)")
    ap.add_argument("--max-new-tokens", type=int, default=512)
    args = ap.parse_args()

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    src = args.merged or args.base_model
    tokenizer = AutoTokenizer.from_pretrained(args.adapter or src)
    model = AutoModelForCausalLM.from_pretrained(
        src, torch_dtype="auto",
        device_map="auto" if torch.cuda.is_available() else None)
    if args.adapter and not args.merged:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, args.adapter)
    model.eval()

    print(f"loaded {src}" + (f" + adapter {args.adapter}" if args.adapter and not args.merged else ""))
    print("type a message (ctrl-c to quit)\n")
    history = []
    while True:
        try:
            user = input("you › ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            break
        if not user:
            continue
        history.append({"role": "user", "content": user})
        inputs = tokenizer.apply_chat_template(
            history, add_generation_prompt=True, return_tensors="pt"
        ).to(model.device)
        with torch.no_grad():
            out = model.generate(inputs, max_new_tokens=args.max_new_tokens,
                                 do_sample=True, temperature=0.7, top_p=0.9,
                                 pad_token_id=tokenizer.eos_token_id)
        reply = tokenizer.decode(out[0][inputs.shape[1]:], skip_special_tokens=True)
        print(f"\n{reply}\n")
        history.append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    main()
