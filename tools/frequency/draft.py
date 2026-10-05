#!/usr/bin/env python3
"""Drafts one language's most frequent words with their translations into every app language.

Usage, from the repository root, after tools/frequency/extract.py for all eight languages:
    python3 tools/frequency/draft.py pl     # writes tools/frequency/drafts/pl.json

1. Frequency: hermitdave/FrequencyWords, 2018 OpenSubtitles counts (CC BY-SA 4.0). Word forms
   are folded into their lemma (`jest`, `są` → `być`) with Wiktionary's inflection tables.
2. Only nouns, verbs, adjectives and adverbs are kept: pronouns, prepositions and the like are
   the top of every list but make poor cards. Names and vulgar words are dropped.
3. A word that one of our own sets already has takes that set's forms in every language — they
   were checked by hand. Others are translated through English Wiktionary's translation tables:
   an English word directly, any other through the English word its gloss names, in the sense
   whose translations list the word itself.
4. The draft keeps `CANDIDATES` words, more than a list needs, so the review (tools/frequency/
   README.md) can reject some. Missing languages are listed in `missing` for it to fill.
Standard library only.
"""

import collections
import gzip
import json
import re
import sys
import unicodedata
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))
from sets import LANGUAGES, bare, plain, texts, unstressed  # noqa: E402
CACHE = HERE / ".cache"
DRAFTS = CACHE / "drafts"
CANDIDATES = 1400

CONTENT = {"noun": "noun", "verb": "verb", "adj": "adjective", "adv": "adverb"}
FUNCTION = {"pron", "det", "prep", "conj", "particle", "article", "num", "intj", "contraction", "postp", "name", "prep_phrase"}
INFINITIVE = {
    "ru": r"(ть|ти|чь)(ся|сь)?$", "uk": r"(ти|ть)(ся)?$", "pl": r"(ć|c)( się)?$",
    "de": r"(en|ern|eln)$", "es": r"(ar|er|ir)(se)?$", "fr": r"(er|ir|re|oir)$",
    "it": r"(are|ere|ire|rre)(si)?$",
}
OFFENSIVE = {"vulgar", "offensive", "derogatory", "ethnic", "slur"}

FREQUENCY_URL = "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/{0}/{0}_50k.txt"


# MARK: - Inputs

def frequency(code: str) -> list[tuple[str, int]]:
    path = CACHE / f"{code}_50k.txt"
    if not path.is_file():
        CACHE.mkdir(exist_ok=True)
        with urllib.request.urlopen(FREQUENCY_URL.format(code)) as response:
            path.write_bytes(response.read())
    pairs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        word, _, count = line.rpartition(" ")
        if word and count.isdigit():
            pairs.append((word, int(count)))
    return pairs


def entries(code: str):
    with gzip.open(CACHE / f"{code}.jsonl.gz", "rt", encoding="utf-8") as lines:
        for line in lines:
            yield json.loads(line)


class Dictionary:
    """One language's extract, indexed for lemma lookup."""

    def __init__(self, code: str):
        self.code = code
        self.by_word: dict[str, list[dict]] = collections.defaultdict(list)
        self.lemmas_of: dict[str, set[str]] = collections.defaultdict(set)
        self.names: set[str] = set()
        self.function: set[str] = set()
        for entry in entries(code):
            key = plain(entry["word"])
            self.by_word[key].append(entry)
            if entry["pos"] == "name":
                self.names.add(key)
                continue
            for lemma in entry.get("formOf", []):
                self.lemmas_of[key].add(plain(lemma))
            if "formOf" not in entry:
                if entry["pos"] in FUNCTION:
                    self.function.add(key)
                for form in entry.get("forms", []):
                    if len(form.split()) == 1 and plain(form) != key:
                        self.lemmas_of[plain(form)].add(key)

    def lemma_entry(self, key: str) -> dict | None:
        """The word's content lemma entry: Wiktionary's first, but the verb when the word looks
        like an infinitive — `wollen` is "to want" before "woollen", `czas` stays "time"."""
        found = [e for e in self.by_word.get(key, []) if e["pos"] in CONTENT and "formOf" not in e]
        infinitive = INFINITIVE.get(self.code)
        if infinitive and re.search(infinitive, key):
            found.sort(key=lambda e: e["pos"] != "verb")
        return (found or [None])[0]

    def is_function(self, key: str) -> bool:
        """A pronoun, preposition, conjunction… — unless it is also a verb (`sein`, `like`)."""
        entry = self.lemma_entry(key)
        # English has no infinitive ending to tell `like` from `you` (Wiktionary: "to you").
        return key in self.function and not (entry and entry["pos"] == "verb" and self.code != "en")


