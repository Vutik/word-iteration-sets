# Word Iteration sets

Ready-made word sets for the WordsIteration app, served as static JSON through GitHub Pages:

**https://vutik.github.io/word-iteration-sets/index.json**

The app downloads `index.json`, shows the sets whose languages cover the learner's pair, and imports
the one the learner picks. Nothing here is compiled; edit the files, push, and Pages republishes in a
minute or two.

## Layout

```
index.json                 catalogue of every set
sets/<id>.json             a set stored in our own format ("json" source)
sets/<id>.txt              a set stored as a word list ("text" source)
images/<id>.png            set covers
tools/check.py             validator, also run by CI on every push
tools/make_cover.py        draws a gradient cover with a title
```

## URLs

Every `url` and `image` is either **relative to the file it is written in** (resolved like a link on
a web page: `sets/fruits.json` in `index.json` means
`https://vutik.github.io/word-iteration-sets/sets/fruits.json`) or an absolute `https://` URL
somewhere else. Images must be PNG, JPEG, WebP or HEIC; iOS does not draw SVG.

## `index.json`

```json
{
  "format": 1,
  "sets": [
    {
      "id": "fruits",
      "title": { "en": "Fruits", "ru": "Фрукты", "pl": "Owoce" },
      "description": { "en": "Twelve everyday fruits." },
      "languages": ["en", "ru", "pl"],
      "image": "images/fruits.png",
      "level": "A1",
      "tags": ["food"],
      "wordCount": 12,
      "updated": "2026-10-01",
      "source": { "type": "json", "url": "sets/fruits.json" }
    }
  ]
}
```

| Field | Required | Meaning |
|---|---|---|
| `format` | yes | Format version, currently `1`. The app ignores catalogues with a newer major format. |
| `id` | yes | Stable kebab-case id. The app uses it to recognise a set it has already imported, so never reuse or rename it. |
| `title` | yes | Language code → text. `en` is required as the fallback; the app picks the learner's known language when present. |
| `description` | no | Same shape as `title`. |
| `languages` | yes | ISO 639-1 codes the set contains, at least two. The app offers a set when both languages of the learner's pair are listed. |
| `image` | no | Cover image, relative or `https://`. |
| `level` | no | CEFR level, `A1` … `C2`. |
| `tags` | no | Free-form strings for filtering. |
| `wordCount` | no | Shown before download. The validator checks it for `json` and `text` sources. |
| `updated` | no | `YYYY-MM-DD`; lets the app tell a learner the set changed. |
| `source` | yes | Where the words are, see below. |

### Sources

**`json`**: words in our own format, kept in this repository.

```json
{ "type": "json", "url": "sets/fruits.json" }
```

**`text`**: a word list, one pair per line, the two columns separated by a tab. `columns` names the
language of each column in order. Inside a cell, commas separate several translations and semicolons
separate alternate wordings, as in the app's text import.

```json
{ "type": "text", "url": "sets/polish-everyday-verbs.txt", "columns": ["pl", "ru"] }
```

**`anki`**: an Anki package (`.apkg`), in this repository or a direct `https://` download link
elsewhere. `deck` optionally picks one deck from a package that holds several. The app maps the note
fields to `languages` the way its Anki import does.

```json
{ "type": "anki", "url": "https://example.org/decks/polish-1000.apkg", "deck": "Polish 1000" }
```

## Set file (`json` source)

```json
{
  "format": 1,
  "id": "fruits",
  "languages": ["en", "ru", "pl"],
  "words": [
    {
      "forms": {
        "en": { "text": "apple", "transcription": "ˈæp.əl", "partOfSpeech": "noun" },
        "ru": "яблоко",
        "pl": { "text": "jabłko", "alternates": ["jabłuszko"] }
      },
      "examples": [
        {
          "language": "en",
          "text": "I eat an apple every day.",
          "translations": { "ru": "Я ем яблоко каждый день." }
        }
      ],
      "image": "../images/apple.png"
    }
  ]
}
```

- `id` and `languages` must match the entry in `index.json`.
- Every word needs a form in every set language. A form is either a plain string or an object with
  `text` and optional `alternates`, `transcription` and `partOfSpeech`.
- `examples` and `image` are optional. A word's `image` is relative to the set file.

One word carries all its languages, so a single set serves every pair it covers: the `fruits` set
trains en↔ru, en↔pl and ru↔pl.

## Adding a set

1. Put the words in `sets/<id>.json` or `sets/<id>.txt`, or find a direct link to an `.apkg`.
2. Optionally draw a cover:
   `python3 tools/make_cover.py images/<id>.png "Title" "#FF7A59" "#FFC15E"`.
3. Add an entry to `index.json`.
4. Run `python3 tools/check.py`; it must print `0 errors`.
5. Commit and push.
