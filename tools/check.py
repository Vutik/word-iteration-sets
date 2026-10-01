#!/usr/bin/env python3
"""Validates index.json and every set it points to. Standard library only.

Usage: python3 tools/check.py            # from the repository root
Exit code 0 when everything is valid, 1 otherwise.
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
LANGUAGE = re.compile(r"^[a-z]{2}$")
SET_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LEVELS = {"A1", "A2", "B1", "B2", "C1", "C2"}
SOURCE_TYPES = {"json", "anki", "text"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".heic"}

errors: list[str] = []


def error(where: str, message: str) -> None:
    errors.append(f"{where}: {message}")


def is_remote(url: str) -> bool:
    return urlparse(url).scheme in ("http", "https")


def local_file(url: str, relative_to: Path, where: str) -> Path | None:
    """Resolves a relative URL against the file it appears in; None for remote URLs."""
    if is_remote(url):
        if urlparse(url).scheme != "https":
            error(where, f"remote URL must be https: {url}")
        return None
    if url.startswith("/") or urlparse(url).scheme:
        error(where, f"URL must be relative to the file or absolute https: {url}")
        return None
    path = (relative_to.parent / url).resolve()
    if ROOT not in path.parents:
        error(where, f"URL points outside the repository: {url}")
        return None
    if not path.is_file():
        error(where, f"file not found: {url}")
        return None
    return path


def check_image(value, relative_to: Path, where: str) -> None:
    if not isinstance(value, str) or not value:
        error(where, "image must be a non-empty string")
        return
    path = local_file(value, relative_to, where)
    if path and path.suffix.lower() not in IMAGE_SUFFIXES:
        error(where, f"image must be one of {sorted(IMAGE_SUFFIXES)} (iOS cannot show SVG): {value}")


def check_localized(value, where: str, required: bool) -> None:
    if value is None and not required:
        return
    if not isinstance(value, dict) or not value:
        error(where, "must be an object of language code → text")
        return
    for code, text in value.items():
        if not LANGUAGE.match(code):
            error(where, f"bad language code {code!r}")
        if not isinstance(text, str) or not text.strip():
            error(where, f"empty text for {code!r}")
    if "en" not in value:
        error(where, "must include \"en\" as the fallback")


def check_languages(value, where: str) -> list[str]:
    if not isinstance(value, list) or len(value) < 2:
        error(where, "languages must list at least two codes")
        return []
    for code in value:
        if not isinstance(code, str) or not LANGUAGE.match(code):
            error(where, f"bad language code {code!r}")
    if len(set(value)) != len(value):
        error(where, "languages repeat")
    return [code for code in value if isinstance(code, str)]


def check_form(value, where: str) -> None:
    if isinstance(value, str):
        if not value.strip():
            error(where, "empty text")
        return
    if not isinstance(value, dict):
        error(where, "form must be a string or an object with \"text\"")
        return
    if not isinstance(value.get("text"), str) or not value["text"].strip():
        error(where, "form needs non-empty \"text\"")
    alternates = value.get("alternates", [])
    if not isinstance(alternates, list) or not all(isinstance(a, str) and a.strip() for a in alternates):
        error(where, "alternates must be a list of non-empty strings")
    for key in ("transcription", "partOfSpeech"):
        if key in value and not isinstance(value[key], str):
            error(where, f"{key} must be a string")
    unknown = set(value) - {"text", "alternates", "transcription", "partOfSpeech"}
    if unknown:
        error(where, f"unknown keys {sorted(unknown)}")


def check_json_set(path: Path, entry_id: str, languages: list[str], where: str) -> int:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        error(where, f"invalid JSON in {path.relative_to(ROOT)}: {exc}")
        return 0
    where = str(path.relative_to(ROOT))
    if document.get("format") != 1:
        error(where, "format must be 1")
    if document.get("id") != entry_id:
        error(where, f"id {document.get('id')!r} differs from index entry {entry_id!r}")
    if document.get("languages") != languages:
        error(where, "languages must match the index entry exactly")
    words = document.get("words")
    if not isinstance(words, list) or not words:
        error(where, "words must be a non-empty list")
        return 0
    for number, word in enumerate(words, start=1):
        at = f"{where} word {number}"
        forms = word.get("forms") if isinstance(word, dict) else None
        if not isinstance(forms, dict):
            error(at, "needs \"forms\"")
            continue
        missing = [code for code in languages if code not in forms]
        if missing:
            error(at, f"missing forms for {missing}")
        extra = [code for code in forms if code not in languages]
        if extra:
            error(at, f"forms for languages not in the set: {extra}")
        for code, form in forms.items():
            check_form(form, f"{at} [{code}]")
        for example in word.get("examples", []):
            if not isinstance(example, dict) or example.get("language") not in languages:
                error(at, "example needs a \"language\" from the set")
                continue
            if not isinstance(example.get("text"), str) or not example["text"].strip():
                error(at, "example needs non-empty \"text\"")
            translations = example.get("translations", {})
            if not isinstance(translations, dict) or any(code not in languages for code in translations):
                error(at, "example translations must be keyed by set languages")
        if "image" in word:
            check_image(word["image"], path, at)
    return len(words)


def check_text_set(path: Path, columns: list[str], where: str) -> int:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for number, line in enumerate(lines, start=1):
        if len(line.split("\t")) != len(columns):
            error(f"{path.relative_to(ROOT)} line {number}", f"expected {len(columns)} tab-separated columns")
    return len(lines)


def check_entry(entry, index_path: Path, seen: set[str]) -> None:
    entry_id = entry.get("id") if isinstance(entry, dict) else None
    where = f"index.json set {entry_id or '?'}"
    if not isinstance(entry_id, str) or not SET_ID.match(entry_id):
        error(where, "id must be lowercase kebab-case")
        return
    if entry_id in seen:
        error(where, "id is not unique")
    seen.add(entry_id)

    check_localized(entry.get("title"), f"{where} title", required=True)
    check_localized(entry.get("description"), f"{where} description", required=False)
    languages = check_languages(entry.get("languages"), where)
    if "image" in entry:
        check_image(entry["image"], index_path, where)
    if "level" in entry and entry["level"] not in LEVELS:
        error(where, f"level must be one of {sorted(LEVELS)}")
    if "tags" in entry and not (isinstance(entry["tags"], list) and all(isinstance(t, str) for t in entry["tags"])):
        error(where, "tags must be a list of strings")
    if "updated" in entry and not (isinstance(entry["updated"], str) and DATE.match(entry["updated"])):
        error(where, "updated must be YYYY-MM-DD")

    source = entry.get("source")
    if not isinstance(source, dict) or source.get("type") not in SOURCE_TYPES:
        error(where, f"source.type must be one of {sorted(SOURCE_TYPES)}")
        return
    url = source.get("url")
    if not isinstance(url, str) or not url:
        error(where, "source.url is required")
        return
    path = local_file(url, index_path, where)

    counted = None
    kind = source["type"]
    if kind == "json":
        if is_remote(url):
            error(where, "json sets must live in this repository")
        elif path:
            counted = check_json_set(path, entry_id, languages, where)
    elif kind == "text":
        columns = source.get("columns")
        if not isinstance(columns, list) or len(columns) != 2 or sorted(columns) != sorted(languages):
            error(where, "text source needs \"columns\": the two set languages in column order")
        elif path:
            counted = check_text_set(path, columns, where)
    elif kind == "anki":
        if path and path.suffix.lower() != ".apkg":
            error(where, "anki source must be an .apkg file")
        if "deck" in source and not isinstance(source["deck"], str):
            error(where, "source.deck must be a string")

    if "wordCount" in entry:
        if not isinstance(entry["wordCount"], int) or entry["wordCount"] < 1:
            error(where, "wordCount must be a positive integer")
        elif counted is not None and counted != entry["wordCount"]:
            error(where, f"wordCount is {entry['wordCount']} but the source has {counted}")


def main() -> int:
    index_path = ROOT / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"index.json: {exc}")
        return 1
    if index.get("format") != 1:
        error("index.json", "format must be 1")
    sets = index.get("sets")
    if not isinstance(sets, list):
        error("index.json", "sets must be a list")
        sets = []
    seen: set[str] = set()
    for entry in sets:
        check_entry(entry, index_path, seen)

    for message in errors:
        print(message)
    print(f"{len(seen)} sets, {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
