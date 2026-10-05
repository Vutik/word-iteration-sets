#!/usr/bin/env python3
"""Everyday work on the sets from the command line: look, search, add, edit, replace.

Usage, from the repository root (every command takes --help):
    python3 tools/sets.py list                         sets with languages and word counts
    python3 tools/sets.py show home [--lang en,ru]     a set's words, numbered
    python3 tools/sets.py find дом [--lang ru]         a word in every set
    python3 tools/sets.py add home en=window ru=окно pl=okno ...
    python3 tools/sets.py edit home 3 ru="окно;оконце" en.pos=noun
    python3 tools/sets.py edit home window ru+=оконце de-=Fenster
    python3 tools/sets.py remove home 3
    python3 tools/sets.py move home 3 school
    python3 tools/sets.py replace "la casa" "la vivienda" --lang es [--set home] [--dry-run]
    python3 tools/sets.py new kitchen-tools --title en="Kitchen tools" ru="Кухонная утварь" ...
    python3 tools/sets.py missing [--lang uk]          words that lack a language of their set
    python3 tools/sets.py conflicts [--lang ru]        one word translated differently in two sets
    python3 tools/sets.py build                        validate and rewrite index.json

A word is picked by its number in `show` or by any of its wordings in any language. In
`lang=text`, `;` separates wordings: the first is the text, the rest are alternates.
`lang+=text` adds an alternate, `lang-=text` removes one, `lang=` drops the language,
`lang.pos=` and `lang.tr=` set part of speech and transcription. Changing a set's words
bumps its `updated` date. Only json sets are edited; text and Anki sets are read-only here.
Standard library only.
"""

import argparse
import datetime
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETS = ROOT / "sets"
LANGUAGES = ["en", "ru", "pl", "uk", "de", "es", "fr", "it"]


# MARK: - Text

STRESS = re.compile(r"(?<=[\u0400-\u04FF])[\u0301\u0300]")


def unstressed(text: str) -> str:
    """Without the stress marks of Cyrillic dictionaries (`свобо́дный` → `свободный`). Only after
    a Cyrillic letter: decomposed, `ć` and `é` carry the same mark."""
    return unicodedata.normalize("NFC", STRESS.sub("", unicodedata.normalize("NFD", text)))


def plain(text: str) -> str:
    """Lowercase, without Cyrillic stress marks. `ё`, `й` and Latin diacritics stay."""
    return unstressed(text).lower().strip()


ARTICLES = re.compile(r"^(der|die|das|el|la|los|las|le|les|il|lo|gli|i|l'|l’|to)\s*", re.IGNORECASE)


def bare(text: str) -> str:
    """For matching: plain, without a leading article or `to`: `das Haus` → `haus`."""
    return ARTICLES.sub("", plain(text)).strip()


def texts(form) -> list[str]:
    """Every wording of a form, the text first."""
    if isinstance(form, str):
        return [form]
    return [form["text"], *form.get("alternates", [])]


def make_form(old, wordings: list[str]):
    """A form with these wordings, keeping the old one's transcription and part of speech, as
    a bare string when there is nothing else — the way the files are written by hand."""
    extra = {k: v for k, v in (old or {}).items() if k in ("transcription", "partOfSpeech")} if isinstance(old, dict) else {}
    if len(wordings) == 1 and not extra:
        return wordings[0]
    form = {"text": wordings[0]}
    if wordings[1:]:
        form["alternates"] = wordings[1:]
    return {**form, **extra}


def short(form) -> str:
    return " / ".join(texts(form))


# MARK: - Sets

