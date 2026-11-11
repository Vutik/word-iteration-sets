#!/usr/bin/env python3
"""Scores every downloaded candidate for nudity with a local image classifier, so `library.py
screen` can hide what a title or a category does not give away (a nude back called "Back view of
a man"). Nothing leaves the Mac.

    python3 -m venv tools/images/.cache/venv
    tools/images/.cache/venv/bin/pip install torch transformers pillow
    tools/images/.cache/venv/bin/python tools/images/nsfw.py      # then: library.py screen

Model: Falconsai/nsfw_image_detection (ViT, Apache 2.0), labels "normal" / "nsfw". Scores go to
tools/images/nsfw.json, {original URL: P(nsfw)} — in git, so a score is never computed twice.
"""
import json
import sys
from pathlib import Path

import torch
from PIL import Image
from transformers import pipeline

HERE = Path(__file__).resolve().parent
LIBRARY = HERE / ".cache" / "library"
SCORES = HERE / "nsfw.json"
MODEL = "Falconsai/nsfw_image_detection"
BATCH = 32


def save(scores):
    partial = SCORES.with_suffix(".json.partial")
    partial.write_text(json.dumps(scores, indent=0, sort_keys=True) + "\n", encoding="utf-8")
    partial.replace(SCORES)


def main():
    library = json.loads((LIBRARY / "candidates.json").read_text(encoding="utf-8"))
    scores = json.loads(SCORES.read_text(encoding="utf-8")) if SCORES.exists() else {}
    todo = list({c["remote"]: c["file"] for e in library.values() for c in e["candidates"]
                 if c["remote"] not in scores}.items())
    print(f"{len(todo)} pictures to score ({len(scores)} already)", flush=True)
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    classify = pipeline("image-classification", model=MODEL, device=device)
    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        images, keys = [], []
        for remote, file in chunk:
            try:
                images.append(Image.open(LIBRARY / file).convert("RGB"))
                keys.append(remote)
            except Exception as exc:
                print(f"  ! {file}: {exc}", flush=True)
        if not images:
            continue
        for remote, result in zip(keys, classify(images, batch_size=BATCH)):
            scores[remote] = round(next((r["score"] for r in result if r["label"] == "nsfw"), 0.0), 4)
        if (start // BATCH) % 20 == 0:
            save(scores)
            print(f"[{min(start + BATCH, len(todo))}/{len(todo)}]", flush=True)
    save(scores)
    high = sum(1 for s in scores.values() if s >= 0.8)
    print(f"done: {len(scores)} scored, {high} at 0.8 or more", flush=True)


if __name__ == "__main__":
    sys.exit(main())
