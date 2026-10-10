#!/usr/bin/env python3
"""Builds the offline dictionaries the app downloads: one small SQLite file per directed pair.

Source: WikDict (https://www.wikdict.com), translations from Wiktionary through DBnary,
CC BY-SA. WikDict has no Ukrainian, so the pairs are those among en, ru, pl, de, es, fr, it.

Usage, from the repository root:
    python3 tools/dictionaries/build.py                 every pair → .cache/out/, dictionaries.json
    python3 tools/dictionaries/build.py pl-ru ru-pl     only these pairs (dictionaries.json untouched)
    python3 tools/dictionaries/build.py --upload        build, then attach the zips to the GitHub release

What a built file holds (format 1):
    meta(key, value)        from, to, release, format, licence, attribution
    entry(word, key, pos, sense, translations, rank)
        word          the headword as Wiktionary writes it, stress marks removed
        key           what the app looks up: lowercased, stress marks removed, ё → е
        pos           noun, verb, adjective, adverb, … or NULL
        sense         Wiktionary's gloss of the meaning, in the headword's language, or NULL
        translations  tab-separated, best first
        rank          WikDict's score of the meaning; order by it, highest first. Its score puts
                      our own frequency sets' translation first more often than its importance
    index entry_key on entry(key, rank DESC)

A meaning WikDict marks as reliable (is_good) is kept; an unreliable one only when the word has
no reliable meaning at all. Those are mostly translations inferred backwards and archaic words
("dziś" → "днесь"), and its rank is cut so it never comes first.

Standard library only. The WikDict downloads are cached in .cache/wikdict-<release>/.
"""

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import unicodedata
import urllib.request
import zipfile
from collections import Counter
from itertools import permutations
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache"
MANIFEST = ROOT / "dictionaries.json"
FORMAT = 1

RELEASE = "2026-06"
SOURCE = f"https://download.wikdict.com/dictionaries/sqlite/2_{RELEASE}"
TAG = f"dictionaries-{RELEASE}"
DOWNLOAD = f"https://github.com/vutik/word-iteration-sets/releases/download/{TAG}"
LANGUAGES = ["en", "ru", "pl", "de", "es", "fr", "it"]
LICENCE = "CC BY-SA 4.0"
ATTRIBUTION = "Wiktionary contributors, via DBnary and WikDict (wikdict.com)"

