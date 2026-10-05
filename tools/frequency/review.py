#!/usr/bin/env python3
"""Cuts a draft into batches for review and collects the answers.

Usage, from the repository root:
    python3 tools/frequency/review.py split pl      # .cache/review/pl-01.json, pl-02.json, …
    python3 tools/frequency/review.py merge pl      # checks every pl-NN.out.json, adds it to reviews/pl.json
    python3 tools/frequency/review.py status pl     # how many words are decided, kept, left
    python3 tools/frequency/review.py auto pl       # decide the rest unreviewed (see `auto`)

The rules for a reviewer are in tools/frequency/REVIEW.md. Decisions are kept by lemma in
`tools/frequency/reviews/<lang>.json`, which is committed; one marked `"by": "hand"` is never
overwritten by a merge: a word decided once is never sent
again, so drafting anew after the sets grew only asks about new words.
Standard library only.
"""

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from sets import LANGUAGES, texts  # noqa: E402

CACHE = HERE / ".cache"
BATCHES = CACHE / "review"
REVIEWS = HERE / "reviews"
SIZE = 100
POS = {"noun", "verb", "adjective", "adverb"}


def draft(code: str) -> list[dict]:
    return json.loads((CACHE / "drafts" / f"{code}.json").read_text(encoding="utf-8"))


def decisions(code: str) -> dict[str, dict]:
    path = REVIEWS / f"{code}.json"
    rows = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    return {row["lemma"]: row for row in rows}


