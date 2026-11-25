#!/usr/bin/env python3
"""Merge the ATC LoRA into whisper-large-v3-turbo (float32) and save a HF dir
that convert-h5-to-ggml.py can read.

usage: python -I merge.py BASE_DIR LORA_DIR OUT_DIR
"""
import os
import shutil
import sys

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import torch
from peft import PeftModel
from transformers import WhisperForConditionalGeneration

TOKENIZER_FILES = (
    "vocab.json", "added_tokens.json", "merges.txt", "normalizer.json",
    "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json",
    "preprocessor_config.json",
)


def main(base_dir: str, lora_dir: str, out_dir: str) -> int:
    torch.manual_seed(0)
    model = WhisperForConditionalGeneration.from_pretrained(
        base_dir, torch_dtype=torch.float32)
    model = PeftModel.from_pretrained(model, lora_dir).merge_and_unload()
    model.save_pretrained(out_dir, safe_serialization=True)
    for f in TOKENIZER_FILES:
        shutil.copyfile(os.path.join(base_dir, f), os.path.join(out_dir, f))
    print("merged ->", out_dir)
    for f in sorted(os.listdir(out_dir)):
        print("   ", f, os.path.getsize(os.path.join(out_dir, f)))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    sys.exit(main(*sys.argv[1:]))
