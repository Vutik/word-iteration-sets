# Picture library for the sets

One picture per word of the ready-made sets, chosen once by a person and hosted here on GitHub
Pages, so the app no longer searches six sources on the phone for every word of a set it
imports (slow on a first launch). Every picture may be used in a paid app: ARASAAC, whose licence
forbids that, is not a source (the app's `docs/MONETIZATION.md` §9, `docs/IMAGE-SOURCES.md`).

```
tools/images/library.py     fetch → gallery → apply (below)
tools/images/catalog.json   what every network source answered for every query — in git
tools/images/.cache/        git-ignored: downloads, candidates, the gallery page, logs
  library/candidates.json   per concept: its words and its candidates (files, licences)
  library/candidates/<key>/<n>.webp
  library/index.html        the gallery
  library/http/             every HTTP response, by URL hash (re-runs cost nothing)
  sources/                  one-time downloads: Mulberry, Fluent, Noto, Twemoji
  fetch-all.log             the last full run
images/<key>.webp           the chosen pictures, served next to the sets (after apply)
```

## Concepts

A *concept* is one English text with its part of speech, across all sets (~3 100). Its key is the
slug of the English text (`ice-cream`), with `-<pos>` added only when the same text appears with
two parts of speech (`light-noun`, `light-adjective`). Every word of every set with that English
form gets the same picture. Search is by the English form: every source has far more under English.

## Sources, in order

Each source is one function in `SOURCES` in `library.py`; Commons search stays last, as extra
alternates that are never the default pick. Up to 14 candidates per concept (`MAX_CANDIDATES`).

| Source | Licence | How it matches | Kept where |
|---|---|---|---|
| OpenMoji | CC BY-SA 4.0 | CLDR emoji keywords (the app's `EmojiKeywords.json`) | CDN, HTTP cache |
| Mulberry Symbols | CC BY-SA 4.0 | its English symbol names (also the base form of a verb) | release zip in `sources/` |
| Fluent Emoji 3D and Flat | MIT | same emoji as OpenMoji, by code point | sparse clone in `sources/` |
| Noto Emoji | Apache 2.0 | same emoji, by code point | sparse clone |
| Twemoji | CC BY 4.0 | same emoji, SVG rasterised at 640 px | sparse clone |
| Wikidata P18 | per file (Commons: free only) | item labelled with the word → its image | `catalog.json` |
| Wikipedia | per file | the article's lead image, Commons files only | `catalog.json` |
| Openverse | CC, `license_type=commercial` | photo search | `catalog.json` |
| Commons search | per file | keyword search, loose | `catalog.json` |

A candidate is kept only when its licence allows commercial use: CC0, public domain, CC BY,
CC BY-SA, MIT, Apache, OpenMoji. NC, ND, fair use and unknown are dropped (`is_commercial`), and
counted in the run's summary. They stay in `catalog.json`, marked `"commercial": false`.

**Sensitive words** (`sensitive.json`, bundled in the app too as
`WordsIteration/Providers/Images/SensitivePictures.json` — keep the two identical): a photo search
for a body part or a wound finds nudity and gore, so the words in `words` (back, chest, skin,
wound, …) get pictures from the drawings only — emoji sets and Mulberry — and a Commons file whose
title or any category matches `blocked` (nude, topless, porn, corpses, …) is dropped for every
word; the categories are asked 50 files a request and kept in `catalog.json` (`categories|File:X`).
Two more layers feed `screen`: `nsfw.py` scores every downloaded picture for nudity with a local
model (Falconsai/nsfw_image_detection, in a venv under `.cache/venv`, see its docstring) into
`nsfw.json` (in git, by original URL); a photo at 0.8 or more (`NSFW_THRESHOLD`) is hidden — drawings
never are. And "hide" under a picture in the gallery writes `hidden.json` (in git) through the
review server. After changing the list, after `nsfw.py`, run `screen`: it marks the candidates now ruled out `"hidden"` in
`candidates.json`, keeping every index, so the choices made stay valid; the gallery leaves them
out, `apply` refuses them, and a pick of a hidden one is taken out of `choices.json` to be
reviewed again.

Every candidate is converted to WebP, 640 px on the long side at most (`WIDTH`): the study card
shows a picture at up to 220 pt on an iPhone and 280 pt on an iPad, ×3. About 30 KB a picture.

## The catalog — no source is asked twice

`catalog.json` records what each *network* source (Wikidata, Wikipedia, Openverse, Commons)
returned for each query: `"<source>|<query>|<first|broad>": {"at": date, "candidates": [...]}`,
with the original URL, the URL of the scaled copy, author, licence and file page. A run reads it
before asking anyone, so a re-run — on another Mac, or after `.cache/` is deleted — makes no
look-up that is already in it (only the image files are downloaded again, if the HTTP cache is
gone). A source that did not ask (Openverse out of budget) records nothing, and the next run asks
it. The file is written atomically, so a run stopped halfway leaves the previous one. Commit it
after every run; it is the expensive part.

The offline sources (emoji sets, Mulberry) are not in it: they are matched locally in a second.

## Limits

- Wikimedia (Wikidata, Wikipedia, Commons): at most 140 requests a minute (`WIKIMEDIA`), with a
  descriptive User-Agent, under their 200.
- Openverse: anonymous 20 a minute and 200 a day; with a key 60 and 10 000. The key (from
  `POST /v1/auth_tokens/register/`, verified by email) is in `~/.config/wordsiteration/openverse.json`,
  outside the repo, or in `OPENVERSE_CLIENT_ID` / `OPENVERSE_CLIENT_SECRET`.
- A full run over all sets: a few hours the first time; minutes when the catalog and HTTP cache
  have it all.

## Commands

```sh
python3 tools/images/library.py fetch                    # all sets (resumable: re-run after a stop)
python3 tools/images/library.py fetch food-and-drinks    # some sets
python3 tools/images/library.py gallery                  # (re)write .cache/library/index.html
python3 tools/images/library.py review                   # serve it; choices saved to tools/images/choices.json
tools/images/.cache/venv/bin/python tools/images/nsfw.py # score new pictures for nudity (after a fetch)
python3 tools/images/library.py screen                   # hide what sensitive.json, nsfw.json, hidden.json rule out
python3 tools/images/library.py fetch --research tools/images/choices.json  # re-search the marked words
python3 tools/images/library.py apply tools/images/choices.json [--root DIR] # write images/ and words.json
python3 tools/build.py                                   # then validate and rewrite index.json
```

## The gallery

`python3 tools/images/library.py review` serves it at http://127.0.0.1:8765/ and writes every
choice, as it is made, to `tools/images/choices.json` (in git: the record of the review, spread
over as many days as it takes; commit it now and then). A reload rebuilds the page, so it shows
what a running fetch has added. Opened as a plain file, the page keeps choices in that browser only.

**Review mode** (the button at the top): one word at a time, only the ones not reviewed yet, big
pictures. 1–9 or a click picks, Enter takes the first, 0/x = no picture, r = search again (with
an optional query), s = skip for now, u = back, Esc = the list. A word is reviewed once picked,
set to no picture, or sent to search again; it comes back to the queue when `fetch --research`
has added candidates (marked "new"). The set and part-of-speech filters apply.

**The list** (`open tools/images/.cache/library/index.html` also works). One row per concept: its English and Russian, part
of speech, sets, and its candidates with source and licence. The first candidate is the default.

- Click a candidate to pick it; "no picture" for a word a picture would only confuse (abstract
  verbs, adverbs).
- "Search again", with an optional better English query ("apple fruit"), for a word with nothing
  good: `fetch --research` adds new candidates (more Openverse and Commons results, skipping the
  ones shown) after the existing ones, so earlier picks keep their numbers.
- Filters by set, status and part of speech; counts at the top.
- `choices.json` holds only the words reviewed: `{key: index | null | {"pick": index|null,
  "research": "query", "seen": n}}` (`seen` = candidates when it was marked; `fetch --research`
  skips a word that already has more). A word not in it gets its default pick from `apply`.
  "Export choices" / "Import choices" move it between browsers by hand.

## What apply writes

For each concept in `choices.json` (a concept not in it gets its default pick) it copies the chosen
WebP to `images/<key>.webp` and gives every word with that English form:

```json
"image": {
  "path": "images/house.webp",
  "source": "https://upload.wikimedia.org/…/House.jpg",
  "attribution": {"author": "…", "license": "CC BY-SA 4.0", "licenseURL": "…", "page": "…"}
}
```

or `"image": {"none": true}`. `source` is the original, for the app to fall back on when our copy
cannot be fetched. A word whose `image` is a plain string (a picture added by hand before the
library) is left alone. `tools/build.py` checks the object: the file exists, attribution present.

It also writes `images/index.json`, the library as a lookup by English text, for words the user
adds that are in no set:

```json
{"pictures": {"house": [{"pos": "noun", "file": "images/house.webp", "source": "https://…",
                         "attribution": {"author": "…", "license": "…", "licenseURL": "…", "page": "…"}}]},
 "version": 1}
```

The key is the English text lowercased, trimmed, inner whitespace collapsed, Unicode NFC. One entry
per concept that got a picture (none for `{"none": true}`); one text with two parts of speech has
two entries (`pos` is `null` when the concept has none). Keys are sorted, one text per line.
`tools/build.py` does not look at it.

## In the app

A set word with `image.path` takes our copy, then `image.source`, then the usual picture chain;
with `{"none": true}` it gets no picture and is not searched. Any other word the user adds is
looked up in `images/index.json` first (`LibraryImageProvider`, first in the chain, English
queries only, also without a leading "to " / "a " / "an " / "the "): a match gets our picture
with its attribution. See the app's `docs/MONETIZATION.md` §9.

## Adding a source

Write `source_<name>(ctx) -> [candidate]` (fields: `source`, `remote`, `fetch` or `local`,
`commercial`, `attribution` {author, license, licenseURL, page}, optional `marker` to merge the
same Commons file found twice), put it in `SOURCES` in chain order, add it to `NETWORK_SOURCES`
when it asks the network, and to the table above. Check its licence first: commercial use,
keeping copies and hosting them here must all be allowed (`docs/IMAGE-SOURCES.md` in the app).