# Part-of-speech labels as each Wiktionary edition writes them in DBnary's entry ids
# ("pol/zamek__rzeczownik__1"), mapped to the app's English names. Anything else is NULL.
POS = {
    "noun": {"noun", "substantiv", "rzeczownik", "сущ", "nom", "sustantivo", "sostantivo",
             "nome", "substantif", "sustantivo_femenino", "sustantivo_masculino",
             "nom_commun", "skrótowiec", "abkürzung"},
    "proper noun": {"proper_noun", "rzeczownik nazwa własna", "rzeczowniknazwawłasna",
                    "eigenname", "nom_propre", "nombre_propio", "nome_proprio", "фам",
                    "имя", "топоним"},
    "verb": {"verb", "czasownik", "гл", "verbe", "verbo", "hilfsverb", "czasownikmodalny"},
    "adjective": {"adjective", "adjektiv", "przymiotnik", "прил", "adjectif", "adjetivo",
                  "aggettivo", "przymiotnikdzierżawczy", "прич"},
    "adverb": {"adverb", "przysłówek", "adv", "нареч", "adverbe", "adverbio", "avverbio",
               "деепр", "lokaladverb", "pronominaladverb", "konjunktionaladverb", "predic"},
    "pronoun": {"pronoun", "pronomen", "zaimek", "мест", "pronom", "pronombre", "pronome",
                "personalpronomen", "possessivpronomen", "demonstrativpronomen",
                "pronom_personnel", "zaimeknieokreślony", "indefinitpronomen", "determiner",
                "article"},
    "preposition": {"preposition", "prep", "postposition", "präposition", "przyimek", "предл", "préposition",
                    "preposición", "preposizione"},
    "conjunction": {"conjunction", "conj", "subjunktion", "konjunktion", "spójnik", "союз", "conjonction",
                    "conjunción", "congiunzione"},
    "interjection": {"interjection", "interjektion", "wykrzyknik", "interj", "межд",
                     "interjección", "interiezione", "onomatop", "grußformel"},
    "numeral": {"numeral", "number", "liczebnikgłówny", "numerale", "liczebnik", "числ", "kardinalzahl", "ordinalzahl",
                "adjectif_numéral", "numeral_cardinal", "numerale_cardinale"},
    "particle": {"particle", "part", "partikel", "partykuła", "част", "particule", "partícula",
                 "particella"},
    "phrase": {"phrase", "prepositional_phrase", "redewendung", "wortverbindung",
               "związek frazeologiczny", "związek_frazeologiczny", "фраз", "locution",
               "locución", "locuzione", "idiom", "fraza_przyimkowa", "fraza_spójnikowa",
               "fraza_partykułowa", "fraza_zdaniowa", "proverb", "przysłowie", "sprichwort",
               "geflügeltes_wort", "intro"},
}
# Compound labels ("zaimekosobowy", "verbo_transitivo", "loc_nom") by the part they contain,
# the first that matches. Proper nouns and phrases come first: "sustantivo_propio" is a name.
POS_STEMS = [
    ("propio", "proper noun"), ("nompr", "proper noun"), ("prénom", "proper noun"),
    ("nom_de_famille", "proper noun"), ("patronyme", "proper noun"), ("nachname", "proper noun"),
    ("toponym", "proper noun"), ("straßenname", "proper noun"),
    ("locución", "phrase"), ("locution", "phrase"), ("loc_", "phrase"), ("espr", "phrase"),
    ("fraza", "phrase"), ("zwrot", "phrase"), ("związek", "phrase"), ("formel", "phrase"),
    ("wyrażenie", "phrase"), ("merkspruch", "phrase"),
    ("zaimek", "pronoun"), ("pronomen", "pronoun"), ("pronom", "pronoun"), ("artikel", "pronoun"),
    ("liczebnik", "numeral"), ("num", "numeral"),
    ("sustantivo", "noun"), ("sost", "noun"), ("acron", "noun"), ("skrót", "noun"),
    ("verbo", "verb"), ("czasownik", "verb"),
    ("imiesłów", "adjective"), ("przymiotnik", "adjective"), ("adj", "adjective"),
    ("agg", "adjective"),
    ("adverb", "adverb"), ("avv", "adverb"), ("przysłów", "adverb"),
    ("partikel", "particle"), ("conjonction", "conjunction"), ("cong", "conjunction"),
    ("spójnik", "conjunction"), ("onomatop", "interjection"), ("wykrzyk", "interjection"),
    ("inter", "interjection"),
]
# Pieces of words, not words: never offered as a translation of anything.
AFFIXES = {"prefix", "suffix", "interfix", "przedrostek", "przyrostek", "temat_słowotwórczy",
           "gebundenes_lexem", "präfix", "präfixoid", "suffixoid", "symbol",
           "końcówka_fleksyjna", "międzyrostek", "sufijo", "prefijo", "pref", "suff", "znak_interpunkcyjny", "ortsnamengrundwort"}
POS_BY_LABEL = {label: name for name, labels in POS.items() for label in labels}
ENTRY = re.compile(r"^[a-z]{3}/(.+?)__(.+?)__\d+$")
WIKILINK = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]")
FOOTNOTE = re.compile(r"\s*\[\d+\]")
# A stress mark after a Cyrillic letter, as in тра́тить. Only there: in é, è, ò the same
# combining marks are letters of the word.
STRESS = re.compile(r"(?<=[\u0400-\u04FF])[\u0300\u0301]")


def strip_stress(text):
    return unicodedata.normalize("NFC", STRESS.sub("", text))


def clean(text):
    text = WIKILINK.sub(r"\1", text or "")
    text = text.replace("'''", "").replace("''", "")
    return " ".join(strip_stress(text).split())


def lookup_key(word):
    return clean(word).lower().replace("ё", "е")


def pos_label(entry):
    match = ENTRY.match(entry or "")
    return match.group(2).lower() if match else None


def usable(translation):
    """False for what is left of markup the cleaning could not read: a template, an HTML tag,
    an unclosed link."""
    return bool(translation) and not re.search(r"[{}<>]|\[\[|\]\]", translation) \
        and translation.count("[") == translation.count("]")


def part_of_speech(entry, unknown):
    label = pos_label(entry)
    if label is None:
        return None
    name = POS_BY_LABEL.get(label) or next(
        (name for stem, name in POS_STEMS if stem in label), None)
    if name is None:
        unknown[label] += 1
    return name


def download(pair):
    path = CACHE / f"wikdict-{RELEASE}" / f"{pair}.sqlite3"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"  downloading {pair} …", flush=True)
        partial = path.with_suffix(".part")
        urllib.request.urlretrieve(f"{SOURCE}/{pair}.sqlite3", partial)
        partial.rename(path)
    return path