# MARK: - Lemmas

# A form counts towards another lemma when that lemma is at least this common on its own.
SHARE = 0.05


def lemmas(code: str, dictionary: Dictionary) -> list[dict]:
    """Content lemmas, most frequent first, with their counts summed over their forms."""
    surface = frequency(code)
    surface_count = {plain(word): count for word, count in reversed(surface)}
    totals: collections.Counter = collections.Counter()
    for word, count in surface:
        key = plain(word)
        if len(key) < 2 or not key.replace("-", "").replace("'", "").isalpha():
            continue
        if dictionary.is_function(key):
            continue
        # A form of a common lemma is that lemma's: `habe` is "haben", not "die Habe"; `mnie`
        # is "ja", not "miąć". A form of a pronoun or the like is dropped with it.
        targets = [t for t in dictionary.lemmas_of.get(key, ()) if surface_count.get(t, 0) >= SHARE * count]
        if any(dictionary.is_function(t) for t in targets):
            continue
        content = [t for t in targets if dictionary.lemma_entry(t)]
        if content:
            totals[max(content, key=lambda t: surface_count.get(t, 0))] += count
        elif dictionary.lemma_entry(key):
            totals[key] += count
        else:
            weak = [t for t in dictionary.lemmas_of.get(key, ()) if dictionary.lemma_entry(t) and not dictionary.is_function(t)]
            if weak:
                totals[max(weak, key=lambda t: surface_count.get(t, 0))] += count

    picked = []
    for key, count in totals.most_common():
        entry = dictionary.lemma_entry(key)
        if not entry or dictionary.is_function(key):
            continue
        if entry["pos"] == "noun" and key in dictionary.names:
            continue
        if OFFENSIVE & set(entry.get("tags", [])):
            continue
        picked.append({"lemma": entry["word"], "pos": entry["pos"], "count": count, "entry": entry})
        if len(picked) == CANDIDATES:
            break
    return picked


# MARK: - Our own sets

def own_words() -> dict[tuple[str, str], tuple[str, dict]]:
    """(language, bare text) → (set id, forms) for every word in the repository's json sets."""
    index = {}
    for path in sorted((ROOT / "sets").glob("*/words.json")):
        for word in json.loads(path.read_text(encoding="utf-8"))["words"]:
            forms = word["forms"]
            if not all(code in forms for code in LANGUAGES):
                continue
            for code, form in forms.items():
                for text in texts(form):
                    index.setdefault((code, bare(text)), (path.parent.name, forms))
    return index


# MARK: - Translations

def gloss_heads(gloss: str, pos: str) -> list[str]:
    """English words a gloss names: `(transitive) to do, to make; to cause` → do, make, cause."""
    text = re.sub(r"\([^)]*\)", "", gloss).split(":")[0]
    heads = []
    for part in re.split(r"[,;]", text):
        part = part.strip().rstrip(".").strip()
        if pos == "verb":
            part = re.sub(r"^to\s+", "", part)
        part = re.sub(r"^(a|an|the)\s+", "", part)
        if part and len(part.split()) <= 3:
            heads.append(part)
    return heads


def sense_groups(entry: dict) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    for item in entry.get("translations", []):
        groups.setdefault(item["sense"], []).append(item)
    return list(groups.items())


def pick_sense(english: list[dict], code: str, lemma: str) -> tuple[str, list[dict]] | None:
    """The sense whose translations into `code` include `lemma`; else the first that covers
    five languages — the first listed is the main sense — or the one that covers most."""
    groups = [group for entry in english for group in sense_groups(entry)]
    if not groups:
        return None
    if code != "en":
        for sense, items in groups:
            if any(item["lang"] == code and bare(item["word"]) == bare(lemma) for item in items):
                return sense, items
    coverage = [len({item["lang"] for item in items}) for _, items in groups]
    for group, covered in zip(groups, coverage):
        if covered >= 5:
            return group
    return groups[coverage.index(max(coverage))]


ARTICLE = {
    "de": {"masculine": "der", "feminine": "die", "neuter": "das", "plural": "die"},
    "es": {"masculine": "el", "feminine": "la", "plural": "los"},
    "fr": {"masculine": "le", "feminine": "la", "plural": "les"},
    "it": {"masculine": "il", "feminine": "la", "plural": "i"},
}


