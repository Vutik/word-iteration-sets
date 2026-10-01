# Word Iteration sets

Ready-made word sets for the WordsIteration app, served as static files through GitHub Pages:

**https://vutik.github.io/word-iteration-sets/index.json**

The app downloads `index.json`, shows the sets whose languages cover the learner's pair, and
downloads the words of the one the learner picks. Edit the files, rebuild the index, push, and
Pages republishes in a minute or two.

## Layout

Every set is its own folder; nothing is kept in one big file.

```
sets/
  fruits/
    set.json        what the set is: title, languages, cover, where the words are
    words.json      the words, in our own format
    cover.png
  kitchen-en-ru/
    set.json
    words.txt       or the words as a tab-separated list
    cover.png
index.json          GENERATED from every sets/*/set.json — do not edit by hand
tools/build.py      validates every set and writes index.json; CI runs it with --check
tools/make_cover.py draws a gradient cover with a title
```

The folder name is the set's **id**. The app uses it to recognise a set it has already imported,
so never rename or reuse a folder.

## Adding a set

1. Make `sets/<id>/` (lowercase, kebab-case).
2. Put the words in `words.json` or `words.txt`, or find a direct link to an `.apkg`.
3. Write `set.json`.
4. Optionally draw a cover:
   `python3 tools/make_cover.py sets/<id>/cover.png "Title" "#FF7A59" "#FFC15E"`.
5. `python3 tools/build.py` — it must print `0 errors`, and it rewrites `index.json`.
6. Commit the set folder **and** `index.json`, push.

## `set.json`

```json
{
  "title": { "en": "Fruits", "ru": "Фрукты", "pl": "Owoce" },
  "description": { "en": "Twelve everyday fruits." },
  "languages": ["en", "ru", "pl"],
  "image": "cover.png",
  "level": "A1",
  "tags": ["food"],
  "updated": "2026-10-01",
  "source": { "type": "json", "url": "words.json" }
}
```

| Field | Required | Meaning |
|---|---|---|
| `title` | yes | Language code → text. `en` is required as the fallback. The app shows the title in the language being learned with the learner's own under it, and names an added set `Owoce — Фрукты`. Give a title in every language of the set. |
| `description` | no | Same shape as `title`. |
| `languages` | yes | ISO 639-1 codes the set contains, at least two. The app offers a set when both languages of the learner's pair are listed. |
| `image` | no | Cover: a file in the folder or an `https://` URL. PNG, JPEG, WebP or HEIC; iOS does not draw SVG. |
| `level` | no | CEFR level, `A1` … `C2`. |
| `tags` | no | Free-form strings. |
| `updated` | no | `YYYY-MM-DD`; bump it when the words change. |
| `source` | yes | Where the words are, see below. |

URLs in `set.json` are relative to the set folder, or absolute `https://`. In `index.json` the
build rewrites them relative to the repository root (`sets/fruits/words.json`) and adds `id` and
`wordCount`.

### Sources

**`json`** — words in our own format, `words.json` in the set folder:

```json
{ "type": "json", "url": "words.json" }
```

**`text`** — one word per line, two cells separated by a tab. `columns` names the language of each
cell in order. Inside a cell `;` separates wordings that are all accepted (`сковорода;сковородка`):

```json
{ "type": "text", "url": "words.txt", "columns": ["en", "ru"] }
```

**`anki`** — an Anki package (`.apkg`) in the folder or a direct `https://` download link. The set
has exactly two `languages`, in the order of the note fields: the first field is `languages[0]`,
the second `languages[1]`. `deck` optionally picks one deck of a package that holds several,
with the decks nested in it; a nested deck is written `Parent / Child`:

```json
{ "type": "anki", "url": "https://example.org/decks/polish-1000.apkg", "deck": "Polish 1000" }
```

## `words.json`

```json
{
  "format": 1,
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
          "translations": { "ru": "Я ем яблоко каждый день.", "pl": "Codziennie jem jabłko." }
        }
      ],
      "image": "apple.png"
    }
  ]
}
```

- Every word needs a form in every language of the set. A form is a plain string, or an object
  with `text` and optional `alternates` (also accepted as answers), `transcription` and
  `partOfSpeech`.
- `examples` and `image` (relative to the set folder, or `https://`) are optional. A word without
  an image gets one the usual way after import.

One word carries all its languages, so one set serves every pair it covers: `fruits` trains
en↔ru, en↔pl and ru↔pl.

## Sets

| id | Languages | Source | Words |
|---|---|---|---|
| `fruits` | en ru pl | json | 12 |
| `family` | en ru pl | json | 15 |
| `colors` | en ru pl | json | 11 |
| `travel-en-ru` | en ru | json | 18 |
| `kitchen-en-ru` | en ru | text | 15 |
| `city-pl-ru` | pl ru | json | 17 |
| `polish-everyday-verbs` | pl ru | text | 15 |
