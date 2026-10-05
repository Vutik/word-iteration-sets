# Reviewing a frequency batch

You get `tools/frequency/.cache/review/<lang>-<NN>.json`: up to 100 rows of the most frequent
words of one language (`<lang>`), drafted by `draft.py`. The words become flashcards in sets
called "<language>: most frequent words", offered to people **learning `<lang>`**. Write your
answer to the same path with `.out.json` instead of `.json`.

Each input row:

```json
{ "rank": 42, "lemma": "trzymać", "pos": "verb", "gloss": "to hold",
  "source": "wiktionary", "sense": "to grasp or grip",
  "forms": { "en": {...}, "ru": "держать", ... }, "missing": ["uk"] }
```

`source` is `wiktionary`: translations taken from Wiktionary's tables, often a bit off. (Words
our sets already have are taken from them by `review.py split` and never reach you; a batch
made before that may still hold `set:<id>` rows — keep their forms unless the sense is wrong.)

## What to answer

A JSON list with one object per input row, same order:

```json
[
  { "rank": 42, "keep": true, "forms": { "en": {...}, "ru": ..., "pl": ..., "uk": ..., "de": ..., "es": ..., "fr": ..., "it": ... } },
  { "rank": 43, "keep": false, "reason": "particle" }
]
```

Nothing else in the file — no comments, no trailing text. Check it parses
(`python3 -m json.tool <file> > /dev/null`).

## keep: false when

- it is not a word of `<lang>` in dictionary form and you cannot tell which word was meant
  (a fragment like `prostu` from `po prostu`, a misread form);
- it is a function word: pronoun, article, preposition, conjunction, particle, interjection,
  a bare number word, or a filler (`gonna`, `whoa`, `okay`, `ну`);
- it is a name, a title like `mister`/`pan` used as a form of address only, or vulgar/offensive;
- its frequency obviously comes from a function-word homograph (Polish `nie`, German `einen`).

Keep everyday adverbs (`here`, `today`, `always`, `quickly`) and modal verbs (`can`, `must`).

## When keeping

- If `lemma` is an inflected form, use the dictionary form of `<lang>` (`dzieje` → `dziać się`
  only if that is what the frequency means; otherwise reject).
- Pick the meaning a learner most needs for this word in everyday speech. For a word that is
  both a common verb and a common noun (`watch`, `stop`), prefer the meaning given by `pos`
  unless it is clearly the rarer one.
- Rows from our sets: keep their forms as they are, unless they carry the wrong meaning for
  the frequent word (`stop` from the transport set means "bus stop"; the frequent `stop` is the
  verb) — then write the right forms.
- Wiktionary rows: correct every translation that is wrong, rare, too formal, or the wrong
  sense; fill every language in `missing`. All eight languages must be in `forms`.

## How forms are written

Exactly as in `sets/*/words.json` (see `sets/home/words.json`, `sets/everyday-verbs/words.json`):

- A form is a string, or `{ "text": …, "alternates": [ … ] }`. Alternates are other answers
  that are just as right (true synonyms, the other aspect of a Slavic verb), at most three.
- `en` always as an object with `partOfSpeech`: `noun`, `verb`, `adjective` or `adverb`.
  English verbs start with `to ` (`to hold`).
- German, Spanish, French and Italian nouns: the text with its article (`das Haus`,
  `la casa`, `l'école`, `lo zaino`), and the bare word as the first alternate
  (`{ "text": "das Haus", "alternates": ["Haus"] }`). A noun used in the plural takes the
  plural article (`die Leute`, `gli occhiali`).
- Russian, Ukrainian and Polish verbs: the imperfective as the text and the perfective as an
  alternate (`{ "text": "держать", "alternates": [] }`, `{ "text": "brać", "alternates": ["wziąć"] }`).
  When `lemma` is the perfective, the `<lang>` form still leads with the imperfective.
- Adjectives in the masculine singular dictionary form. No stress marks, no
  abbreviations like `sth`/`kogoś` unless the word needs them (`dbać o`).
- Lowercase, except German nouns and what the language always capitalises.
- Ukrainian is Ukrainian, not Russian (`питання`, not `вопрос`); Russian keeps `ё` where the
  dictionary has it.
