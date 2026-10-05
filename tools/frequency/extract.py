#!/usr/bin/env python3
"""Streams one language's Wiktionary extract from kaikki.org and keeps what the frequency sets
need, so the multi-gigabyte file never lands on disk.

Usage, from the repository root:
    python3 tools/frequency/extract.py pl      # writes tools/frequency/.cache/pl.jsonl.gz

Per entry it keeps the headword, part of speech, the glosses of its first senses, the lemmas a
form-of entry points to, every inflected form, and — for English only — the translations into
the app's languages. Data: English Wiktionary via kaikki.org (wiktextract), CC BY-SA 4.0.
Standard library only.
"""

import gzip
import json
import sys
import urllib.request
from pathlib import Path

LANGUAGES = {
    "en": "English", "ru": "Russian", "pl": "Polish", "uk": "Ukrainian",
    "de": "German", "es": "Spanish", "fr": "French", "it": "Italian",
}
CACHE = Path(__file__).resolve().parent / ".cache"
SENSES = 4
GENDERS = {"masculine", "feminine", "neuter", "plural"}


def url(code: str) -> str:
    name = LANGUAGES[code]
    return f"https://kaikki.org/dictionary/{name}/kaikki.org-dictionary-{name}.jsonl"


def translations(entry: dict) -> list[dict]:
    """Translations into the app's languages, each with the sense it belongs to. Newer extracts
    keep them per sense, older ones on the entry."""
    found = []
    for sense in entry.get("senses", []):
        for item in sense.get("translations", []):
            found.append({**item, "sense": item.get("sense") or "; ".join(sense.get("glosses", []))})
    found += entry.get("translations", [])
    return [
        {"lang": t["lang_code"], "word": t["word"], "sense": t.get("sense", ""),
         "gender": [g for g in t.get("tags", []) if g in GENDERS]}
        for t in found
        if t.get("lang_code") in LANGUAGES and t.get("lang_code") != "en" and t.get("word")
    ]


def compact(entry: dict, code: str) -> dict:
    senses = entry.get("senses", [])
    kept = {
        "word": entry["word"],
        "pos": entry.get("pos", ""),
        "glosses": [g for s in senses[:SENSES] for g in s.get("glosses", [])[-1:]],
        "tags": sorted({t for s in senses[:SENSES] for t in s.get("tags", [])}),
    }
    form_of = [f["word"] for s in senses for key in ("form_of", "alt_of") for f in s.get(key, []) if f.get("word")]
    if form_of:
        kept["formOf"] = sorted(set(form_of))
    forms = sorted({
        f["form"] for f in entry.get("forms", [])
        if f.get("form") and not {"table-tags", "inflection-template", "romanization"} & set(f.get("tags", []))
    })
    if forms:
        kept["forms"] = forms
    if code == "en":
        found = translations(entry)
        if found:
            kept["translations"] = found
    return kept


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in LANGUAGES:
        print(f"usage: extract.py <{'|'.join(LANGUAGES)}>")
        return 1
    code = sys.argv[1]
    CACHE.mkdir(exist_ok=True)
    target = CACHE / f"{code}.jsonl.gz"
    partial = target.with_suffix(".partial")
    count = 0
    with urllib.request.urlopen(url(code)) as response, gzip.open(partial, "wt", encoding="utf-8") as out:
        for line in response:
            entry = json.loads(line)
            if entry.get("lang_code") != code:
                continue
            out.write(json.dumps(compact(entry, code), ensure_ascii=False) + "\n")
            count += 1
    partial.rename(target)
    print(f"{code}: {count} entries → {target.relative_to(Path.cwd()) if target.is_relative_to(Path.cwd()) else target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
