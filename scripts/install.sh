#!/usr/bin/env bash
# Local UniRig install for an NVIDIA card, no sudo. Everything lives in ~/unirig (about 12 GB),
# apart from model weights (5.5 GB), which Hugging Face caches under ~/.cache/huggingface on first run.
# Run on Linux, or inside WSL on Windows:  bash scripts/install.sh   (safe to rerun)
#
# It differs from UniRig's own instructions because the RTX 50-series needs PyTorch 2.7.1 with
# CUDA 12.8 and Python 3.11. Tested on an RTX 5060 Ti 16 GB under WSL Ubuntu 24.04.
set -eo pipefail
T=${UNIRIG_HOME:-$HOME/unirig}
mkdir -p "$T/bin"
cd "$T"
stage() { echo; echo "=== STAGE: $* === $(date +%T)"; }

stage micromamba
[ -x bin/micromamba ] || curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xj bin/micromamba
export MAMBA_ROOT_PREFIX=$T/mamba

stage env
[ -x env/bin/python ] || bin/micromamba create -y -p "$T/env" -c conda-forge python=3.11 pip
export PATH=$T/env/bin:$PATH
export LD_LIBRARY_PATH=$T/env/lib:${LD_LIBRARY_PATH:-}

stage torch
pip install torch==2.7.1 torchvision==0.22.1 --index-url https://download.pytorch.org/whl/cu128
python -c "import torch;print(torch.__version__,torch.cuda.is_available(),torch.cuda.get_device_name(0))"

stage repo
# The commit this install was tested against. A later one may move what the patch below edits.
UNIRIG_COMMIT=6793c6640ff01c8fb389f3993434124bb43d2933
if [ ! -d UniRig ]; then
  rm -rf UniRig.partial
  git init -q UniRig.partial
  git -C UniRig.partial fetch -q --depth 1 https://github.com/VAST-AI-Research/UniRig.git "$UNIRIG_COMMIT"
  git -C UniRig.partial checkout -q FETCH_HEAD
  mv UniRig.partial UniRig
fi
git -C UniRig rev-parse HEAD

stage requirements
# flash_attn has no source build without the CUDA toolkit; it is installed from a wheel below.
grep -viE '^\s*flash[_-]attn' UniRig/requirements.txt > requirements.filtered.txt
pip install -r requirements.filtered.txt

stage sparse
pip install spconv-cu120
pip install torch_scatter torch_cluster -f https://data.pyg.org/whl/torch-2.7.0+cu128.html --no-cache-dir
pip install numpy==1.26.4

stage flash_attn
# The skin model imports flash_attn. Take a prebuilt wheel for PyTorch 2.7 / Python 3.11.
if ! python -c 'import flash_attn' 2>/dev/null; then
  for v in 2.8.3 2.8.2 2.8.1 2.8.0.post2; do
    url="https://github.com/Dao-AILab/flash-attention/releases/download/v$v/flash_attn-$v+cu12torch2.7cxx11abiTRUE-cp311-cp311-linux_x86_64.whl"
    pip install -q "$url" && python -c 'import flash_attn' && break
  done
fi
python -c 'import flash_attn; print("flash_attn", flash_attn.__version__)'

stage patch
# PyTorch 2.6+ loads checkpoints in safe mode and rejects UniRig's skin checkpoint, which stores
# its config as a box.Box. Allow just that class instead of switching safe loading off.
f=$T/UniRig/run.py
grep -q 'add_safe_globals' "$f" || sed -i '1i import torch, box.box\ntorch.serialization.add_safe_globals([box.box.Box])' "$f"
head -2 "$f"

stage verify
python - <<'EOF'
import torch, numpy, spconv, torch_scatter, torch_cluster
print('torch', torch.__version__, torch.cuda.get_device_name(0), 'numpy', numpy.__version__, 'spconv', spconv.__version__)
EOF
echo "=== INSTALL DONE === $(date +%T)"
