#!/usr/bin/env bash
# Deploy a merged fine-tuned model to Ollama so the agent can use it.
#
#   bash training/deploy_ollama.sh runs/v1/merged atlas-tuned
#
# Steps: HF model dir → GGUF (llama.cpp) → quantise → `ollama create`.
# Requires: git, cmake, python with llama.cpp's requirements, ollama.
set -euo pipefail

MERGED_DIR="${1:?usage: deploy_ollama.sh <merged_model_dir> <ollama_name>}"
NAME="${2:?usage: deploy_ollama.sh <merged_model_dir> <ollama_name>}"
QUANT="${3:-Q4_K_M}"   # good quality/size balance for local inference

if [ ! -d "llama.cpp" ]; then
  echo "==> cloning llama.cpp (one-time)"
  git clone --depth 1 https://github.com/ggerganov/llama.cpp
  pip install -r llama.cpp/requirements.txt
  cmake -S llama.cpp -B llama.cpp/build && cmake --build llama.cpp/build -j --target llama-quantize
fi

echo "==> converting to GGUF"
python llama.cpp/convert_hf_to_gguf.py "$MERGED_DIR" --outfile "${NAME}-f16.gguf" --outtype f16

echo "==> quantising to ${QUANT}"
./llama.cpp/build/bin/llama-quantize "${NAME}-f16.gguf" "${NAME}.gguf" "${QUANT}"

echo "==> registering with Ollama as '${NAME}'"
# The chat template is embedded in the GGUF during conversion; Ollama reads it.
printf 'FROM ./%s.gguf\nPARAMETER temperature 0.7\n' "${NAME}" > Modelfile
ollama create "${NAME}" -f Modelfile

echo
echo "Done. Run your agent on the fine-tuned brain:"
echo "  AGENT_OLLAMA_MODEL=${NAME} python run.py --backend ollama"
