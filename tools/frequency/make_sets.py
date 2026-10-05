#!/usr/bin/env python3
"""Writes one language's most frequent words as ten sets of a hundred, from its draft and the
review's decisions.

Usage, from the repository root, after review.py merge:
    python3 tools/frequency/make_sets.py pl     # sets/top-words-pl-01 … top-words-pl-10

Kept words in frequency order; a word whose `<lang>` wording another kept word already has is
left out (`pójść` after `iść`, which lists it as its perfective). Every set carries all eight
languages and `learning: [<lang>]`, so the app offers it only to learners of that language.
Writing a language again rewrites its sets; a set whose words did not change keeps its
revision. Covers need Pillow.
"""

import datetime
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))
from sets import LANGUAGES, bare, texts  # noqa: E402

CACHE = HERE / ".cache"
TOTAL, SIZE = 1000, 100

# NAMES[x][y]: the name of language x, written in language y.
NAMES = {
    "en": dict(en="English", ru="Английский", pl="Angielski", uk="Англійська", de="Englisch", es="Inglés", fr="Anglais", it="Inglese"),
    "ru": dict(en="Russian", ru="Русский", pl="Rosyjski", uk="Російська", de="Russisch", es="Ruso", fr="Russe", it="Russo"),
    "pl": dict(en="Polish", ru="Польский", pl="Polski", uk="Польська", de="Polnisch", es="Polaco", fr="Polonais", it="Polacco"),
    "uk": dict(en="Ukrainian", ru="Украинский", pl="Ukraiński", uk="Українська", de="Ukrainisch", es="Ucraniano", fr="Ukrainien", it="Ucraino"),
    "de": dict(en="German", ru="Немецкий", pl="Niemiecki", uk="Німецька", de="Deutsch", es="Alemán", fr="Allemand", it="Tedesco"),
    "es": dict(en="Spanish", ru="Испанский", pl="Hiszpański", uk="Іспанська", de="Spanisch", es="Español", fr="Espagnol", it="Spagnolo"),
    "fr": dict(en="French", ru="Французский", pl="Francuski", uk="Французька", de="Französisch", es="Francés", fr="Français", it="Francese"),
    "it": dict(en="Italian", ru="Итальянский", pl="Włoski", uk="Італійська", de="Italienisch", es="Italiano", fr="Italien", it="Italiano"),
}
TITLE = dict(
    en="{name}: top words {a}–{b}", ru="{name}: частые слова {a}–{b}", pl="{name}: najczęstsze słowa {a}–{b}",
    uk="{name}: часті слова {a}–{b}", de="{name}: häufigste Wörter {a}–{b}", es="{name}: palabras más frecuentes {a}–{b}",
    fr="{name} : mots les plus fréquents {a}–{b}", it="{name}: parole più frequenti {a}–{b}",
)
DESCRIPTION = dict(
    en="Nouns, verbs, adjectives and adverbs {a}–{b} by frequency in film subtitles (FrequencyWords, OpenSubtitles 2018, CC BY-SA 4.0); translations from Wiktionary (CC BY-SA 4.0), checked.",
    ru="Существительные, глаголы, прилагательные и наречия с {a} по {b} по частоте в субтитрах фильмов (FrequencyWords, OpenSubtitles 2018, CC BY-SA 4.0); переводы из Викисловаря (CC BY-SA 4.0), проверены.",
    pl="Rzeczowniki, czasowniki, przymiotniki i przysłówki od {a} do {b} według częstości w napisach filmowych (FrequencyWords, OpenSubtitles 2018, CC BY-SA 4.0); tłumaczenia z Wikisłownika (CC BY-SA 4.0), sprawdzone.",
    uk="Іменники, дієслова, прикметники й прислівники з {a} по {b} за частотою в субтитрах фільмів (FrequencyWords, OpenSubtitles 2018, CC BY-SA 4.0); переклади з Вікісловника (CC BY-SA 4.0), перевірені.",
    de="Nomen, Verben, Adjektive und Adverbien {a}–{b} nach Häufigkeit in Filmuntertiteln (FrequencyWords, OpenSubtitles 2018, CC BY-SA 4.0); Übersetzungen aus dem Wiktionary (CC BY-SA 4.0), geprüft.",
    es="Sustantivos, verbos, adjetivos y adverbios {a}–{b} por frecuencia en subtítulos de películas (FrequencyWords, OpenSubtitles 2018, CC BY-SA 4.0); traducciones de Wikcionario (CC BY-SA 4.0), revisadas.",
    fr="Noms, verbes, adjectifs et adverbes {a}–{b} par fréquence dans les sous-titres de films (FrequencyWords, OpenSubtitles 2018, CC BY-SA 4.0) ; traductions du Wiktionnaire (CC BY-SA 4.0), vérifiées.",
    it="Nomi, verbi, aggettivi e avverbi {a}–{b} per frequenza nei sottotitoli dei film (FrequencyWords, OpenSubtitles 2018, CC BY-SA 4.0); traduzioni dal Wikizionario (CC BY-SA 4.0), controllate.",
)
COVER = {
    "en": ("#3A6EA5", "#7FB3E0"), "ru": ("#B23A48", "#E8828C"), "pl": ("#C8102E", "#F28B82"),
    "uk": ("#0057B7", "#E6B800"), "de": ("#3D3D3D", "#D4A017"), "es": ("#C60B1E", "#F1BF00"),
    "fr": ("#2B4FA3", "#E05A5A"), "it": ("#1E8C45", "#D94A4A"),
}


