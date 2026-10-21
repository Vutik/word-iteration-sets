"""Marks the aspect pairs of Russian, Polish and Ukrainian verbs in the sets.

A verb is often written as a pair of aspects — `просыпаться` with `проснуться` as an alternate.
This adds `"aspects": {"impf": [...], "pf": [...]}` to such a form, so the app can study the two
as separate words. `text` and `alternates` stay as they are, for older versions of the app.

Only wordings that are the same phrase around a verb of the other aspect and share its root
count as a pair; a synonym (`obserwować` / `śledzić`) stays a plain alternate. Groups the rules
cannot settle are listed for review, and settled by hand in `DECISIONS`.

Needs `pymorphy3`, `pymorphy3-dicts-ru`, `pymorphy3-dicts-uk` and `morfeusz2`:

    python3 tools/aspects.py            # report what would change
    python3 tools/aspects.py --write    # write it into sets/*/words.json
"""

import argparse
import glob
import json
import os
from difflib import SequenceMatcher

import morfeusz2
import pymorphy3

ROOT = os.path.join(os.path.dirname(__file__), "..")
LANGUAGES = ("ru", "pl", "uk")

# Pairs from different roots, which the shared-root rule cannot see.
SUPPLETIVE = {
    ("брать", "взять"), ("говорить", "сказать"), ("класть", "положить"), ("ловить", "поймать"),
    ("садиться", "сесть"), ("ложиться", "лечь"), ("становиться", "стать"), ("искать", "найти"),
    ("брати", "взяти"), ("говорити", "сказати"), ("казати", "сказати"), ("класти", "покласти"),
    ("ловити", "спіймати"), ("ловити", "впіймати"), ("сідати", "сісти"), ("лягати", "лягти"),
    ("ставати", "стати"), ("brać", "wziąć"), ("mówić", "powiedzieć"), ("kłaść", "położyć"),
    ("oglądać", "obejrzeć"), ("widzieć", "zobaczyć"), ("siadać", "usiąść"), ("kłaść się", "położyć się"),
    ("wkładać", "włożyć"), ("bić", "uderzyć"), ("iść", "pójść"), ("składać", "złożyć"),
    ("wzdychać", "westchnąć"), ("возвращать", "вернуть"), ("идти", "пойти"), ("падать", "упасть"),
    ("пинать", "пнуть"), ("плевать", "плюнуть"), ("сажать", "посадить"), ("складывать", "сложить"),
    ("усаживать", "посадить"), ("вмикати", "увімкнути"), ("вмирати", "померти"), ("входити", "увійти"),
    ("йти", "піти"), ("ловити", "піймати"), ("лопатися", "луснути"), ("падати", "впасти"),
    ("падати", "упасти"), ("хапати", "схопити"), ("zakładać", "założyć"), ("wychodzić", "wyjść"), ("przychodzić", "przyjść"),
}

