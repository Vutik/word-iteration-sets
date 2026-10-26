#!/usr/bin/env python3
"""Lists the words of some sets for picture generation: one line per word, tab-separated —
file name, English text, part of speech, Russian text. Written to tools/images/.cache/words.tsv.

    python3 tools/images/words.py food-and-drinks feelings
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = Path(__file__).resolve().parent / ".cache"


def text(form):
    return form if isinstance(form, str) else (form or {}).get("text", "")


def main(set_ids):
    CACHE.mkdir(exist_ok=True)
    lines, seen = [], set()
    for set_id in set_ids:
        data = json.loads((ROOT / "sets" / set_id / "words.json").read_text(encoding="utf-8"))
        for word in data["words"] if isinstance(data, dict) else data:
            forms = word["forms"]
            en = text(forms.get("en"))
            if not en:
                continue
            name = re.sub(r"[^a-z0-9]+", "-", en.lower()).strip("-")
            if name in seen:
                continue
            seen.add(name)
            pos = forms["en"].get("partOfSpeech", "") if isinstance(forms["en"], dict) else ""
            lines.append("\t".join([name, en, pos, text(forms.get("ru"))]))
    (CACHE / "words.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(lines)} words → {CACHE / 'words.tsv'}")


if __name__ == "__main__":
    main(sys.argv[1:])
