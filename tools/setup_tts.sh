#!/usr/bin/env bash
# Set up the dispatch voice (Kokoro-82M text-to-speech) that sizeup's TTS server runs on.
# Idempotent: run it again any time; finished steps are skipped.
#
#   tools/setup_tts.sh                     # -> ~/.local/share/sizeup/tts/{venv,python,hf}
#   SIZEUP_TTS_HOME=/elsewhere tools/setup_tts.sh
#   SIZEUP_TTS_SEED=/path/to/eval/tts tools/setup_tts.sh   # reuse an earlier download (its uv cache and
#                                                          # cache/hf Kokoro weights) instead of fetching again
#
# What it makes (outside the repo, ~1.1 GB): a Python 3.12 venv (kokoro, misaki, torch: CPU is used),
# the Kokoro-82M weights and the English voices pinned to one Hugging Face revision. sizeup then
# starts sizeup/tts_server.py with that venv's Python ([dispatch] tts_python, the default path).
set -euo pipefail

TTS_HOME="${SIZEUP_TTS_HOME:-$HOME/.local/share/sizeup/tts}"
SEED="${SIZEUP_TTS_SEED:-}"
REPO_ID="hexgrad/Kokoro-82M"
REVISION="f3ff3571791e39611d31c381e3a41a3af07b4987"   # Kokoro v1.0, as evaluated
PY_VERSION="3.12"
PACKAGES=(
  "kokoro==0.9.4" "misaki[en]==0.9.4" "torch==2.14.1" "numpy==2.5.3" "soundfile==0.14.0" "huggingface-hub==1.33.0"
  "en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
)

say() { printf '[setup_tts] %s\n' "$*"; }
mkdir -p "$TTS_HOME"

# 1. uv: on PATH, or our own copy (bootstrapped once with any python3 that has venv + pip).
UV="$(command -v uv || true)"
if [[ -z "$UV" && -x "$TTS_HOME/tools/bin/uv" ]]; then UV="$TTS_HOME/tools/bin/uv"; fi
if [[ -z "$UV" ]]; then
  BOOT="$(command -v python3.14 || command -v python3.13 || command -v python3 || true)"
  [[ -n "$BOOT" ]] || { say "need uv or a python3 to install it with"; exit 1; }
  say "installing uv into $TTS_HOME/tools (with $BOOT)"
  "$BOOT" -m venv "$TTS_HOME/tools"
  "$TTS_HOME/tools/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade uv
  UV="$TTS_HOME/tools/bin/uv"
fi
say "uv: $UV ($("$UV" --version))"

export UV_PYTHON_INSTALL_DIR="$TTS_HOME/python"
if [[ -n "$SEED" && -d "$SEED/cache/uv" && -z "${UV_CACHE_DIR:-}" ]]; then
  export UV_CACHE_DIR="$SEED/cache/uv"        # packages already downloaded there (copied, not linked)
  export UV_LINK_MODE=copy
  say "reusing package cache $UV_CACHE_DIR"
fi

# 2. Python 3.12 + the venv (kept under TTS_HOME; nothing is put on PATH).
"$UV" python install "$PY_VERSION" --no-bin --quiet
if [[ ! -x "$TTS_HOME/venv/bin/python" ]]; then
  say "creating $TTS_HOME/venv"
  "$UV" venv --quiet --python "$PY_VERSION" --managed-python "$TTS_HOME/venv"
fi
PY="$TTS_HOME/venv/bin/python"

# 3. Packages (pinned; a no-op when already installed).
"$UV" pip install --quiet --python "$PY" "${PACKAGES[@]}"

# 4. Weights + voices. An earlier download (SIZEUP_TTS_SEED) is copied first; whatever is missing
#    (the model, the English voices) is then fetched for the pinned revision.
export HF_HOME="$TTS_HOME/hf"
HUB="$HF_HOME/hub"
mkdir -p "$HUB"
SEED_MODEL="$SEED/cache/hf/hub/models--hexgrad--Kokoro-82M"
if [[ -n "$SEED" && -d "$SEED_MODEL" && ! -d "$HUB/models--hexgrad--Kokoro-82M" ]]; then
  say "copying Kokoro weights from $SEED_MODEL"
  cp -R "$SEED_MODEL" "$HUB/"
fi
"$PY" -I - "$REPO_ID" "$REVISION" <<'EOF'
import sys
from huggingface_hub import snapshot_download
repo, rev = sys.argv[1], sys.argv[2]
# American (a) and British (b) English voices: the ones the dispatch voice can use.
path = snapshot_download(repo, revision=rev, allow_patterns=["config.json", "kokoro-v1_0.pth", "voices/a[fm]_*.pt",
                                                              "voices/b[fm]_*.pt"])
print("[setup_tts] model:", path)
EOF
# The pinned revision is what `main` resolves to offline.
echo -n "$REVISION" > "$HUB/models--hexgrad--Kokoro-82M/refs/main"

# 5. Check it speaks, offline, on CPU.
HF_HUB_OFFLINE=1 "$PY" -I - <<'EOF'
import time, warnings
warnings.filterwarnings("ignore")
t = time.time()
from kokoro import KModel, KPipeline
m = KModel(repo_id="hexgrad/Kokoro-82M").to("cpu").eval()
p = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M", model=m)
n = sum(len(a) for _, _, a in p("Port Chester Fire, Engine sixty.", voice="af_heart", speed=0.92))
assert n > 24000 * 0.5, n
print(f"[setup_tts] check: {n / 24000:.1f} s of speech, {time.time() - t:.1f} s to load and speak")
EOF

say "ready: $PY ($(du -sh "$TTS_HOME" | cut -f1) in $TTS_HOME)"
say "config: [dispatch] tts_python = \"$PY\" (the default when TTS_HOME is the default)"