# Hand decisions for the groups the rules leave open, by language and wordings as written:
# "imperfective, … / perfective, …" for a pair, None for no pair.
DECISIONS: dict[str, dict[str, str | None]] = {
    "pl": {
        "dochodzić | dojść": "dochodzić / dojść",
        "znosić | znieść": "znosić / znieść",
        "aplikować | ubiegać się": None,
        "brać ślub | pobrać się": None,
        "być może | ewentualnie": None,
        "być może | może": None,
        "może | być może": None,
        "być potrzebnym | przydać się": None,
        "być wartym | warto": None,
        "ciąć | kroić": None,
        "kapować | zrozumieć": None,
        "móc | potrafić": None,
        "mówić | porozmawiać": None,
        "nadawać | transmitować": None,
        "narzekać | poskarżyć się": None,
        "postać | sylwetka": None,
        "rezygnować | zwolnić się": None,
        "schodzić | zejść": "schodzić / zejść",
        "wchodzić | wejść": "wchodzić / wejść",
        "wpychać | wepchnąć": "wpychać / wepchnąć",
        "wsadzać do więzienia | wsadzić": "wsadzać do więzienia / wsadzić",
        "wychodzić | wyjeżdżać": None,
        "wychodzić za mąż | żenić się": None,
        "wzywać | wezwać": "wzywać / wezwać",
        "włazić | wleźć": "włazić / wleźć",
        "zachwycać | oczarować": None,
        "zbierać | zebrać": "zbierać / zebrać",
        "zrywać | zerwać": "zrywać / zerwać",
        "śmieć | odważyć się": None,
        "śpieszyć się | pośpieszyć": "śpieszyć się / pośpieszyć",
    },
    "ru": {
        "вкладывать | вложить": "вкладывать / вложить",
        "выходить | выйти": "выходить / выйти",
        "доходить | дойти": "доходить / дойти",
        "доходить | дойти | добраться": "доходить / дойти, добраться",
        "заходить | зайти": "заходить / зайти",
        "находить | найти": "находить / найти",
        "обходить | обойти": "обходить / обойти",
        "отходить | отойти": "отходить / отойти",
        "рожать | родить": "рожать / родить",
        "рождаться | родиться": "рождаться / родиться",
        "адресовать | направлять | направить": "адресовать, направлять / направить",
        "беседовать | поговорить": None,
        "бить | ударить": "бить / ударить",
        "брать интервью | интервьюировать": None,
        "браться | взяться": "браться / взяться",
        "быть должным | задолжать": None,
        "быть похожим | походить | напоминать": None,
        "велеть | приказывать | приказать": "велеть, приказывать / приказать",
        "возвращаться | вернуться": "возвращаться / вернуться",
        "возможно | быть может": None,
        "возможно | может быть": None,
        "может быть | возможно | наверное": None,
        "входить | войти": "входить / войти",
        "выходить замуж | жениться": None,
        "держать пари | поспорить": None,
        "достигать | достичь": "достигать / достичь",
        "женить | выдать замуж": None,
        "жениться | выйти замуж": None,
        "жениться | выйти замуж | выходить замуж": None,
        "жениться | выходить замуж": None,
        "заманивать | заманить в ловушку": "заманивать / заманить в ловушку",
        "идентифицировать | опознавать | опознать": "идентифицировать, опознавать / опознать",
        "использовать | применять | употреблять": None,
        "использовать | употреблять": None,
        "исследовать | обследовать": None,
        "касаться | коснуться": "касаться / коснуться",
        "лаять | гавкнуть": None,
        "морда | пасть": None,
        "нападать | напасть | атаковать": "нападать, атаковать / напасть",
        "осуществлять | осуществить | реализовать": "осуществлять, реализовать / осуществить",
        "пристально смотреть | уставиться | таращиться": None,
        "проходить | пройти | миновать": "проходить, миновать / пройти",
        "сажать в тюрьму | посадить": "сажать в тюрьму / посадить",
        "срывать | сорвать": "срывать / сорвать",
        "ссылаться | сослаться": "ссылаться / сослаться",
        "стоять на коленях | встать на колени": None,
        "судить | считать": None,
        "сходить с ума | спятить": None,
        "считать | полагать": None,
        "советовать | рекомендовать": None,
        "насмехаться | посмеяться": None,
        "торопиться | поторопиться | спешить": "торопиться, спешить / поторопиться",
        "транслировать | передавать": None,
        "уходить | уезжать": None,
        "уходить | уйти": "уходить / уйти",
    },
    "uk": {
        "виходити | вийти": "виходити / вийти",
        "заходити | зайти": "заходити / зайти",
        "нападати | напасти | атакувати": "нападати, атакувати / напасти",
        "обходити | обійти": "обходити / обійти",
        "рухати | рушити": "рухати / рушити",
        "рухатися | рушити": "рухатися / рушити",
        "бити | вдарити": "бити / вдарити",
        "бити ногою | копнути": None,
        "брати інтерв'ю | інтерв'ювати": None,
        "братися | взятися": "братися / взятися",
        "бути винним | заборгувати": None,
        "веліти | наказувати": None,
        "викликати | покликати": None,
        "викликати огиду | викликати відразу": None,
        "вимагати | зажадати": None,
        "винаймати | винайняти | орендувати": "винаймати, орендувати / винайняти",
        "виходити | виїжджати": None,
        "входити | авторизуватися": None,
        "відходити | піти": None,
        "дзвонити | зателефонувати": "дзвонити / зателефонувати",
        "дивитися | глянути": None,
        "доходити | дійти": "доходити / дійти",
        "доходити | дійти | дістатися": "доходити / дійти, дістатися",
        "заманювати | заманити в пастку": "заманювати / заманити в пастку",
        "збирати | зібрати": "збирати / зібрати",
        "звати | покликати": "звати / покликати",
        "здійснювати | здійснити | реалізувати": "здійснювати, реалізувати / здійснити",
        "злитися | розсердитися": None,
        "знадобитися | бути потрібним": None,
        "знаходитися | перебувати": None,
        "зривати | зірвати": "зривати / зірвати",
        "ладнати | порозумітися": None,
        "мама | мати": None,
        "мати | мама": None,
        "мусити | повинен": None,
        "повинен | мусити": None,
        "намагатися | спробувати": None,
        "намагатися | спробувати | старатися": None,
        "насміхатися | поглузувати": None,
        "одружуватися | виходити заміж": None,
        "одружуватися | женитися": None,
        "пам'ятати | згадати": None,
        "повідомляти | інформувати": None,
        "подавати заявку | подаватися": None,
        "почуватися | почуватись": None,
        "радити | рекомендувати": None,
        "розмовляти | поговорити": None,
        "саджати до в'язниці | посадити": "саджати до в'язниці / посадити",
        "справлятися | впоратися": "справлятися / впоратися",
        "спричиняти | викликати": None,
        "спричиняти | спричинити | викликати": "спричиняти, викликати / спричинити",
        "стикатися | зіткнутися": "стикатися / зіткнутися",
        "стосуватися | стосуватись": None,
        "стояти на колінах | стати на коліна": None,
        "судити | вважати": None,
        "транслювати | передавати": None,
        "тікати | втекти": "тікати / втекти",
        "тікати | утекти": "тікати / утекти",
        "хвилюватися | занепокоїтися": None,
        "ідентифікувати | визначати": None,
        "поспішати | поквапитися": None,
        "адресувати | спрямовувати": None,
        "піклуватися | потурбуватися": None,
        "розійтися | розлучатися": None,
    },
}