def meanings(source, unknown):
    """(word, pos, sense, translations, rank, reliable) per meaning, rows of one meaning merged."""
    merged = {}
    rows = source.execute(
        "SELECT lexentry, written_rep, sense, trans_list, score, is_good FROM translation")
    for entry, word, sense, trans_list, score, is_good in rows:
        if pos_label(entry) in AFFIXES:
            continue
        word = clean(word)
        linked = WIKILINK.sub(r"\1", trans_list or "")
        translations = [FOOTNOTE.sub("", clean(t)).strip() for t in linked.split(" | ")]
        translations = [t for t in translations if usable(t)]
        if not word or not translations:
            continue
        sense = clean(sense) or None
        key = (entry, word, sense)
        if key not in merged:
            merged[key] = [word, part_of_speech(entry, unknown), sense, [], 0.0, False]
        meaning = merged[key]
        for translation in translations:
            if translation not in meaning[3]:
                meaning[3].append(translation)
        meaning[4] = max(meaning[4], score or 0.0)
        meaning[5] = meaning[5] or bool(is_good)
    return merged.values()


def build(pair):
    source_language, target_language = pair.split("-")
    unknown = Counter()
    source = sqlite3.connect(download(pair))
    all_meanings = list(meanings(source, unknown))
    source.close()

    reliable_words = {lookup_key(m[0]) for m in all_meanings if m[5]}
    kept = []
    dropped = 0
    for word, pos, sense, translations, rank, reliable in all_meanings:
        key = lookup_key(word)
        if not reliable:
            if key in reliable_words:
                dropped += 1
                continue
            rank = min(rank, 0.01)
        kept.append((word, key, pos, sense, "\t".join(translations), round(rank, 4)))

    out = CACHE / "out" / f"{pair}.sqlite"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    db = sqlite3.connect(out)
    db.executescript("""
        PRAGMA page_size = 4096;
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE entry(
            word TEXT NOT NULL, key TEXT NOT NULL, pos TEXT, sense TEXT,
            translations TEXT NOT NULL, rank REAL NOT NULL);
    """)
    db.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("from", source_language), ("to", target_language), ("release", RELEASE),
        ("format", str(FORMAT)), ("licence", LICENCE), ("attribution", ATTRIBUTION)])
    db.executemany("INSERT INTO entry VALUES (?, ?, ?, ?, ?, ?)", sorted(kept, key=lambda r: r[1]))
    db.execute("CREATE INDEX entry_key ON entry(key, rank DESC)")
    db.commit()
    db.execute("VACUUM")
    db.close()

    archive = CACHE / "out" / f"{pair}.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.write(out, f"{pair}.sqlite")

    words = len({row[1] for row in kept})
    print(f"{pair}: {words} words, {len(kept)} meanings ({dropped} unreliable dropped), "
          f"{out.stat().st_size / 1e6:.1f} MB, zip {archive.stat().st_size / 1e6:.1f} MB")
    if unknown:
        print("  unmapped parts of speech:",
              ", ".join(f"{label} {count}" for label, count in unknown.most_common(12)))
    leftovers = [row[4] for row in kept if re.search(r"[\[\]{}]", row[4])][:3]
    if leftovers:
        print("  markup left:", " / ".join(leftovers))
    return {
        "from": source_language, "to": target_language,
        "url": f"{DOWNLOAD}/{pair}.zip",
        "size": archive.stat().st_size,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "unpackedSize": out.stat().st_size,
        "words": words,
    }


def main(arguments):
    upload = "--upload" in arguments
    pairs = [a for a in arguments if not a.startswith("--")]
    every = [f"{a}-{b}" for a, b in permutations(LANGUAGES, 2)]
    for pair in pairs:
        if pair not in every:
            sys.exit(f"unknown pair {pair}; pairs are {', '.join(every)}")
    built = [build(pair) for pair in (pairs or every)]

    if not pairs:
        MANIFEST.write_text(json.dumps({
            "format": FORMAT, "release": RELEASE, "licence": LICENCE,
            "attribution": ATTRIBUTION, "source": "https://www.wikdict.com",
            "dictionaries": built,
        }, ensure_ascii=False, indent=2) + "\n")
        print(f"wrote {MANIFEST.relative_to(ROOT)}: {len(built)} dictionaries, "
              f"{sum(d['size'] for d in built) / 1e6:.0f} MB in all")

    if upload:
        zips = [str(CACHE / "out" / f"{d['from']}-{d['to']}.zip") for d in built]
        exists = subprocess.run(["gh", "release", "view", TAG], capture_output=True).returncode == 0
        if not exists:
            subprocess.run(["gh", "release", "create", TAG, "--title", f"Dictionaries {RELEASE}",
                            "--notes", f"Offline dictionaries from WikDict {RELEASE}. {LICENCE}, "
                                       f"{ATTRIBUTION}."], check=True)
        subprocess.run(["gh", "release", "upload", TAG, "--clobber", *zips], check=True)


if __name__ == "__main__":
    main(sys.argv[1:])