def save_decisions(code: str, decided: dict[str, dict]):
    REVIEWS.mkdir(exist_ok=True)
    rows = sorted(decided.values(), key=lambda row: row["rank"])
    (REVIEWS / f"{code}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def check_form(form, where: str) -> list[str]:
    if isinstance(form, str):
        return [] if form.strip() else [f"{where}: empty"]
    if not isinstance(form, dict) or not isinstance(form.get("text"), str) or not form["text"].strip():
        return [f"{where}: needs text"]
    problems = []
    if set(form) - {"text", "alternates", "partOfSpeech", "transcription"}:
        problems.append(f"{where}: unknown keys {sorted(set(form) - {'text', 'alternates', 'partOfSpeech', 'transcription'})}")
    alternates = form.get("alternates", [])
    if not isinstance(alternates, list) or not all(isinstance(a, str) and a.strip() for a in alternates):
        problems.append(f"{where}: alternates must be non-empty strings")
    return problems


CYRILLIC = re.compile(r"[\u0400-\u04FF]")
LATIN = re.compile(r"[A-Za-z\u00C0-\u024F]")


def check_script(form, code: str, where: str) -> list[str]:
    """A Latin letter in a Russian word (`torт`) or the reverse looks right and never matches
    what the learner types."""
    wrong = LATIN if code in ("ru", "uk") else CYRILLIC
    return [f"{where}: mixed alphabet in {text!r}" for text in texts(form) if wrong.search(text)]


def check_answer(row: dict, asked: dict) -> list[str]:
    where = f"#{asked['rank']} {asked['lemma']}"
    if row.get("rank") != asked["rank"]:
        return [f"{where}: answer is for #{row.get('rank')}; answers must keep the input order"]
    if not isinstance(row.get("keep"), bool):
        return [f"{where}: keep must be true or false"]
    if not row["keep"]:
        return []
    forms = row.get("forms")
    if not isinstance(forms, dict) or sorted(forms) != sorted(LANGUAGES):
        return [f"{where}: forms must have exactly {' '.join(LANGUAGES)}"]
    problems = [p for code in LANGUAGES for p in check_form(forms[code], f"{where} [{code}]")]
    problems += [p for code in LANGUAGES for p in check_script(forms[code], code, f"{where} [{code}]")]
    english = forms["en"]
    if not isinstance(english, dict) or english.get("partOfSpeech") not in POS:
        problems.append(f"{where} [en]: needs partOfSpeech, one of {sorted(POS)}")
    elif english["partOfSpeech"] == "verb" and not english["text"].startswith("to "):
        problems.append(f"{where} [en]: a verb starts with 'to '")
    return problems


def split(code: str):
    """Words one of our sets already has are taken as they are — those sets were checked; only
    the rest go to review."""
    decided = decisions(code)
    taken = 0
    for row in draft(code):
        if row["lemma"] not in decided and row["source"].startswith("set:"):
            decided[row["lemma"]] = {"lemma": row["lemma"], "rank": row["rank"], "keep": True,
                                     "forms": {c: row["forms"][c] for c in LANGUAGES}, "by": row["source"]}
            taken += 1
    if taken:
        save_decisions(code, decided)
        print(f"{code}: {taken} words taken from our sets as they are")
    # Decided by `auto` without review: offered for review like undecided words.
    rows = [row for row in draft(code)
            if row["lemma"] not in decided or decided[row["lemma"]].get("by", "").startswith("auto")]
    BATCHES.mkdir(parents=True, exist_ok=True)
    for old in BATCHES.glob(f"{code}-*.json"):
        old.unlink()
    for number, start in enumerate(range(0, len(rows), SIZE), start=1):
        batch = [{k: v for k, v in row.items() if k != "count"} for row in rows[start:start + SIZE]]
        (BATCHES / f"{code}-{number:02}.json").write_text(json.dumps(batch, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{code}: {len(rows)} words to review in {-(-len(rows) // SIZE)} batches under {BATCHES.relative_to(Path.cwd())}"
          if rows else f"{code}: every word is decided")


def merge(code: str) -> int:
    decided = decisions(code)
    failed = 0
    for path in sorted(BATCHES.glob(f"{code}-[0-9][0-9].json")):
        out = path.with_suffix(".out.json")
        if not out.is_file():
            print(f"{path.name}: no answer yet")
            continue
        asked = json.loads(path.read_text(encoding="utf-8"))
        try:
            answers = json.loads(out.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            print(f"{out.name}: not JSON: {error}")
            failed += 1
            continue
        if not isinstance(answers, list) or len(answers) != len(asked):
            print(f"{out.name}: {len(answers) if isinstance(answers, list) else 'no'} answers for {len(asked)} rows")
            failed += 1
            continue
        problems = [p for answer, row in zip(answers, asked) for p in check_answer(answer, row)]
        if problems:
            failed += 1
            print(f"{out.name}: {len(problems)} problems")
            for problem in problems[:20]:
                print(f"  {problem}")
            continue
        for answer, row in zip(answers, asked):
            if decided.get(row["lemma"], {}).get("by", "").startswith(("hand", "set:")):
                continue
            entry = {"lemma": row["lemma"], "rank": row["rank"], "keep": answer["keep"]}
            if answer["keep"]:
                entry["forms"] = {code_: answer["forms"][code_] for code_ in LANGUAGES}
            elif answer.get("reason"):
                entry["reason"] = answer["reason"]
            decided[row["lemma"]] = entry
        print(f"{out.name}: {len(answers)} merged")
    save_decisions(code, decided)
    status(code)
    return 1 if failed else 0


def auto(code: str):
    """Decides every undecided word without review: kept when Wiktionary's tables gave all
    eight languages in a valid form, left out otherwise. Marked `"by": "auto"`, so a later
    `split` sends them to review after all."""
    decided = decisions(code)
    kept = left = 0
    for row in draft(code):
        if row["lemma"] in decided:
            continue
        entry = {"lemma": row["lemma"], "rank": row["rank"], "by": "auto"}
        answer = {"rank": row["rank"], "keep": True, "forms": row["forms"]}
        if not row["missing"] and not check_answer(answer, row):
            entry.update(keep=True, forms={c: row["forms"][c] for c in LANGUAGES})
            kept += 1
        else:
            entry.update(keep=False, reason="not reviewed: Wiktionary lacks a language")
            left += 1
        decided[row["lemma"]] = entry
    save_decisions(code, decided)
    print(f"{code}: {kept} words kept unreviewed from Wiktionary, {left} left out")
    status(code)


def status(code: str):
    decided = decisions(code)
    rows = draft(code)
    kept = sum(1 for row in rows if decided.get(row["lemma"], {}).get("keep"))
    left = sum(1 for row in rows if row["lemma"] not in decided)
    print(f"{code}: {len(rows)} drafted, {len(rows) - left} decided, {kept} kept, {left} left")


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in ("split", "merge", "status", "auto") or sys.argv[2] not in LANGUAGES:
        print(__doc__)
        return 1
    command, code = sys.argv[1:]
    if command == "split":
        split(code)
    elif command == "merge":
        return merge(code)
    elif command == "auto":
        auto(code)
    else:
        status(code)
    return 0


if __name__ == "__main__":
    sys.exit(main())