_ru = pymorphy3.MorphAnalyzer(lang="ru")
_uk = pymorphy3.MorphAnalyzer(lang="uk")
_pl = morfeusz2.Morfeusz()


def word_aspects(language: str, word: str) -> set[str]:
    """The aspects `word` has as an infinitive, empty when it is not one."""
    if language == "pl":
        found = set()
        for _, _, (_, _, tag, _, _) in _pl.analyse(word):
            if tag.startswith("inf"):
                found |= {a for a in tag.split(":")[1].split(".") if a in ("imperf", "perf")}
        return {"impf" if a == "imperf" else "pf" for a in found}
    analyzer = _ru if language == "ru" else _uk
    found = set()
    for parse in analyzer.parse(word):
        # Russian tags an infinitive `INFN`; Ukrainian tags it `VERB`, and an infinitive is its own normal form.
        if parse.tag.POS in ("INFN", "VERB") and parse.normal_form == word.lower():
            if parse.tag.aspect:
                found.add("impf" if parse.tag.aspect == "impf" else "pf")
    return found


def head(language: str, text: str):
    """(index of the verb, its aspects, the words of the phrase), or None without a verb."""
    words = text.split()
    for index, word in enumerate(words):
        aspects = word_aspects(language, word.strip(",.()"))
        if aspects:
            return index, aspects, words
    return None


ENDINGS = ("ться", "тися", "тись", "ть", "ти", "ć")


def stem(verb: str) -> str:
    verb = verb.lower()
    for ending in ENDINGS:
        if verb.endswith(ending):
            return verb[: -len(ending)]
    return verb


def related(a: str, b: str) -> str | None:
    """How two verbs are seen to share a root, or None."""
    if (a, b) in SUPPLETIVE or (b, a) in SUPPLETIVE:
        return "listed"
    a_stem, b_stem = sorted((stem(a), stem(b)), key=len)
    if SequenceMatcher(None, a_stem, b_stem).find_longest_match().size >= 3:
        return "root"
    # A short root under a prefix: `мыть` / `помыть`.
    if len(a_stem) >= 2 and a_stem in b_stem:
        return "root"
    if a[:2].lower() == b[:2].lower():
        return "prefix"
    return None