def with_article(code: str, word: str, genders: list[str]) -> str:
    table = ARTICLE.get(code)
    if not table or not genders:
        return word
    article = table.get(genders[0])
    if not article:
        return word
    first = word[:1].lower()
    if code in ("fr", "it") and genders[0] != "plural" and (first in "aeiouhàâéèêîôûœ" if code == "fr" else first in "aeiou"):
        return f"l'{word}"
    if code == "it" and article == "il" and re.match(r"^(s[^aeiou]|z|gn|ps|x|y)", word.lower()):
        article = "lo"
    return f"{article} {word}"


def form_for(code: str, words: list[dict], pos: str) -> dict:
    """The form our sets use: nouns with their article and the bare word as an alternate."""
    seen = []
    for item in words:
        text = unstressed(item["word"])
        if text not in [s[0] for s in seen]:
            seen.append((text, item.get("gender", [])))
    text, genders = seen[0]
    alternates = [t for t, _ in seen[1:3]]
    if pos == "noun" and code in ARTICLE:
        full = with_article(code, text, genders)
        if full != text:
            alternates = [text, *alternates]
            text = full
    return {"text": text, "alternates": alternates} if alternates else {"text": text}


def english_form(word: str, pos: str) -> dict:
    text = f"to {word}" if pos == "verb" and not word.startswith("to ") else word
    return {"text": text, "partOfSpeech": CONTENT[pos]}


def own_form(code: str, lemma: str, entry: dict) -> dict:
    """The source language's own word, as the other forms are written."""
    if code == "en":
        return english_form(lemma, entry["pos"])
    genders = [t for t in entry.get("tags", []) if t in ("masculine", "feminine", "neuter")]
    return form_for(code, [{"word": lemma, "gender": genders[:1]}], entry["pos"])


def draft(code: str) -> list[dict]:
    dictionary = Dictionary(code)
    english = dictionary if code == "en" else Dictionary("en")
    own = own_words()
    drafted = []
    for rank, item in enumerate(lemmas(code, dictionary), start=1):
        lemma, pos, entry = item["lemma"], item["pos"], item["entry"]
        row = {"rank": rank, "lemma": lemma, "pos": CONTENT[pos], "count": item["count"],
               "gloss": (entry.get("glosses") or [""])[0]}
        found = own.get((code, bare(lemma)))
        if found:
            row["source"] = f"set:{found[0]}"
            row["forms"] = found[1]
            row["missing"] = []
            drafted.append(row)
            continue

        if code == "en":
            pivots = [lemma]
            english_entries = [e for e in english.by_word.get(plain(lemma), []) if e["pos"] == pos]
        else:
            pivots, english_entries = [], []
            for gloss in entry.get("glosses", []):
                for head in gloss_heads(gloss, pos):
                    found_entries = [e for e in english.by_word.get(plain(head), []) if e["pos"] == pos and e.get("translations")]
                    if found_entries:
                        pivots.append(head)
                        english_entries += found_entries
                if pivots:
                    break

        forms = {code: own_form(code, lemma, entry)}
        if pivots and code != "en":
            forms["en"] = english_form(pivots[0], pos)
        sense = pick_sense(english_entries, code, lemma)
        if sense:
            row["sense"] = sense[0]
            by_language = collections.defaultdict(list)
            for translation in sense[1]:
                by_language[translation["lang"]].append(translation)
            for other in LANGUAGES:
                if other not in forms and by_language.get(other):
                    forms[other] = form_for(other, by_language[other], pos)
        row["source"] = "wiktionary"
        row["forms"] = {c: forms[c] for c in LANGUAGES if c in forms}
        row["missing"] = [c for c in LANGUAGES if c not in forms]
        drafted.append(row)
    return drafted


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in LANGUAGES:
        print(f"usage: draft.py <{'|'.join(LANGUAGES)}>")
        return 1
    code = sys.argv[1]
    rows = draft(code)
    DRAFTS.mkdir(exist_ok=True)
    (DRAFTS / f"{code}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    own = sum(r["source"].startswith("set:") for r in rows)
    complete = sum(not r["missing"] for r in rows)
    print(f"{code}: {len(rows)} words, {own} from our sets, {complete} with every language")
    return 0


if __name__ == "__main__":
    sys.exit(main())