class WordSet:
    def __init__(self, set_id: str):
        self.id = set_id
        self.folder = SETS / set_id
        if not (self.folder / "set.json").is_file():
            sys.exit(f"no set {set_id!r} (python3 tools/sets.py list)")
        self.meta = json.loads((self.folder / "set.json").read_text(encoding="utf-8"))
        self.languages = self.meta.get("languages", [])
        source = self.meta.get("source", {})
        self.words_path = self.folder / source.get("url", "words.json") if source.get("type") == "json" else None
        self.document = (
            json.loads(self.words_path.read_text(encoding="utf-8"))
            if self.words_path and self.words_path.is_file() else {"format": 1, "words": []}
        )

    @property
    def words(self) -> list[dict]:
        return self.document["words"]

    @property
    def title(self) -> str:
        return self.meta.get("title", {}).get("en", self.id)

    def require_json(self):
        if not self.words_path:
            sys.exit(f"{self.id} is a {self.meta.get('source', {}).get('type')} set; only json sets are edited here")

    def find(self, selector: str) -> int:
        """The index of the word `selector` names: a number from `show`, or a wording."""
        if selector.isdigit():
            index = int(selector) - 1
            if not 0 <= index < len(self.words):
                sys.exit(f"{self.id} has {len(self.words)} words, no #{selector}")
            return index
        key = bare(selector)
        hits = [i for i, word in enumerate(self.words)
                if any(bare(t) == key for form in word["forms"].values() for t in texts(form))]
        if not hits:
            sys.exit(f"no word {selector!r} in {self.id}")
        if len(hits) > 1:
            sys.exit(f"{selector!r} is words {', '.join(str(i + 1) for i in hits)} in {self.id}; pick one by number")
        return hits[0]

    def save(self):
        self.require_json()
        self.words_path.write_text(json.dumps(self.document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.meta["updated"] = datetime.date.today().isoformat()
        save_meta(self.folder, self.meta)


def save_meta(folder: Path, meta: dict):
    (folder / "set.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def all_sets() -> list[WordSet]:
    return [WordSet(p.name) for p in sorted(SETS.iterdir()) if (p / "set.json").is_file()]


def line(word: dict, languages: list[str]) -> str:
    forms = word["forms"]
    return "  ".join(f"{code}: {short(forms[code])}" for code in languages if code in forms)


def wanted_languages(option: str | None, default: list[str]) -> list[str]:
    return option.split(",") if option else default


# MARK: - Changes

ASSIGNMENT = re.compile(r"^([a-z]{2})(\.pos|\.tr)?(\+=|-=|=)(.*)$", re.DOTALL)


def apply(word: dict, assignments: list[str], languages: list[str]) -> list[str]:
    """Applies `lang=...` changes to a word; returns what changed, for the report."""
    forms = word.setdefault("forms", {})
    changes = []
    for assignment in assignments:
        match = ASSIGNMENT.match(assignment)
        if not match:
            sys.exit(f"cannot read {assignment!r}: write lang=text, lang+=text, lang-=text, lang.pos=… or lang.tr=…")
        code, field, operator, value = match.groups()
        if code not in languages:
            sys.exit(f"{code} is not a language of this set ({' '.join(languages)})")
        old = forms.get(code)
        if field:
            if old is None:
                sys.exit(f"no {code} form to set {field[1:]} on")
            key = "partOfSpeech" if field == ".pos" else "transcription"
            form = dict(old) if isinstance(old, dict) else {"text": old}
            if value:
                form[key] = value
            else:
                form.pop(key, None)
            forms[code] = make_form(form, texts(form))
        elif operator == "=":
            if value:
                forms[code] = make_form(old, [w.strip() for w in value.split(";") if w.strip()])
            else:
                forms.pop(code, None)
        elif operator == "+=":
            if old is None:
                sys.exit(f"no {code} form to add an alternate to; use {code}=…")
            if bare(value) not in [bare(t) for t in texts(old)]:
                forms[code] = make_form(old, [*texts(old), value.strip()])
        else:
            if old is None or bare(value) not in [bare(t) for t in texts(old)]:
                sys.exit(f"{value!r} is not a {code} wording of this word")
            kept = [t for t in texts(old) if bare(t) != bare(value)]
            if not kept:
                sys.exit(f"{value!r} is the only {code} wording; use {code}= to drop the language")
            forms[code] = make_form(old, kept)
        changes.append(f"{code}: {short(old) if old else '—'} → {short(forms[code]) if code in forms else '—'}")
    word["forms"] = {code: forms[code] for code in languages if code in forms}
    return changes


def duplicates(word: dict, wordset: WordSet, skip: int | None = None) -> list[str]:
    """Other words of the set that share a wording with `word` in the same language."""
    found = []
    for index, other in enumerate(wordset.words):
        if index == skip:
            continue
        for code, form in word["forms"].items():
            if code in other["forms"] and {bare(t) for t in texts(form)} & {bare(t) for t in texts(other["forms"][code])}:
                found.append(f"#{index + 1} ({code}: {short(other['forms'][code])})")
                break
    return found


# MARK: - Commands

def command_list(args):
    for wordset in all_sets():
        kind = wordset.meta.get("source", {}).get("type")
        count = len(wordset.words) if wordset.words_path else "—"
        print(f"{wordset.id:28} {' '.join(wordset.languages):24} {kind:5} {count:>5}  {wordset.title}")


def command_show(args):
    wordset = WordSet(args.set)
    languages = wanted_languages(args.lang, wordset.languages)
    for index, word in enumerate(wordset.words, start=1):
        print(f"{index:4}  {line(word, languages)}")


def command_find(args):
    key = bare(args.query)
    languages = wanted_languages(args.lang, LANGUAGES)
    shown = wanted_languages(args.show, LANGUAGES)
    hits = 0
    for wordset in all_sets():
        for index, word in enumerate(wordset.words, start=1):
            matched = any(
                (bare(t) == key if args.exact else key in bare(t))
                for code, form in word["forms"].items() if code in languages
                for t in texts(form)
            )
            if matched:
                hits += 1
                print(f"{wordset.id}#{index}  {line(word, shown)}")
    print(f"{hits} found")


def command_add(args):
    wordset = WordSet(args.set)
    wordset.require_json()
    word = {"forms": {}}
    apply(word, args.forms, wordset.languages)
    missing = [code for code in wordset.languages if code not in word["forms"]]
    if missing:
        sys.exit(f"every word needs all of {' '.join(wordset.languages)}; missing {' '.join(missing)}")
    same = duplicates(word, wordset)
    if same and not args.force:
        sys.exit(f"already in {wordset.id}: {', '.join(same)} (--force adds it anyway)")
    wordset.words.append(word)
    wordset.save()
    print(f"{wordset.id}#{len(wordset.words)}  {line(word, wordset.languages)}")


def command_edit(args):
    wordset = WordSet(args.set)
    wordset.require_json()
    index = wordset.find(args.word)
    changes = apply(wordset.words[index], args.changes, wordset.languages)
    wordset.save()
    for change in changes:
        print(f"{wordset.id}#{index + 1}  {change}")


def command_remove(args):
    wordset = WordSet(args.set)
    wordset.require_json()
    index = wordset.find(args.word)
    word = wordset.words.pop(index)
    wordset.save()
    print(f"removed {wordset.id}#{index + 1}  {line(word, wordset.languages)}")


def command_move(args):
    source, target = WordSet(args.set), WordSet(args.target)
    source.require_json()
    target.require_json()
    index = source.find(args.word)
    word = source.words[index]
    missing = [code for code in target.languages if code not in word["forms"]]
    if missing:
        sys.exit(f"{target.id} needs {' '.join(missing)}, which the word lacks")
    word = {**word, "forms": {code: word["forms"][code] for code in target.languages}}
    target.words.append(word)
    source.words.pop(index)
    source.save()
    target.save()
    print(f"{source.id}#{index + 1} → {target.id}#{len(target.words)}")


def command_replace(args):
    """Replaces a wording everywhere it is the whole wording (or, with --substring, part of
    one), in text and alternates."""
    languages = wanted_languages(args.lang, LANGUAGES)
    sets = [WordSet(args.set)] if args.set else [s for s in all_sets() if s.words_path]
    total = 0
    for wordset in sets:
        changed = False
        for index, word in enumerate(wordset.words, start=1):
            for code in languages:
                form = word["forms"].get(code)
                if form is None:
                    continue
                old = texts(form)
                if args.substring:
                    new = [t.replace(args.old, args.new) for t in old]
                else:
                    new = [args.new if t == args.old else t for t in old]
                if new != old:
                    word["forms"][code] = make_form(form, list(dict.fromkeys(new)))
                    changed = True
                    total += 1
                    print(f"{wordset.id}#{index}  {code}: {short(form)} → {short(word['forms'][code])}")
        if changed and not args.dry_run:
            wordset.save()
    print(f"{total} replaced" + (" (dry run, nothing written)" if args.dry_run else ""))


def localized(pairs: list[str]) -> dict:
    result = {}
    for pair in pairs:
        code, _, text = pair.partition("=")
        if not text:
            sys.exit(f"write {code}=text, not {pair!r}")
        result[code] = text
    return result


def command_new(args):
    folder = SETS / args.id
    if folder.exists():
        sys.exit(f"{args.id} exists")
    if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", args.id):
        sys.exit("the id is lowercase kebab-case")
    title = localized(args.title)
    if "en" not in title:
        sys.exit("give at least en=… in --title")
    meta = {"title": title}
    if args.description:
        meta["description"] = localized(args.description)
    meta["languages"] = args.languages.split(",")
    if args.level:
        meta["level"] = args.level
    if args.tags:
        meta["tags"] = args.tags.split(",")
    meta["updated"] = datetime.date.today().isoformat()
    meta["source"] = {"type": "json", "url": "words.json"}
    folder.mkdir(parents=True)
    save_meta(folder, meta)
    (folder / "words.json").write_text(json.dumps({"format": 1, "words": []}, indent=2) + "\n", encoding="utf-8")
    print(f"made sets/{args.id}; add words with: python3 tools/sets.py add {args.id} en=… ru=…")


def command_missing(args):
    languages = wanted_languages(args.lang, LANGUAGES)
    count = 0
    for wordset in all_sets():
        for index, word in enumerate(wordset.words, start=1):
            lacking = [c for c in wordset.languages if c in languages and c not in word["forms"]]
            if lacking:
                count += 1
                print(f"{wordset.id}#{index}  lacks {' '.join(lacking)}  {line(word, wordset.languages)}")
    print(f"{count} words")


def command_conflicts(args):
    """A wording that two sets translate differently into another language — often one of them
    is a slip, sometimes both are right and the sets mean different senses."""
    languages = wanted_languages(args.lang, LANGUAGES)
    seen: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    for wordset in all_sets():
        for index, word in enumerate(wordset.words, start=1):
            for code, form in word["forms"].items():
                seen.setdefault((code, bare(form if isinstance(form, str) else form["text"])), []).append(
                    (f"{wordset.id}#{index}", word["forms"]))
    count = 0
    for (code, key), places in sorted(seen.items()):
        if code not in languages or len({place.split("#")[0] for place, _ in places}) < 2:
            continue
        for other in LANGUAGES:
            if other == code:
                continue
            # Two words of one set that share a wording are two senses (`рука`: arm, hand);
            # across sets, translations with no wording in common are worth a look.
            clashes = set()
            for i, (place, forms) in enumerate(places):
                for later, later_forms in places[i + 1:]:
                    if place.split("#")[0] == later.split("#")[0] or other not in forms or other not in later_forms:
                        continue
                    if not {bare(t) for t in texts(forms[other])} & {bare(t) for t in texts(later_forms[other])}:
                        clashes |= {(place, short(forms[other])), (later, short(later_forms[other]))}
            if clashes:
                count += 1
                listed = "; ".join(f"{text} ({place})" for place, text in sorted(clashes))
                print(f"{code} {key!r} → {other}: {listed}")
    print(f"{count} conflicts")


def command_build(args):
    sys.exit(subprocess.call([sys.executable, str(ROOT / "tools" / "build.py")]))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="sets with languages and word counts").set_defaults(run=command_list)

    show = commands.add_parser("show", help="a set's words, numbered")
    show.add_argument("set")
    show.add_argument("--lang", help="languages to show, comma-separated")
    show.set_defaults(run=command_show)

    find = commands.add_parser("find", help="a word in every set (ignores case, stress, articles, `to`)")
    find.add_argument("query")
    find.add_argument("--lang", help="search only these languages, comma-separated")
    find.add_argument("--show", help="languages to print, comma-separated")
    find.add_argument("--exact", action="store_true", help="whole wording, not part of one")
    find.set_defaults(run=command_find)

    add = commands.add_parser("add", help="add a word: lang=text for every language of the set")
    add.add_argument("set")
    add.add_argument("forms", nargs="+")
    add.add_argument("--force", action="store_true", help="add even if the set has the word")
    add.set_defaults(run=command_add)

    edit = commands.add_parser("edit", help="change a word: lang=…, lang+=…, lang-=…, lang.pos=…, lang.tr=…")
    edit.add_argument("set")
    edit.add_argument("word", help="number from show, or any wording")
    edit.add_argument("changes", nargs="+")
    edit.set_defaults(run=command_edit)

    remove = commands.add_parser("remove", help="remove a word")
    remove.add_argument("set")
    remove.add_argument("word")
    remove.set_defaults(run=command_remove)

    move = commands.add_parser("move", help="move a word to another set")
    move.add_argument("set")
    move.add_argument("word")
    move.add_argument("target")
    move.set_defaults(run=command_move)

    replace = commands.add_parser("replace", help="replace a wording in every set")
    replace.add_argument("old")
    replace.add_argument("new")
    replace.add_argument("--lang", help="only these languages, comma-separated")
    replace.add_argument("--set", help="only this set")
    replace.add_argument("--substring", action="store_true", help="replace inside wordings too")
    replace.add_argument("--dry-run", action="store_true", help="print what would change")
    replace.set_defaults(run=command_replace)

    new = commands.add_parser("new", help="make an empty json set")
    new.add_argument("id")
    new.add_argument("--title", nargs="+", required=True, metavar="LANG=TEXT")
    new.add_argument("--description", nargs="+", metavar="LANG=TEXT")
    new.add_argument("--languages", default=",".join(LANGUAGES))
    new.add_argument("--level", choices=["A1", "A2", "B1", "B2", "C1", "C2"])
    new.add_argument("--tags", help="comma-separated")
    new.set_defaults(run=command_new)

    missing = commands.add_parser("missing", help="words that lack a language of their set")
    missing.add_argument("--lang", help="only these languages, comma-separated")
    missing.set_defaults(run=command_missing)

    conflicts = commands.add_parser("conflicts", help="one wording translated differently in two sets")
    conflicts.add_argument("--lang", help="only wordings in these languages, comma-separated")
    conflicts.set_defaults(run=command_conflicts)

    commands.add_parser("build", help="validate every set and rewrite index.json").set_defaults(run=command_build)

    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
