#!/usr/bin/env python3
"""Validates every set under sets/ and writes index.json from their set.json files.

Usage, from the repository root:
    python3 tools/build.py           # validate, then rewrite index.json
    python3 tools/build.py --check   # validate, and fail if index.json is out of date (CI)

Standard library only. Exit code 0 when everything is valid, 1 otherwise.
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SETS = ROOT / "sets"
INDEX = ROOT / "index.json"
FORMAT = 1

LANGUAGE = re.compile(r"^[a-z]{2}$")
SET_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LEVELS = {"A1", "A2", "B1", "B2", "C1", "C2"}
SOURCE_TYPES = {"json", "anki", "text"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".heic"}
SET_KEYS = {"title", "description", "languages", "image", "level", "tags", "updated", "source"}
FORM_KEYS = {"text", "alternates", "transcription", "partOfSpeech"}

errors: list[str] = []


def error(where: str, message: str) -> None:
    errors.append(f"{where}: {message}")


def is_remote(url: str) -> bool:
    return urlparse(url).scheme in ("http", "https")


def local_file(url: str, folder: Path, where: str) -> Path | None:
    """The file a relative URL in `folder` names; None for a remote or bad URL."""
    if is_remote(url):
        if urlparse(url).scheme != "https":
            error(where, f"remote URL must be https: {url}")
        return None
    if url.startswith("/") or urlparse(url).scheme:
        error(where, f"URL must be relative to the set folder or absolute https: {url}")
        return None
    path = (folder / url).resolve()
    if folder.resolve() not in path.parents:
        error(where, f"URL points outside the set folder: {url}")
        return None
    if not path.is_file():
        error(where, f"file not found: {url}")
        return None
    return path


def index_url(url: str, set_id: str) -> str:
    """A set-relative URL as index.json needs it: relative to the repository root."""
    return url if is_remote(url) else f"sets/{set_id}/{url}"


def check_image(value, folder: Path, where: str) -> None:
    if not isinstance(value, str) or not value:
        error(where, "image must be a non-empty string")
        return
    path = local_file(value, folder, where)
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
    unknown = set(value) - FORM_KEYS
    if unknown:
        error(where, f"unknown keys {sorted(unknown)}")


def check_words_json(path: Path, languages: list[str]) -> int:
    where = str(path.relative_to(ROOT))
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        error(where, f"invalid JSON: {exc}")
        return 0
    if document.get("format") != FORMAT:
        error(where, f"format must be {FORMAT}")
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
            check_image(word["image"], path.parent, at)
    return len(words)


def check_words_text(path: Path, columns: list[str]) -> int:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for number, line in enumerate(lines, start=1):
        cells = line.split("\t")
        if len(cells) != len(columns) or not all(cell.strip() for cell in cells):
            error(f"{path.relative_to(ROOT)} line {number}", f"expected {len(columns)} non-empty tab-separated cells")
    return len(lines)


def build_entry(folder: Path) -> dict | None:
    """Validates one set folder and returns its index.json entry."""
    set_id = folder.name
    where = f"sets/{set_id}"
    if not SET_ID.match(set_id):
        error(where, "folder name is the set id and must be lowercase kebab-case")
        return None
    try:
        meta = json.loads((folder / "set.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        error(where, "set.json is missing")
        return None
    except json.JSONDecodeError as exc:
        error(where, f"set.json is invalid JSON: {exc}")
        return None
    where += "/set.json"

    unknown = set(meta) - SET_KEYS
    if unknown:
        error(where, f"unknown keys {sorted(unknown)}")
    check_localized(meta.get("title"), f"{where} title", required=True)
    check_localized(meta.get("description"), f"{where} description", required=False)
    languages = check_languages(meta.get("languages"), where)
    if "image" in meta:
        check_image(meta["image"], folder, where)
    if "level" in meta and meta["level"] not in LEVELS:
        error(where, f"level must be one of {sorted(LEVELS)}")
    if "tags" in meta and not (isinstance(meta["tags"], list) and all(isinstance(t, str) for t in meta["tags"])):
        error(where, "tags must be a list of strings")
    if "updated" in meta and not (isinstance(meta["updated"], str) and DATE.match(meta["updated"])):
        error(where, "updated must be YYYY-MM-DD")

    source = meta.get("source")
    if not isinstance(source, dict) or source.get("type") not in SOURCE_TYPES:
        error(where, f"source.type must be one of {sorted(SOURCE_TYPES)}")
        return None
    url = source.get("url")
    if not isinstance(url, str) or not url:
        error(where, "source.url is required")
        return None
    path = local_file(url, folder, where)

    count = None
    kind = source["type"]
    if kind == "json":
        if is_remote(url):
            error(where, "json words must live in the set folder")
        elif path:
            count = check_words_json(path, languages)
    elif kind == "text":
        columns = source.get("columns")
        if not isinstance(columns, list) or len(columns) != 2 or sorted(columns) != sorted(languages):
            error(where, "text source needs \"columns\": the two set languages in column order")
        elif path:
            count = check_words_text(path, columns)
    elif kind == "anki":
        if len(languages) != 2:
            error(where, "an anki set has exactly two languages, in the order of the note fields")
        if path and path.suffix.lower() != ".apkg":
            error(where, "anki source must be an .apkg file")
        if "deck" in source and not isinstance(source["deck"], str):
            error(where, "source.deck must be a string")

    entry = {"id": set_id, **{key: meta[key] for key in ("title", "description", "languages") if key in meta}}
    if "image" in meta:
        entry["image"] = index_url(meta["image"], set_id)
    for key in ("level", "tags"):
        if key in meta:
            entry[key] = meta[key]
    if count is not None:
        entry["wordCount"] = count
    if "updated" in meta:
        entry["updated"] = meta["updated"]
    entry["source"] = {**source, "url": index_url(url, set_id)}
    return entry


def build_index() -> dict:
    folders = sorted(p for p in SETS.iterdir() if p.is_dir()) if SETS.is_dir() else []
    entries = [entry for entry in map(build_entry, folders) if entry]
    return {"format": FORMAT, "sets": entries}


def render(index: dict) -> str:
    return json.dumps(index, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    text = render(build_index())

    if not errors:
        if check_only:
            current = INDEX.read_text(encoding="utf-8") if INDEX.is_file() else ""
            if current != text:
                error("index.json", "out of date; run python3 tools/build.py and commit the result")
        else:
            INDEX.write_text(text, encoding="utf-8")

    for message in errors:
        print(message)
    sets = len(json.loads(text)["sets"])
    print(f"{sets} sets, {len(errors)} errors" + ("" if errors or check_only else ", index.json written"))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
