# Agent Jo — Dependency & Model License Inventory

*Prepared as a commercialization aid. This is a map of what to verify, not legal
advice — confirm everything against the canonical source at release time, and
have a lawyer review the model-provider terms and your employment IP position.*

## How to read this

Agent Jo ships **your own code** (app.py, the `agent/` package, your avatar
image) plus a set of **third-party Python libraries**, and at runtime it talks to
**external model providers** (cloud APIs) and **local models** (pulled by Ollama
or downloaded on first use). Each layer has different licensing:

- **Python libraries** — their open-source license governs whether you can ship
  them. Verdict below: all permissive, no copyleft.
- **Cloud models (Claude, DeepSeek, Mistral, NVIDIA)** — you don't ship these;
  your right to build a commercial product on them is set by each provider's
  **commercial terms of service + usage policy**. This is the real gating layer.
- **Local models (Qwen, nomic-embed, Whisper)** — governed by each **model
  card's license**. Mostly permissive, but Qwen cards vary by size/version and
  must be checked individually.

---

## 1. Python dependencies

Licenses below come from the installed package metadata (runtime deps) or the
package's well-established license (training deps, not installed in this check).
Confirm against your **pinned** versions before release.

| Package | Declared | License | Commercial OK | Used for |
| --- | --- | --- | --- | --- |
| anthropic | >=0.40 | MIT | Yes | Claude API SDK |
| openai | >=1.40 | Apache-2.0 | Yes | All OpenAI-compatible engines (DeepSeek, Mistral, custom) |
| rich | >=13.7 | MIT | Yes | Console output |
| pypdf | >=4.0 | BSD-3-Clause | Yes | PDF text extraction |
| openpyxl | >=3.1 | MIT | Yes | .xlsx reading |
| python-docx | >=1.1 | MIT | Yes | .docx reading |
| gradio | >=4.44 | Apache-2.0 | Yes | Web UI (ships its own front-end bundle, also permissive) |
| faster-whisper | >=1.0 | MIT | Yes | Local speech-to-text (downloads Whisper weights, MIT) |
| torch | >=2.3 | BSD-3-Clause | Yes | Fine-tuning only |
| transformers | >=4.46 | Apache-2.0 | Yes | Fine-tuning only |
| datasets | >=3.0 | Apache-2.0 | Yes | Fine-tuning only |
| peft | >=0.13 | Apache-2.0 | Yes | Fine-tuning only |
| trl | >=0.12 | Apache-2.0 | Yes | Fine-tuning only |
| accelerate | >=1.0 | Apache-2.0 | Yes | Fine-tuning only |
| bitsandbytes | >=0.44 | MIT | Yes | Fine-tuning only (Linux/CUDA) |

**Verdict:** no GPL/LGPL/AGPL or other copyleft in the declared set — nothing
forces you to open-source your code. Apache-2.0 packages carry a patent grant and
a notice requirement; MIT/BSD require preserving the copyright notice.

> **Action — transitive dependencies.** The table is the *top level*. Gradio and
> the training stack each pull a large tree of sub-dependencies. Before selling,
> run a license scan on the frozen environment, e.g.:
> ```
> pip install pip-licenses
> pip-licenses --format=markdown --with-urls --with-license-file
> ```
> Review for any copyleft that slipped in transitively, then bundle a
> `THIRD-PARTY-NOTICES` file containing every license text.

---

## 2. Models & providers

You do **not** ship model weights — cloud models are API calls; local models are
pulled by the user's Ollama. So the question per model is "what are we allowed to
do commercially," answered by the provider/card, not by your code license.

| Model | How Agent Jo reaches it | Governed by | Commercial note |
| --- | --- | --- | --- |
| Claude (Sonnet / Haiku / Opus) | Anthropic API | Anthropic Commercial Terms + Usage Policy | Commercial use allowed under a commercial account; don't imply Anthropic endorsement; follow the usage policy. |
| DeepSeek V4 Pro / Flash | DeepSeek, NVIDIA, or OpenRouter API | The chosen provider's terms | Provider-dependent. **NVIDIA's free tier is for evaluation, not production** — you hit this already with the 429s. |
| Mistral Large (`mistral-large-latest`) | Mistral API | Mistral API commercial terms | **API use is commercial-OK.** But the *open weights* of Mistral Large are under a non-commercial research license — do **not** self-host those weights in a commercial product without a separate license. Keep "API" and "weights" separate. |
| Any custom engine you add | User-entered endpoint | That provider's terms | If customers bring their own keys, this is largely their responsibility — but say so in your terms. |
| qwen2.5-coder:32b (local) | Ollama | Qwen model-card license | **Verify the exact card.** Most Qwen2.5 sizes are Apache-2.0, but some Qwen licenses have historically carried usage-scale clauses. Check before shipping. |
| qwen3.x (local) | Ollama | Qwen3 model-card license | Qwen3 was released under Apache-2.0 — but confirm the precise tag you ship/recommend. |
| nomic-embed-text (local) | Ollama | Apache-2.0 | OK for commercial use. |
| Whisper (voice STT) | faster-whisper download | MIT (OpenAI) | OK for commercial use. |

**Two distinctions that matter most:**

1. **API access vs. open weights.** For Mistral and DeepSeek, *calling the API*
   is governed by the provider's commercial terms (generally fine for a paid
   product). *Self-hosting the open weights* is governed by the weight license,
   which can be non-commercial. Don't conflate them in marketing or architecture.
2. **Free/eval tiers are not production licenses.** NVIDIA's free NIM tier and
   most "free" API keys are for evaluation. A commercial product needs paid,
   production-grade accounts with the matching terms.

---

## 3. Bundled assets & fonts

| Asset | Source | License / note |
| --- | --- | --- |
| `agent_avatar.png`, `atlas.ico` | Your own illustration | Your IP — fine to use commercially. |
| Inter, JetBrains Mono (UI fonts) | Fetched from Google Fonts at runtime by Gradio | Both SIL Open Font License — commercial-OK. Note they load from a **Google CDN at runtime**, which is an external network call (a privacy/offline consideration; bundle them locally if you need fully offline or zero-third-party-calls). |
| "Segoe UI" (primary UI font) | User's operating system | System font, not bundled — no license obligation on you. |
| Your application code | You | Your copyright; you choose the product license/EULA. |

---

## 4. Pre-sale checklist (licensing only)

1. Run `pip-licenses` on the **frozen** production venv; resolve anything
   non-permissive in the transitive tree.
2. Pull and archive the **license text of every Ollama model** you ship or
   recommend (Qwen especially — per tag).
3. Obtain **paid/commercial accounts** for every cloud provider you depend on,
   and read each one's commercial + acceptable-use terms.
4. Ship a **`THIRD-PARTY-NOTICES`** file containing all bundled library licenses
   (Apache-2.0/MIT/BSD all require the notice to travel with the software).
5. Decide the **keys model**: customers supply their own provider keys
   (lighter for you) vs. you host and resell access (you must hold and comply
   with every provider's reseller/commercial terms).
6. If you bundle the fonts locally instead of the CDN, include their **OFL**
   license files too.

---

## Caveats

- **Not legal advice.** I'm summarizing licenses to help you scope the work; a
  qualified lawyer should review the provider terms and your employment-IP
  position before you commercialize.
- **Versions matter.** The library licenses reflect the versions checked; a
  different pinned version could differ. Re-scan at release.
- **Provider/model terms change.** Cloud commercial terms and model-card licenses
  are updated over time and some details here may post-date or pre-date the
  current version — verify each against its official source at release time.