def classify(language: str, texts: list[str]):
    """`(aspects, note)`: aspects is `{"impf": [...], "pf": [...]}` or None; a note on aspects
    says what to look over, a note without them asks for a decision in `DECISIONS`."""
    decisions = DECISIONS.get(language, {})
    group = " | ".join(texts)
    if group in decisions:
        if decisions[group] is None:
            return None, None
        impf, pf = decisions[group].split(" / ")
        return {"impf": impf.split(", "), "pf": pf.split(", ")}, None

    heads = [head(language, t) for t in texts]
    if all(h is None for h in heads):
        return None, None
    if any(h is None for h in heads):
        return None, "no verb in " + ", ".join(t for t, h in zip(texts, heads) if h is None)

    def frame(h):
        index, _, words = h
        return [w for i, w in enumerate(words) if i != index]

    def verb(h):
        return h[2][h[0]]

    # Wordings that are the same phrase around verbs of one root, which can be of different aspects.
    links = []
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            hi, hj = heads[i], heads[j]
            if frame(hi) != frame(hj) or len(hi[1] | hj[1]) < 2 or hi[1] == hj[1] == {"impf", "pf"}:
                continue
            if hi[1] == hj[1]:
                continue
            how = related(verb(hi), verb(hj)) or related(texts[i], texts[j])
            if how:
                links.append((i, j, how))

    # A dictionary gives some verbs both aspects through a homonym (`просыпаться` of `просыпать`);
    # next to a partner of one aspect such a verb takes the other.
    kinds = [next(iter(h[1])) if len(h[1]) == 1 else None for h in heads]
    for i, j, how in links:
        if how == "prefix":
            continue
        if kinds[i] is None and kinds[j]:
            kinds[i] = "pf" if kinds[j] == "impf" else "impf"
        elif kinds[j] is None and kinds[i]:
            kinds[j] = "pf" if kinds[i] == "impf" else "impf"
    pairs = [(i, j, how) for i, j, how in links if kinds[i] and kinds[j] and kinds[i] != kinds[j]]
    if not pairs:
        if len({k for k in kinds if k}) == 2 or None in kinds:
            return None, "no pair: " + ", ".join(f"{t}={'/'.join(sorted(h[1]))}" for t, h in zip(texts, heads))
        return None, None
    if None in kinds:
        return None, "unsettled: " + ", ".join(t for t, k in zip(texts, kinds) if k is None)

    aspects = {"impf": [], "pf": []}
    for t, k in zip(texts, kinds):
        aspects[k].append(t)
    paired = {i for i, j, _ in pairs} | {j for i, j, _ in pairs}
    notes = [f"by {how}" for _, _, how in pairs if how == "prefix"]
    if len(paired) < len(texts):
        notes.append("extra: " + ", ".join(texts[i] for i in range(len(texts)) if i not in paired))
    return aspects, "; ".join(notes) or None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    groups: dict[tuple, tuple] = {}
    files = sorted(glob.glob(os.path.join(ROOT, "sets", "*", "words.json")))
    for path in files:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        changed = False
        for word in data["words"]:
            for language in LANGUAGES:
                form = word["forms"].get(language)
                if not isinstance(form, dict) or not form.get("alternates"):
                    continue
                texts = [form["text"]] + form["alternates"]
                key = (language, tuple(texts))
                if key not in groups:
                    groups[key] = classify(language, texts)
                aspects, _ = groups[key]
                if aspects and form.get("aspects") != aspects:
                    form["aspects"] = aspects
                    changed = True
                elif not aspects and "aspects" in form:
                    del form["aspects"]
                    changed = True
        if args.write and changed:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")

    paired = [k for k, (a, _) in groups.items() if a]
    review = sorted((k, n) for k, (a, n) in groups.items() if n and not a)
    look = sorted((k, n) for k, (a, n) in groups.items() if n and a)
    print(f"{len(groups)} groups, {len(paired)} pairs, {len(review)} to decide, {len(look)} to look over")
    for title, rows in (("To decide", review), ("To look over", look)):
        print(f"\n{title}:")
        for (language, texts), note in rows:
            print(f"{language}\t{' | '.join(texts)}\t{note}")


if __name__ == "__main__":
    main()