def level(chunk: int) -> str:
    return "A1" if chunk <= 3 else "A2" if chunk <= 7 else "B1"


def words(code: str) -> list[dict]:
    draft = json.loads((CACHE / "drafts" / f"{code}.json").read_text(encoding="utf-8"))
    decided = {row["lemma"]: row for row in json.loads((HERE / "reviews" / f"{code}.json").read_text(encoding="utf-8"))}
    undecided = [row["lemma"] for row in draft if row["lemma"] not in decided]
    picked, seen = [], set()
    for row in draft:
        decision = decided.get(row["lemma"])
        if decision is None:
            break
        if not decision["keep"]:
            continue
        wordings = {bare(t) for t in texts(decision["forms"][code])}
        if wordings & seen:
            continue
        seen |= wordings
        picked.append({"forms": decision["forms"]})
        if len(picked) == TOTAL:
            return picked
    sys.exit(f"{code}: only {len(picked)} words before the first undecided one"
             + (f" ({undecided[0]}); run review.py split/merge" if undecided else "; draft more candidates"))


def write_set(code: str, chunk: int, chunk_words: list[dict]):
    set_id = f"top-words-{code}-{chunk:02}"
    folder = ROOT / "sets" / set_id
    folder.mkdir(parents=True, exist_ok=True)
    a, b = (chunk - 1) * SIZE + 1, chunk * SIZE
    words_path = folder / "words.json"
    text = json.dumps({"format": 1, "words": chunk_words}, ensure_ascii=False, indent=2) + "\n"
    changed = not words_path.is_file() or words_path.read_text(encoding="utf-8") != text
    words_path.write_text(text, encoding="utf-8")

    meta_path = folder / "set.json"
    old = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    meta = {
        "title": {lang: TITLE[lang].format(name=NAMES[code][lang], a=a, b=b) for lang in LANGUAGES},
        "description": {lang: DESCRIPTION[lang].format(a=a, b=b) for lang in LANGUAGES},
        "languages": LANGUAGES,
        "learning": [code],
        "image": "cover.png",
        "level": level(chunk),
        "tags": ["frequency"],
        "updated": datetime.date.today().isoformat() if changed or "updated" not in old else old["updated"],
        "source": {"type": "json", "url": "words.json"},
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not (folder / "cover.png").is_file():
        start, end = COVER[code]
        subprocess.run([sys.executable, str(ROOT / "tools" / "make_cover.py"), str(folder / "cover.png"),
                        f"{code.upper()} {a}–{b}", start, end], check=True)
    return set_id, changed


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in LANGUAGES:
        print(__doc__)
        return 1
    code = sys.argv[1]
    picked = words(code)
    for chunk in range(1, TOTAL // SIZE + 1):
        set_id, changed = write_set(code, chunk, picked[(chunk - 1) * SIZE:chunk * SIZE])
        print(f"sets/{set_id}" + ("" if changed else " (unchanged)"))
    return subprocess.call([sys.executable, str(ROOT / "tools" / "build.py")])


if __name__ == "__main__":
    sys.exit(main())
