#!/bin/bash
# Server-side environment setup for CSS experiment.
# Creates a uv venv (Python 3.12) and installs numpy / torch(cu121) /
# sentence-transformers / faiss-cpu. Idempotent: re-running is safe.
set -e

PROJ=/home/wushang/workspace/skills_evolve
UV=/home/wushang/.local/bin/uv
cd "$PROJ"

echo "=== [1/4] Creating venv (Python 3.12) ==="
"$UV" venv --python 3.12 .venv

echo "=== [2/4] Installing torch (CPU-only — embeddings run on CPU) ==="
"$UV" pip install --python .venv/bin/python \
  "torch==2.4.1" --index-url https://download.pytorch.org/whl/cpu

echo "=== [3/4] Installing numpy / sentence-transformers / faiss-cpu / openpyxl / pandas ==="
"$UV" pip install --python .venv/bin/python \
  numpy "sentence-transformers>=2.7,<4" faiss-cpu openpyxl pandas

echo "=== [4/4] Verifying imports ==="
.venv/bin/python - <<'PY'
import numpy; print("numpy", numpy.__version__)
import torch
print("torch", torch.__version__, "| cuda_available", torch.cuda.is_available())
import sentence_transformers as st; print("sentence-transformers", st.__version__)
import faiss; print("faiss OK")
import openpyxl; print("openpyxl", openpyxl.__version__)
import pandas; print("pandas", pandas.__version__)
PY
echo "=== Setup complete ==="
