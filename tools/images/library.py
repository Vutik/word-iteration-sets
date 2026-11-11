#!/usr/bin/env python3
"""A picture library of our own for the ready-made sets, so the app need not search pictures
on the phone. Standard library only; needs cwebp (/opt/homebrew/bin/cwebp) and macOS sips.

    python3 tools/images/library.py fetch [set-id ...]    candidates for every concept (no ids: all sets)
    python3 tools/images/library.py gallery               .cache/library/index.html to review them
    python3 tools/images/library.py apply choices.json    copy the chosen pictures to images/ and
                                                          write "image" into sets/*/words.json
    python3 tools/images/library.py apply choices.json --root DIR   work on a copy of the repo

A concept is a unique (English text, part of speech). Its key is the slug of the English text,
plus "-<pos>" only when the same text occurs with two parts of speech.

Candidates are gathered in the app's fallback chain order (the Swift providers in
WordsIteration/Providers/Images are mirrored): OpenMoji (offline match through
EmojiKeywords.json), Wikidata P18, Wikipedia summary thumbnail, Openverse (commercial licences
only), then Commons keyword search as extra alternates. ARASAAC is NOT used: its licence is
non-commercial. Wikipedia files that are not on Commons (fair use) are skipped.

Every HTTP response is cached under tools/images/.cache/library/http, so reruns are cheap.
"""
import copy
import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = Path(__file__).resolve().parent / ".cache" / "library"
HTTP_CACHE = CACHE / "http"
CANDIDATES_DIR = CACHE / "candidates"
CANDIDATES_JSON = CACHE / "candidates.json"
# What every network source answered for every query — kept in git (unlike .cache), so a rerun on
# any machine, or after .cache is wiped, asks no source again, and the URLs, licences and authors
# stay for later use: {"<source>|<query>|<first|broad>": {"at": date, "candidates": [...]}}.
# Non-commercial results are in it too, marked "commercial": false; the gallery drops them.
CATALOG_JSON = Path(__file__).resolve().parent / "catalog.json"
NETWORK_SOURCES = {"wikidata", "wikipedia", "openverse", "commons"}
SENSITIVE_JSON = Path(__file__).resolve().parent / "sensitive.json"
REVIEW_JSON = Path(__file__).resolve().parent / "choices.json"
HIDDEN_JSON = Path(__file__).resolve().parent / "hidden.json"   # {key: {index: reason}}: hidden by hand in the gallery
NSFW_JSON = Path(__file__).resolve().parent / "nsfw.json"       # {original URL: P(nsfw)}, from nsfw.py
NSFW_THRESHOLD = 0.8


def hidden_by_hand():
    try:
        return json.loads(HIDDEN_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
KEYWORDS = ROOT.parent / "WordsIteration" / "Providers" / "Images" / "EmojiKeywords.json"
SOURCES_DIR = Path(__file__).resolve().parent / ".cache" / "sources"
CWEBP = "/opt/homebrew/bin/cwebp"
USER_AGENT = "WordsIterationSetsTool/1.0 (https://vutik.github.io/word-iteration-sets/)"
WIDTH = 640
MAX_CANDIDATES = 14
COMMONS_EXTRA = 4
CONCURRENCY = 4

# MARK: - Words


def form_text(form):
    return form if isinstance(form, str) else (form or {}).get("text", "")


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def collect_concepts(root):
    """{key: {en, pos, ru, sets}} over every set under root/sets."""
    found = {}  # (en, pos) -> {ru, sets}
    for words_json in sorted((root / "sets").glob("*/words.json")):
        set_id = words_json.parent.name
        data = json.loads(words_json.read_text(encoding="utf-8"))
        for word in data["words"] if isinstance(data, dict) else data:
            forms = word["forms"]
            en = form_text(forms.get("en"))
            if not en:
                continue
            pos = forms["en"].get("partOfSpeech", "") if isinstance(forms["en"], dict) else ""
            entry = found.setdefault((en, pos), {"ru": form_text(forms.get("ru")), "sets": []})
            if set_id not in entry["sets"]:
                entry["sets"].append(set_id)
    poses = {}
    for en, pos in found:
        poses.setdefault(slug(en), set()).add(pos)
    concepts = {}
    for (en, pos), entry in found.items():
        key = slug(en)
        if len(poses[key]) > 1:
            key += "-" + (slug(pos) or "x")
        concepts[key] = {"en": en, "pos": pos, "ru": entry["ru"], "sets": entry["sets"]}
    return concepts


def concept_key(concepts_by_pair, en, pos):
    return concepts_by_pair.get((en, pos))


# MARK: - HTTP with a disk cache and rate limits


class Limiter:
    """At most `per_minute` calls a minute, spread out evenly; thread-safe."""

    def __init__(self, per_minute):
        self.interval = 60.0 / per_minute
        self.next = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next)
            self.next = at + self.interval
        if at > now:
            time.sleep(at - now)


WIKIMEDIA = Limiter(140)
OTHER = Limiter(300)
stats = {"network": 0, "cached": 0, "catalog": 0, "dropped": 0, "sensitive": 0, "errors": []}


# MARK: - Sensitive words and files (sensitive.json)

_sensitive = json.loads(SENSITIVE_JSON.read_text(encoding="utf-8"))
SENSITIVE_WORDS = {w.lower() for w in _sensitive["words"]}
BLOCKED = re.compile(r"\b(" + "|".join(_sensitive["blocked"]) + r")\b", re.I)


def is_sensitive(text):
    """A word whose pictures come from drawings only: a photo search finds nudity or gore."""
    t = re.sub(r"\s+", " ", (text or "").strip().lower())
    t = re.sub(r"^(to|a|an|the) ", "", t)
    return t in SENSITIVE_WORDS


def commons_flags(titles):
    """{'File:X': the blocked title or category} for those of `titles` that match `blocked`.
    Categories are asked 50 titles a request and recorded in the catalog, so it costs nothing twice."""
    titles = [t for t in dict.fromkeys(titles) if t.startswith("File:")]
    missing = [t for t in titles if f"categories|{t}" not in catalog]
    for i in range(0, len(missing), 50):
        chunk = missing[i:i + 50]
        found = {t: [] for t in chunk}
        cont = {}
        while True:
            try:
                data = get_json(api("commons.wikimedia.org", action="query", format="json", formatversion=2,
                                    titles="|".join(chunk), prop="categories", cllimit="max", redirects=1, **cont))
            except NotFound:
                break
            names = {n["to"]: n["from"] for n in data.get("query", {}).get("normalized", [])}
            names.update({r["to"]: r["from"] for r in data.get("query", {}).get("redirects", [])})
            for page in data.get("query", {}).get("pages", []):
                title = names.get(page["title"], page["title"])
                found.setdefault(title, []).extend(c["title"].removeprefix("Category:") for c in page.get("categories", []))
            if "continue" not in data:
                break
            cont = {k: v for k, v in data["continue"].items()}
        with catalog_lock:
            for t, cats in found.items():
                catalog[f"categories|{t}"] = {"at": time.strftime("%Y-%m-%d"), "categories": sorted(set(cats))}
    out = {}
    for t in titles:
        for text in [t.removeprefix("File:")] + catalog.get(f"categories|{t}", {}).get("categories", []):
            match = BLOCKED.search(text.replace("_", " "))
            if match:
                out[t] = f"{text}"
                break
    return out


def commons_title(candidate):
    """The Commons file title of a candidate hosted there, from its file page; else None."""
    page = (candidate.get("attribution") or {}).get("page") or ""
    m = re.match(r"https?://commons\.wikimedia\.org/wiki/(File:.+)$", page)
    return urllib.parse.unquote(m.group(1)).replace("_", " ") if m else None
stats_lock = threading.Lock()
catalog, catalog_lock = {}, threading.Lock()


class SourceSkipped(Exception):
    """A source did not ask (out of budget for the run): nothing to record in the catalog."""


def load_catalog():
    try:
        catalog.update(json.loads(CATALOG_JSON.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass


def save_catalog():
    with catalog_lock:
        lines = [json.dumps(k, ensure_ascii=False) + ": " + json.dumps(catalog[k], ensure_ascii=False, sort_keys=True)
                 for k in sorted(catalog)]
    # Through a temporary file: a run stopped mid-write must not leave half a catalog behind.
    partial = CATALOG_JSON.with_suffix(".json.partial")
    partial.write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8")
    partial.replace(CATALOG_JSON)


class NotFound(Exception):
    pass


def http(url, headers=None, data=None, cache=True, limiter=None):
    """Bytes of a URL. Cached on disk (404s too); retried on 429/5xx. Raises NotFound or OSError."""
    name = hashlib.sha256((url + (data.decode() if data else "")).encode()).hexdigest()
    body_path, status_path = HTTP_CACHE / name, HTTP_CACHE / (name + ".status")
    if cache and status_path.exists():
        with stats_lock:
            stats["cached"] += 1
        if status_path.read_text() == "404":
            raise NotFound(url)
        return body_path.read_bytes()
    if limiter is None:
        limiter = WIKIMEDIA if "wikimedia.org" in url or "wikipedia.org" in url or "wikidata.org" in url else OTHER
    last = None
    for attempt in range(5):
        limiter.wait()
        request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
        try:
            with stats_lock:
                stats["network"] += 1
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
            HTTP_CACHE.mkdir(parents=True, exist_ok=True)
            body_path.write_bytes(body)
            status_path.write_text("200")
            return body
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                HTTP_CACHE.mkdir(parents=True, exist_ok=True)
                status_path.write_text("404")
                raise NotFound(url)
            last = exc
            if exc.code == 429 or exc.code >= 500:
                retry = exc.headers.get("Retry-After", "")
                time.sleep(float(retry) if retry.isdigit() else 5 * (attempt + 1))
                if exc.code == 429 and "openverse" in url:
                    raise
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
            time.sleep(3 * (attempt + 1))
    raise OSError(f"{url}: {last}")


def get_json(url, **kwargs):
    return json.loads(http(url, **kwargs))


def api(host, **params):
    return f"https://{host}/w/api.php?" + urllib.parse.urlencode(params)


# MARK: - Openverse (quota persisted on disk)


class Openverse:
    STATE = CACHE / "openverse-state.json"

    def __init__(self):
        self.lock = threading.Lock()
        # The environment, else ~/.config/wordsiteration/openverse.json (the answer of
        # /v1/auth_tokens/register/, kept out of the repo).
        saved = {}
        try:
            saved = json.loads((Path.home() / ".config/wordsiteration/openverse.json").read_text())
        except (OSError, ValueError):
            pass
        self.client_id = os.environ.get("OPENVERSE_CLIENT_ID") or saved.get("client_id")
        self.client_secret = os.environ.get("OPENVERSE_CLIENT_SECRET") or saved.get("client_secret")
        self.token = None
        self.exhausted = False
        self.minute, self.day = (60, 10000) if self.client_id and self.client_secret else (20, 200)
        self.skipped = 0
        try:
            self.times = [t for t in json.loads(self.STATE.read_text()) if t > time.time() - 86400]
        except (OSError, ValueError):
            self.times = []

    def _spend(self):
        """Reserve one request; False when the daily budget is gone. Waits out the minute budget."""
        while True:
            with self.lock:
                now = time.time()
                self.times = [t for t in self.times if t > now - 86400]
                if len(self.times) >= self.day:
                    return False
                recent = [t for t in self.times if t > now - 60]
                if len(recent) < self.minute - 1:
                    self.times.append(now)
                    CACHE.mkdir(parents=True, exist_ok=True)
                    self.STATE.write_text(json.dumps(self.times))
                    return True
                wait = recent[0] + 61 - now
            time.sleep(max(wait, 1))

    def _token(self):
        if self.token or not (self.client_id and self.client_secret):
            return self.token
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret,
        }).encode()
        try:
            self.token = json.loads(http(
                "https://api.openverse.org/v1/auth_tokens/token/", data=body, cache=False,
                headers={"Content-Type": "application/x-www-form-urlencoded"}))["access_token"]
        except Exception as exc:  # anonymous is still fine
            print(f"  openverse: OAuth failed ({exc}), going on anonymously", flush=True)
            self.client_id = None
            self.minute, self.day = 20, 200
        return self.token

    def search(self, term, count=1):
        """Results (dicts) for the term, or [] — also when the budget is spent."""
        url = "https://api.openverse.org/v1/images/?" + urllib.parse.urlencode({
            "q": term, "page_size": 5 if count <= 1 else 10, "license_type": "commercial", "mature": "false", "category": "photograph",
            "language": "en"})
        name = hashlib.sha256(url.encode()).hexdigest()
        if not (HTTP_CACHE / (name + ".status")).exists():
            if self.exhausted or not self._spend():
                self.exhausted = True
                self.skipped += 1
                raise SourceSkipped()
            token = self._token()
        else:
            token = None
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            data = get_json(url, headers=headers, limiter=OTHER)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                self.exhausted = True
                self.skipped += 1
                print("  openverse: 429, stopping Openverse for this run", flush=True)
                raise SourceSkipped()
            raise
        except NotFound:
            return []
        return data.get("results", [])


# MARK: - Providers


def fold(text):
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c)).strip()


EN_PARTICLES = ["to ", "the ", "a ", "an "]


def search_term(en):
    term = en.strip().lower()
    for prefix in EN_PARTICLES:
        if term.startswith(prefix):
            term = term[len(prefix):]
            break
    return term.strip()


FUNCTION_WORDS = {"the", "and", "for", "with", "from", "into", "not", "are", "was", "has", "his", "her"}


class Emoji:
    def __init__(self):
        data = json.loads(KEYWORDS.read_text(encoding="utf-8"))
        self.version = data.get("openmoji", "15.1.0")
        self.index = data["en"]

    def hexcodes(self, term):
        key = term.strip().lower()
        if key in self.index:
            return self.index[key]
        words = [w for w in re.split(r"[^\w-]+", key) if w]
        if len(words) < 2:
            return []
        heads = [w for w in reversed(words) if len(w) > 2 and w not in FUNCTION_WORDS][:2]
        for head in heads:
            if head in self.index:
                return self.index[head]
        return []

    def candidates(self, term):
        out = []
        for code in self.hexcodes(term)[:2]:
            url = f"https://cdn.jsdelivr.net/gh/hfg-gmuend/openmoji@{self.version}/color/618x618/{code}.png"
            out.append({
                "source": "openmoji", "remote": url, "fetch": url, "commercial": True,
                "attribution": {
                    "author": "OpenMoji", "license": "CC BY-SA 4.0",
                    "licenseURL": "https://creativecommons.org/licenses/by-sa/4.0/",
                    "page": f"https://openmoji.org/library/emoji-{code}/"},
            })
        return out


WIKIDATA_REJECTED = {
    "Q5", "Q101352", "Q202444", "Q4167410", "Q13406463", "Q532", "Q3957", "Q11424", "Q482994", "Q7366",
    "Q134556", "Q215380", "Q4830453", "Q16521", "Q12308941", "Q11879590", "Q3409032", "Q486972", "Q515",
    "Q23442", "Q737498", "Q5633421", "Q13442814", "Q571", "Q3305213", "Q7889", "Q1656682",
}


def plain_text(value):
    value = re.sub(r"<[^>]+>", "", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


COMMERCIAL_OK = re.compile(r"^(mit\b|apache|cc0|cc[ -]?by|pdm|public domain|pd\b|pd-|attribution\b|openmoji)", re.I)
COMMERCIAL_NO = re.compile(r"(\bnc\b|-nc|non-?commercial|\bnd\b|-nd|no ?derivs|fair use|non-free|nonfree|all rights reserved)", re.I)


def is_commercial(licence):
    """True for CC0, public domain, CC BY, CC BY-SA (and OpenMoji); NC, ND, fair use and unknown are False."""
    licence = (licence or "").strip()
    return bool(licence) and not COMMERCIAL_NO.search(licence) and bool(COMMERCIAL_OK.match(licence))


def commons_infos(titles):
    """{ 'File:X': candidate-dict-without-source } for Commons titles; one request for several."""
    titles = [t for t in dict.fromkeys(titles)]
    out = {}
    for i in range(0, len(titles), 20):
        chunk = titles[i:i + 20]
        try:
            data = get_json(api(
                "commons.wikimedia.org", action="query", format="json", formatversion=2, titles="|".join(chunk),
                prop="imageinfo", iiprop="url|extmetadata|mime", iiextmetadatafilter="Artist|LicenseShortName|LicenseUrl|ObjectName",
                iiurlwidth=WIDTH, redirects=1))
        except NotFound:
            continue
        for page in data.get("query", {}).get("pages", []):
            candidate = commons_candidate(page)
            if candidate:
                out[page["title"]] = candidate
        for redirect in data.get("query", {}).get("redirects", []):
            if redirect["to"] in out:
                out[redirect["from"]] = out[redirect["to"]]
        for norm in data.get("query", {}).get("normalized", []):
            if norm["to"] in out:
                out[norm["from"]] = out[norm["to"]]
    return out


def commons_candidate(page):
    info = (page.get("imageinfo") or [None])[0]
    if not info or not (info.get("mime") or "").startswith("image/"):
        return None
    meta = info.get("extmetadata") or {}
    value = lambda k: (meta.get(k) or {}).get("value")
    licence = value("LicenseShortName") or ""
    if not licence:
        return None
    fetch = info.get("thumburl") or info.get("url")
    if not fetch:
        return None
    return {
        "remote": info.get("url") or fetch, "fetch": fetch, "commercial": is_commercial(licence),
        "attribution": {
            "author": plain_text(value("Artist")), "license": licence, "licenseURL": value("LicenseUrl") or "",
            "page": info.get("descriptionurl", ""),
        },
    }


def wikidata_files(term):
    """Commons file titles of the first Wikidata match with a P18 (mirrors WikidataImageProvider)."""
    data = get_json(api("www.wikidata.org", action="wbsearchentities", format="json", formatversion=2, type="item",
                        limit=7, language="en", uselang="en", search=term))
    wanted = fold(term)
    matches = []
    for hit in data.get("search", []):
        matched = (hit.get("match") or {}).get("text") or hit.get("label")
        if matched and fold(matched) == wanted:
            matches.append(hit["id"])
    if not matches:
        return []
    entities = get_json(api("www.wikidata.org", action="wbgetentities", format="json", formatversion=2, props="claims",
                            languages="en", ids="|".join(matches)))["entities"]
    for qid in matches:
        claims = (entities.get(qid) or {}).get("claims", {})
        types = {c.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
                 for c in claims.get("P31", []) if isinstance(c.get("mainsnak", {}).get("datavalue", {}).get("value"), dict)}
        if types & WIKIDATA_REJECTED:
            continue
        for claim in claims.get("P18", []):
            name = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
            if isinstance(name, str):
                return ["File:" + name]
    return []


def wikipedia_file(term):
    """Commons file title of the article's lead image (mirrors WikipediaImageProvider)."""
    title = urllib.parse.quote(term.strip().replace(" ", "_"), safe="")
    try:
        summary = get_json(f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}?redirect=true")
    except NotFound:
        return []
    if summary.get("type") == "disambiguation":
        return []
    source = (summary.get("thumbnail") or summary.get("originalimage") or {}).get("source", "")
    match = re.match(r"https://(?:upload|thumb)\.wikimedia\.org/wikipedia/commons/(?:thumb/)?[0-9a-f]/[0-9a-f]{2}/([^/]+)", source)
    if not match:  # not on Commons: a local, possibly fair-use, file
        return []
    return ["File:" + urllib.parse.unquote(match.group(1)).replace("_", " ")]


def commons_search(term):
    """File titles in search-rank order, with their info, in one request (mirrors CommonsSearchImageProvider)."""
    try:
        data = get_json(api(
            "commons.wikimedia.org", action="query", format="json", formatversion=2, generator="search",
            gsrsearch=f"{term} filetype:bitmap", gsrnamespace=6, gsrlimit=10, prop="imageinfo",
            iiprop="url|extmetadata|mime", iiextmetadatafilter="Artist|LicenseShortName|LicenseUrl|ObjectName",
            iiurlwidth=WIDTH))
    except NotFound:
        return []
    pages = sorted(data.get("query", {}).get("pages", []), key=lambda p: p.get("index", 1 << 30))
    out = []
    for page in pages:
        candidate = commons_candidate(page)
        if candidate:
            out.append((page["title"], candidate))
    return out


def openverse_licence(result):
    kind, version = (result.get("license") or "").lower(), result.get("license_version") or ""
    if kind == "cc0":
        return "CC0 " + version if version else "CC0"
    if kind == "pdm":
        return "Public Domain Mark"
    return ("CC " + kind.upper() + " " + version).strip() if kind else ""


def openverse_candidates(openverse, term, count):
    out = []
    for result in openverse.search(term, count)[:count]:
        fetch = result.get("thumbnail") or result.get("url")
        if not fetch:
            continue
        licence = openverse_licence(result)
        out.append({
            "source": "openverse", "remote": result.get("url") or fetch, "fetch": fetch,
            "commercial": is_commercial(licence),
            "attribution": {
                "author": result.get("creator") or "", "license": licence,
                "licenseURL": result.get("license_url") or "", "page": result.get("foreign_landing_url") or ""},
        })
    return out


# MARK: - Images


def image_size(path):
    out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                         capture_output=True, text=True).stdout
    w = re.search(r"pixelWidth: (\d+)", out)
    h = re.search(r"pixelHeight: (\d+)", out)
    return (int(w.group(1)), int(h.group(1))) if w and h else (0, 0)


def to_webp(data, target):
    """Convert image bytes to webp (q 80, long side at most WIDTH). Returns (width, height) or None."""
    target.parent.mkdir(parents=True, exist_ok=True)
    source = target.with_suffix(".src")
    source.write_bytes(data)
    try:
        width, height = image_size(source)
        if not width:
            return None
        command = [CWEBP, "-quiet", "-q", "80", "-alpha_q", "90", "-mt"]
        if max(width, height) > WIDTH:
            command += ["-resize", str(WIDTH), "0"] if width >= height else ["-resize", "0", str(WIDTH)]
            scale = WIDTH / max(width, height)
            width, height = max(1, round(width * scale)), max(1, round(height * scale))
        result = subprocess.run(command + [str(source), "-o", str(target)], capture_output=True)
        if result.returncode != 0 or not target.exists():
            return None
        return width, height
    finally:
        source.unlink(missing_ok=True)


# MARK: - fetch



# MARK: - Local sources (one sparse clone or archive per project, cached under .cache/sources)

git_lock = threading.Lock()
raster_lock = threading.Lock()


def run(command, cwd=None):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise OSError(f"{' '.join(map(str, command))}: {result.stderr.strip()[:300]}")
    return result.stdout


def ensure_repo(name, url, patterns):
    """A shallow, blob-less, sparse clone of `url` in SOURCES_DIR/name holding only `patterns`."""
    folder = SOURCES_DIR / name
    with git_lock:
        if not (folder / ".ready").exists():
            if folder.exists():
                shutil.rmtree(folder)
            SOURCES_DIR.mkdir(parents=True, exist_ok=True)
            print(f"  cloning {name} (sparse, once)...", flush=True)
            run(["git", "clone", "--depth", "1", "--filter=blob:none", "--no-checkout", url, str(folder)])
            run(["git", "sparse-checkout", "init", "--no-cone"], cwd=folder)
            run(["git", "sparse-checkout", "set", "--no-cone", *patterns], cwd=folder)
            run(["git", "checkout"], cwd=folder)
            (folder / ".ready").write_text("1")
    return folder


def sparse_add(folder, paths):
    """Check out more paths of an already cloned sparse repo, in one call."""
    paths = [p for p in dict.fromkeys(paths) if not (folder / p).exists()]
    if not paths:
        return
    with git_lock:
        subprocess.run(["git", "sparse-checkout", "add", "--stdin"], cwd=folder, input="\n".join("/" + p for p in paths),
                       text=True, capture_output=True, check=True)
        run(["git", "checkout"], cwd=folder)


def rasterise(svg, width=WIDTH):
    """PNG bytes of an SVG at `width` px, through rsvg-convert (brew install librsvg)."""
    rsvg = shutil.which("rsvg-convert") or "/opt/homebrew/bin/rsvg-convert"
    out = SOURCES_DIR / "raster" / (hashlib.sha256(str(svg).encode()).hexdigest()[:20] + f"-{width}.png")
    with raster_lock:
        if not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            run([rsvg, "-w", str(width), "-f", "png", "-o", str(out), str(svg)])
    return out


def code_variants(hexcode):
    """OpenMoji hexcode '1F468-200D-1F4BB' -> lowercase code lists with and without FE0F."""
    parts = [p.lower() for p in hexcode.split("-")]
    bare = [p for p in parts if p != "fe0f"]
    return [parts, bare] if parts != bare else [bare]


class Mulberry:
    """Mulberry Symbols (CC BY-SA 4.0), from the release archive; matched by English name."""
    TAG = "v3.6.1"
    BASE = f"https://github.com/mulberrysymbols/mulberry-symbols/releases/download/{TAG}"

    def __init__(self):
        self.lock = threading.Lock()
        self.index = None

    def load(self):
        with self.lock:
            if self.index is not None:
                return self.index
            folder = SOURCES_DIR / "mulberry"
            if not (folder / ".ready").exists():
                folder.mkdir(parents=True, exist_ok=True)
                print("  downloading Mulberry Symbols (once)...", flush=True)
                for name in ("mulberry-symbols.zip", "symbol-info.csv"):
                    if not (folder / name).exists():
                        (folder / name).write_bytes(http(f"{self.BASE}/{name}", cache=False))
                run(["unzip", "-q", "-o", str(folder / "mulberry-symbols.zip"), "EN-symbols/*", "-d", str(folder)])
                (folder / ".ready").write_text("1")
            index = {}
            for svg in (folder / "EN-symbols").glob("*.svg"):
                name = svg.stem.lower().replace("_", " ")
                index.setdefault(name, svg)
                base = re.sub(r"\s*\(.*?\)", "", name).strip()
                index.setdefault(base, svg)
            self.index = index
            return index

    def candidates(self, term):
        index = self.load()
        key = term.strip().lower()
        found = index.get(key)
        if not found:
            words = key.split()
            if len(words) > 1:  # "red apple" has no symbol of its own; do not guess
                return []
        if not found:
            return []
        name = found.stem
        return [{
            "source": "mulberry", "local": rasterise(found),
            "remote": f"https://github.com/mulberrysymbols/mulberry-symbols/blob/master/EN/symbols/{urllib.parse.quote(name)}.svg",
            "commercial": True,
            "attribution": {"author": "Mulberry Symbols © Steve Lee", "license": "CC BY-SA 4.0",
                            "licenseURL": "https://creativecommons.org/licenses/by-sa/4.0/",
                            "page": "https://mulberrysymbols.org/"},
        }]


class Fluent:
    """Microsoft Fluent Emoji (MIT): 3D PNG and Flat SVG, found by Unicode through each metadata.json."""
    URL = "https://github.com/microsoft/fluentui-emoji"

    def __init__(self):
        self.lock = threading.Lock()
        self.by_code = None

    def load(self):
        with self.lock:
            if self.by_code is None:
                folder = ensure_repo("fluentui-emoji", self.URL, ["/assets/*/metadata.json"])
                by_code = {}
                for meta in (folder / "assets").glob("*/metadata.json"):
                    try:
                        unicode = json.loads(meta.read_text(encoding="utf-8")).get("unicode", "")
                    except ValueError:
                        continue
                    code = unicode.lower().split()
                    by_code.setdefault("-".join(p for p in code if p != "fe0f"), meta.parent.name)
                self.by_code = by_code
            return self.by_code

    def folder_for(self, hexcodes):
        by_code = self.load()
        for hexcode in hexcodes:
            bare = "-".join(code_variants(hexcode)[-1])
            if bare in by_code:
                return by_code[bare]
        return None

    def files(self, name):
        """(3D png path, flat svg path) relative to the clone; the files are listed without blobs."""
        folder = SOURCES_DIR / "fluentui-emoji"
        listing = run(["git", "ls-tree", "-r", "--name-only", "HEAD", f"assets/{name}"], cwd=folder).splitlines()
        png = next((f for f in listing if "/3D/" in f and f.endswith(".png") and "/Default/" in f), None) or \
            next((f for f in listing if "/3D/" in f and f.endswith(".png")), None)
        svg = next((f for f in listing if "/Flat/" in f and f.endswith(".svg") and "/Default/" in f), None) or \
            next((f for f in listing if "/Flat/" in f and f.endswith(".svg")), None)
        return png, svg

    def prepare(self, hexcodes_lists):
        wanted = []
        for hexcodes in hexcodes_lists:
            name = self.folder_for(hexcodes)
            if name:
                wanted += [f for f in self.files(name) if f]
        sparse_add(SOURCES_DIR / "fluentui-emoji", wanted)

    def candidates(self, hexcodes):
        name = self.folder_for(hexcodes)
        if not name:
            return []
        png, svg = self.files(name)
        folder = SOURCES_DIR / "fluentui-emoji"
        sparse_add(folder, [f for f in (png, svg) if f])
        attribution = {"author": "Microsoft", "license": "MIT",
                       "licenseURL": "https://github.com/microsoft/fluentui-emoji/blob/main/LICENSE",
                       "page": f"{self.URL}/tree/main/assets/{urllib.parse.quote(name)}"}
        out = []
        if png and (folder / png).exists():
            out.append({"source": "fluent-3d", "local": folder / png, "commercial": True, "attribution": attribution,
                        "remote": f"{self.URL}/blob/main/{urllib.parse.quote(png)}"})
        if svg and (folder / svg).exists():
            out.append({"source": "fluent-flat", "local": rasterise(folder / svg), "commercial": True,
                        "attribution": attribution, "remote": f"{self.URL}/blob/main/{urllib.parse.quote(svg)}"})
        return out


class CodepointRepo:
    """An emoji set that is one file per codepoint sequence: Noto (PNG) and Twemoji (SVG)."""

    def __init__(self, source, name, url, directory, pattern, svg, attribution, page):
        self.source, self.name, self.url, self.directory = source, name, url, directory
        self.pattern, self.svg, self.attribution, self.page = pattern, svg, attribution, page

    def candidates(self, hexcodes):
        folder = ensure_repo(self.name, self.url, [f"/{self.directory}/"])
        for hexcode in hexcodes:
            for codes in code_variants(hexcode):
                file = folder / self.directory / self.pattern.format("_".join(codes) if "noto" in self.name else "-".join(codes))
                if file.exists():
                    rel = file.relative_to(folder)
                    return [{"source": self.source, "local": rasterise(file) if self.svg else file, "commercial": True,
                             "remote": f"{self.page}/blob/main/{urllib.parse.quote(str(rel))}",
                             "attribution": self.attribution}]
        return []


NOTO = CodepointRepo(
    "noto", "noto-emoji", "https://github.com/googlefonts/noto-emoji", "2D/png/512", "emoji_u{}.png", False,
    {"author": "Google", "license": "Apache 2.0", "licenseURL": "https://www.apache.org/licenses/LICENSE-2.0",
     "page": "https://github.com/googlefonts/noto-emoji"}, "https://github.com/googlefonts/noto-emoji")
TWEMOJI = CodepointRepo(
    "twemoji", "twemoji", "https://github.com/jdecked/twemoji", "assets/svg", "{}.svg", True,
    {"author": "Twitter, Inc and other contributors (jdecked/twemoji)", "license": "CC BY 4.0",
     "licenseURL": "https://creativecommons.org/licenses/by/4.0/", "page": "https://github.com/jdecked/twemoji"},
    "https://github.com/jdecked/twemoji")
MULBERRY, FLUENT = Mulberry(), Fluent()


class Ctx:
    """What a source function gets: the query, the shared helpers and what is already shown.
    `broad` is a re-search: more results from each source, skipping what the concept already has."""

    def __init__(self, term, emoji, openverse, broad=False, seen=(), sensitive=False):
        self.term, self.emoji, self.openverse, self.broad = term, emoji, openverse, broad
        self.sensitive = sensitive or is_sensitive(term)
        self.seen = set(seen)


# Each source is one function: Ctx -> list of candidate dicts {source, remote, fetch, commercial,
# attribution[, marker]}. `marker` (a Commons file title) lets one file found by two sources count
# once. To add a source: write the function, put it in SOURCES in chain order; Commons search
# stays last, its results are alternates and never the default pick.

def source_openmoji(ctx):
    return ctx.emoji.candidates(ctx.term)


def source_mulberry(ctx):
    return MULBERRY.candidates(ctx.term)


def source_fluent(ctx):
    return FLUENT.candidates(ctx.emoji.hexcodes(ctx.term))


def source_noto(ctx):
    return NOTO.candidates(ctx.emoji.hexcodes(ctx.term))


def source_twemoji(ctx):
    return TWEMOJI.candidates(ctx.emoji.hexcodes(ctx.term))


def commons_file_source(name, finder):
    def source(ctx):
        titles = finder(ctx.term)
        infos = commons_infos(titles) if titles else {}
        return [{"source": name, "marker": t, **infos[t]} for t in titles if t in infos]
    return source


def source_openverse(ctx):
    return openverse_candidates(ctx.openverse, ctx.term, 5 if ctx.broad else 1)


def source_commons_search(ctx):
    found = commons_search(ctx.term)
    return [{"source": "commons", "marker": t, **c} for t, c in found][:8 if ctx.broad else COMMONS_EXTRA + 4]


SOURCES = [
    ("openmoji", source_openmoji),
    ("mulberry", source_mulberry),
    ("fluent", source_fluent),
    ("noto", source_noto),
    ("twemoji", source_twemoji),
    ("wikidata", commons_file_source("wikidata", wikidata_files)),
    ("wikipedia", commons_file_source("wikipedia", wikipedia_file)),
    ("openverse", source_openverse),
    ("commons", source_commons_search),
]


def gather(ctx, cap):
    """Candidates not yet in ctx.seen, up to cap; non-commercial ones are dropped and counted."""
    found, errors = [], []
    for name, source in SOURCES:
        if ctx.sensitive and name in NETWORK_SOURCES:
            continue  # photos of a body part or a wound: drawings only (sensitive.json)
        recorded = f"{name}|{ctx.term}|{'broad' if ctx.broad else 'first'}"
        try:
            if name in NETWORK_SOURCES and recorded in catalog:
                candidates = copy.deepcopy(catalog[recorded]["candidates"])
                with stats_lock:
                    stats["catalog"] += 1
            else:
                candidates = source(ctx)
                if name in NETWORK_SOURCES:
                    with catalog_lock:
                        catalog[recorded] = {"at": time.strftime("%Y-%m-%d"), "candidates": copy.deepcopy(candidates)}
        except SourceSkipped:
            continue
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            continue
        taken = 0
        flagged = commons_flags([c.get("marker") or "" for c in candidates]) if name in NETWORK_SOURCES else {}
        for candidate in candidates:
            marker = candidate.pop("marker", None) or candidate["remote"]
            if marker in ctx.seen or candidate["remote"] in ctx.seen:
                continue
            if name == "commons" and taken >= (8 if ctx.broad else COMMONS_EXTRA):
                break
            if len(found) >= cap:
                break
            ctx.seen.update({marker, candidate["remote"]})
            if marker in flagged:
                with stats_lock:
                    stats["sensitive"] += 1
                continue
            if not candidate.get("commercial"):
                with stats_lock:
                    stats["dropped"] += 1
                continue
            found.append(candidate)
            taken += 1
    return found, errors


def download(key, candidates, start, errors):
    result = []
    for candidate in candidates:
        number = start + len(result)
        target = CANDIDATES_DIR / key / f"{number}.webp"
        try:
            if "local" in candidate:
                data = Path(candidate["local"]).read_bytes()
            else:
                data = http(candidate["fetch"], limiter=OTHER if candidate["source"] == "openmoji" else None)
            size = to_webp(data, target)
        except Exception as exc:
            errors.append(f"download {candidate.get('fetch') or candidate.get('local')}: {exc}")
            continue
        if not size:
            errors.append(f"convert {candidate.get('fetch') or candidate.get('local')}: failed")
            continue
        result.append({
            "source": candidate["source"], "remote": candidate["remote"], "commercial": True,
            "file": f"candidates/{key}/{number}.webp", "width": size[0], "height": size[1],
            "bytes": target.stat().st_size, "attribution": candidate["attribution"],
        })
    return result


def process(key, concept, emoji, openverse, previous=None, query=None):
    """First fetch of a concept, or (with `previous`, its entry) a re-search that appends."""
    term = search_term(query) if query else search_term(concept["en"])
    errors = []
    if previous is None:
        folder = CANDIDATES_DIR / key
        if folder.exists():
            shutil.rmtree(folder)
        found, errors = gather(Ctx(term, emoji, openverse, sensitive=is_sensitive(concept["en"])), MAX_CANDIDATES)
        candidates = download(key, found, 0, errors)
        entry = {k: concept[k] for k in ("en", "pos", "ru", "sets")}
    else:
        shown = {c["remote"] for c in previous["candidates"]}
        found, errors = gather(Ctx(term, emoji, openverse, broad=True, seen=shown, sensitive=is_sensitive(concept["en"])), 8)
        # file numbers continue after the highest, so earlier picks keep their index
        start = len(previous["candidates"])
        candidates = previous["candidates"] + download(key, found, start, errors)
        entry = {**previous}
    entry["candidates"] = candidates
    return key, entry, errors


def cmd_fetch(args):
    started = time.time()
    concepts = collect_concepts(ROOT)
    wanted = args.sets
    research = {}
    if args.research:
        raw = json.loads(Path(args.research).read_text(encoding="utf-8"))
        research = {k: (v.get("research") or "") for k, v in raw.items() if isinstance(v, dict) and "research" in v}
        concepts = {k: v for k, v in concepts.items() if k in research}
        wanted = None
        print(f"re-searching {len(research)} concepts")
    if wanted:
        unknown = [s for s in wanted if not (ROOT / "sets" / s / "words.json").exists()]
        if unknown:
            sys.exit(f"no such sets: {unknown}")
        concepts = {k: v for k, v in concepts.items() if set(v["sets"]) & set(wanted)}
    emoji, openverse = Emoji(), Openverse()
    CACHE.mkdir(parents=True, exist_ok=True)
    load_catalog()
    try:
        library = json.loads(CANDIDATES_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        library = {}
    if args.research:
        concepts = {k: v for k, v in concepts.items() if k in library}
        # marked in the review page: "seen" = candidates it had then; more now = already searched again
        seen = {k: v.get("seen") for k, v in raw.items() if isinstance(v, dict)}
        done = [k for k in concepts if seen.get(k) is not None and len(library[k]["candidates"]) > seen[k]]
        concepts = {k: v for k, v in concepts.items() if k not in done}
        if done:
            print(f"{len(done)} of them were already searched again, skipped")
    try:  # one git call for every Fluent file we will need
        FLUENT.prepare([emoji.hexcodes(search_term(research.get(k) or c["en"])) for k, c in concepts.items()])
    except Exception as exc:
        print(f"  fluent prefetch failed ({exc}); going on one by one", flush=True)
    total, done, lock = len(concepts), 0, threading.Lock()
    print(f"{total} concepts", flush=True)

    def run(item):
        nonlocal done
        key, concept = item
        try:
            key, entry, errors = process(key, concept, emoji, openverse,
                                         previous=library.get(key) if args.research else None,
                                         query=research.get(key) or None)
        except Exception as exc:
            with lock:
                done += 1
                stats["errors"].append(f"{key}: {exc}")
                print(f"[{done}/{total}] {key}: ERROR {exc}", flush=True)
            return
        with lock:
            done += 1
            library[key] = entry
            stats["errors"].extend(f"{key}: {e}" for e in errors)
            kinds = ",".join(c["source"][:3] for c in entry["candidates"]) or "none"
            print(f"[{done}/{total}] {key}: {len(entry['candidates'])} ({kinds})", flush=True)
            if done % 10 == 0:
                CANDIDATES_JSON.write_text(json.dumps(library, ensure_ascii=False, indent=1), encoding="utf-8")
                save_catalog()

    with ThreadPoolExecutor(CONCURRENCY) as pool:
        list(pool.map(run, concepts.items()))
    CANDIDATES_JSON.write_text(json.dumps(library, ensure_ascii=False, indent=1), encoding="utf-8")
    save_catalog()

    mine = [library[k] for k in concepts if k in library]
    files = [c for e in mine for c in e["candidates"]]
    empty = sum(1 for e in mine if not e["candidates"])
    megabytes = sum(c["bytes"] for c in files) / 1e6
    print(f"\n{len(mine)} concepts, {len(files)} candidates, {empty} with none, {megabytes:.1f} MB, "
          f"{(sum(c['bytes'] for c in files) / len(files) / 1000 if files else 0):.1f} KB per webp, "
          f"{time.time() - started:.0f} s")
    per_source = {}
    for e in mine:
        for name in {c["source"] for c in e["candidates"]}:
            per_source[name] = per_source.get(name, 0) + 1
    print("concepts hit per source:", ", ".join(f"{n} {v}" for n, v in sorted(per_source.items())))
    print(f"requests: {stats['network']} network, {stats['cached']} cached, {stats['catalog']} source answers from catalog.json; "
          f"openverse skipped for {openverse.skipped} concepts; {stats['dropped']} non-commercial dropped; {stats['sensitive']} sensitive files dropped; {len(stats['errors'])} errors")
    for line in stats["errors"][:40]:
        print("  !", line)


# MARK: - screen


def first_visible(entry):
    return next((i for i, c in enumerate(entry["candidates"]) if not c.get("hidden")), None)


def cmd_screen(_args):
    """Hide (keeping every index) the candidates sensitive.json now rules out: photos of a sensitive
    word, Commons files whose title or category is blocked. Picks of a hidden one are taken out of
    choices.json, so the review asks for those words again."""
    library = json.loads(CANDIDATES_JSON.read_text(encoding="utf-8"))
    load_catalog()
    titles = [t for e in library.values() for c in e["candidates"] if (t := commons_title(c))]
    print(f"checking the categories of {len(set(titles))} Commons files", flush=True)
    flagged = commons_flags(titles)
    save_catalog()
    by_hand = hidden_by_hand()
    nsfw = json.loads(NSFW_JSON.read_text(encoding="utf-8")) if NSFW_JSON.exists() else {}
    print(f"{sum(len(v) for v in by_hand.values())} hidden by hand, {len(nsfw)} nudity scores", flush=True)
    hidden = words = 0
    for key, entry in library.items():
        sensitive = is_sensitive(entry["en"])
        touched = False
        for i, c in enumerate(entry["candidates"]):
            reason = None
            if str(i) in by_hand.get(key, {}):
                reason = f"by hand: {by_hand[key][str(i)]}"
            elif nsfw.get(c["remote"], 0) >= NSFW_THRESHOLD and c["source"] in NETWORK_SOURCES:
                reason = f"nsfw {nsfw[c['remote']]:.2f}"
            elif sensitive and c["source"] in NETWORK_SOURCES:
                reason = "sensitive word: drawings only"
            elif (t := commons_title(c)) in flagged:
                reason = f"blocked: {flagged[t]}"
            if reason and c.get("hidden") != reason:
                hidden += 1
                touched = True
            if reason:
                c["hidden"] = reason
            else:
                c.pop("hidden", None)
        words += touched
    CANDIDATES_JSON.write_text(json.dumps(library, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{hidden} candidates newly hidden in {words} words; "
          f"{sum(1 for e in library.values() for c in e['candidates'] if c.get('hidden'))} hidden in all")
    if REVIEW_JSON.exists():
        choices = json.loads(REVIEW_JSON.read_text(encoding="utf-8"))
        undone = []
        for key, value in list(choices.items()):
            pick = value.get("pick") if isinstance(value, dict) else value
            if isinstance(pick, int) and key in library and pick < len(library[key]["candidates"]) \
                    and library[key]["candidates"][pick].get("hidden"):
                undone.append(key)
                del choices[key]
        if undone:
            REVIEW_JSON.write_text(json.dumps(choices, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            print(f"picks of a now hidden picture taken back, to review again: {', '.join(undone)}")


# MARK: - gallery

GALLERY = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Picture library review</title>
<style>
:root{--bg:#fff;--fg:#1d1d1f;--mut:#6e6e73;--line:#d2d2d7;--acc:#0a66ff;--ok:#e6f4ea;--no:#fdecea}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--fg:#f5f5f7;--mut:#98989d;--line:#3a3a3c;--acc:#4c8dff;--ok:#1e3a28;--no:#4a2422}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.35 -apple-system,system-ui,sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px;z-index:5}
header .row{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center}
select,button,input{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:4px 8px}
button{cursor:pointer}
#counts{color:var(--mut)}
.concept{display:flex;gap:16px;padding:10px 16px;border-bottom:1px solid var(--line);outline:none}
.concept:focus{background:color-mix(in srgb,var(--acc) 10%,transparent)}
.info{width:200px;flex:none}
.info b{font-size:16px}
.info .mut{color:var(--mut);font-size:12px}
.cands{display:flex;flex-wrap:wrap;gap:10px}
.cand{width:128px;border:2px solid transparent;border-radius:8px;padding:4px;cursor:pointer;font-size:11px;color:var(--mut)}
.cand img{width:120px;height:120px;object-fit:contain;background:repeating-conic-gradient(#8883 0 25%,transparent 0 50%) 0 0/16px 16px;border-radius:4px;display:block}
.cand.sel{border-color:var(--acc);background:var(--ok)}
.cand .n{color:var(--fg);font-weight:600}
.cand a{color:var(--acc)}
.none{align-self:flex-start}
.none.sel{background:var(--no);border-color:#d33}
.badge{display:inline-block;padding:0 5px;border-radius:8px;background:var(--ok);color:var(--fg);font-weight:600}
.research{align-self:flex-start;display:flex;flex-direction:column;gap:4px;font-size:12px}
.research input[type=text]{width:150px}
.cand span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.done-mark{color:#2a8a3e;font-weight:600}
#review{max-width:1200px;margin:0 auto;padding:16px}
.rv-top{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
.rv-top progress{flex:1;min-width:160px;height:10px}
.rv-word{font-size:40px;font-weight:700;line-height:1.1}
.rv-ru{font-size:22px;margin-top:4px}
.rv-meta{color:var(--mut);margin:4px 0 14px}
.rv-note{display:inline-block;background:var(--ok);border-radius:6px;padding:2px 8px;margin-bottom:10px}
.rv-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}
.rv-c{border:2px solid var(--line);border-radius:10px;padding:6px;cursor:pointer;font-size:12px;color:var(--mut);position:relative}
.rv-c:hover{border-color:var(--acc)}
.rv-c.def{border-color:var(--acc)}
.rv-c img{width:100%;aspect-ratio:1;object-fit:contain;display:block;border-radius:6px;background:repeating-conic-gradient(#8883 0 25%,transparent 0 50%) 0 0/16px 16px}
.rv-c .num{position:absolute;top:10px;left:10px;background:var(--fg);color:var(--bg);border-radius:6px;padding:0 7px;font-weight:700;font-size:14px}
.rv-c .new{position:absolute;top:10px;right:10px;background:var(--acc);color:#fff;border-radius:6px;padding:0 6px;font-weight:700}
.rv-c span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rv-c a{color:var(--acc)}
.rv-actions{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:16px 0}
.rv-actions button{padding:8px 14px}
.rv-actions input{width:220px;padding:8px}
.rv-empty{font-size:20px;padding:40px 0;text-align:center}
.mut{color:var(--mut)}
</style></head><body>
<header><div class="row">
<b>Picture library</b><button id="rv-start"></button><span id="counts"></span><span id="saved" style="font-size:12px"></span>
<label>Set <select id="fset"></select></label>
<label>Status <select id="fstatus"><option value="">all</option><option value="todo">not reviewed</option><option value="done">reviewed</option><option value="waiting">waiting for "search again"</option><option value="picked">picked</option><option value="none">no picture</option><option value="empty">no candidates</option><option value="default">default pick (untouched)</option></select></label>
<label>Part of speech <select id="fpos"></select></label>
<button id="export">Export choices</button>
<label>Import choices <input type="file" id="import" accept=".json"></label>
</div>
<div class="mut" style="color:var(--mut);font-size:12px;margin-top:4px">Keys: j/k or arrows = next/previous concept, 1-6 = pick candidate, 0 or x = no picture, d = back to default pick.</div>
</header>
<main id="list"></main>
<div id="review" hidden>
<div class="rv-top"><b>Review</b><progress id="rv-prog"></progress><span id="rv-count"></span><button id="rv-exit">Back to the list (Esc)</button></div>
<div id="rv-card"></div>
<div class="mut" style="font-size:12px">Keys: 1-9 = pick that picture, Enter = pick the first, "hide" under a picture = indecent or gory, never offered again, 0 or x = no picture, r = search again, s = skip for now, u = back to the previous word, Esc = list. The set and part-of-speech filters above apply here too.</div>
</div>
<script>
const DATA = __DATA__;
const KEY = "wi-library-choices";
let choices = {};
let research = {};
const RKEY = "wi-library-research";
try { research = JSON.parse(localStorage.getItem(RKEY) || "{}") } catch (e) {}
const saveR = () => { try { localStorage.setItem(RKEY, JSON.stringify(research)) } catch (e) {} push(); };
const researchOf = k => research[k];
// how many candidates a concept had when it was sent to "search again": more than that = it came back
const SKEY = "wi-library-research-seen";
let rseen = {};
try { rseen = JSON.parse(localStorage.getItem(SKEY) || "{}") } catch (e) {}
const saveS = () => { try { localStorage.setItem(SKEY, JSON.stringify(rseen)) } catch (e) {} push(); };
const cameBack = k => researchOf(k) !== undefined && rseen[k] !== undefined && DATA[k].total > rseen[k];
const waiting = k => researchOf(k) !== undefined && !cameBack(k);
// reviewed = the user picked, chose "no picture", or sent it to search again (until new candidates arrive)
const reviewed = k => researchOf(k) !== undefined ? !cameBack(k) : k in choices;
try { choices = JSON.parse(localStorage.getItem(KEY) || "{}") } catch (e) {}
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(choices)) } catch (e) {} push(); };
// Served by `library.py review`: every change is also written to tools/images/choices.json in the
// repo, so a review spread over days survives the browser; opened as a file, only localStorage keeps it.
const SERVER = location.protocol.startsWith("http");
let pushTimer = null, loaded = !SERVER;
function stateOut() {
  const out = {};
  new Set([...Object.keys(choices), ...Object.keys(research)]).forEach(k => {
    if (!(k in DATA)) return;
    out[k] = research[k] !== undefined ? {research: research[k], pick: k in choices ? choices[k] : pick(k), seen: rseen[k]} : choices[k];
  });
  return out;
}
function stateIn(o) {
  for (const k in o) if (k in DATA) {
    const v = o[k];
    if (v !== null && typeof v === "object") { research[k] = v.research || ""; rseen[k] = v.seen ?? rseen[k] ?? DATA[k].total; if ("pick" in v) choices[k] = v.pick; }
    else choices[k] = v;
  }
}
function saved(text, bad) { const el = $("saved"); if (el) { el.textContent = text; el.style.color = bad ? "#d33" : "var(--mut)"; } }
function push() {
  if (!SERVER || !loaded) return;
  clearTimeout(pushTimer); saved("saving…");
  pushTimer = setTimeout(async () => {
    try { const r = await fetch("state", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(stateOut())}); if (!r.ok) throw new Error(r.status); saved("saved to choices.json ✓"); }
    catch (err) { saved("NOT saved to the file (is library.py review still running?) — kept in the browser", true); }
  }, 300);
}
const keys = Object.keys(DATA);
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const sets = [...new Set(keys.flatMap(k => DATA[k].sets))].sort();
const poses = [...new Set(keys.map(k => DATA[k].pos || "(none)"))].sort();
$("fset").innerHTML = '<option value="">all</option>' + sets.map(s => `<option>${esc(s)}</option>`).join("");
$("fpos").innerHTML = '<option value="">all</option>' + poses.map(s => `<option>${esc(s)}</option>`).join("");
function pick(k) {
  if (k in choices) return choices[k];
  return DATA[k].candidates.length ? DATA[k].candidates[0].i : null;
}
function status(k) {
  if (!DATA[k].candidates.length) return "empty";
  if (!(k in choices)) return "default";
  return choices[k] === null ? "none" : "picked";
}
function visible(k) {
  const d = DATA[k], f = $("fstatus").value, s = $("fset").value, p = $("fpos").value;
  if (s && !d.sets.includes(s)) return false;
  if (p && (d.pos || "(none)") !== p) return false;
  if (f === "todo") return !reviewed(k);
  if (f === "done") return reviewed(k);
  if (f === "waiting") return waiting(k);
  if (f === "picked") return pick(k) !== null && DATA[k].candidates.length > 0;
  if (f === "none") return pick(k) === null && DATA[k].candidates.length > 0;
  if (f === "empty") return !d.candidates.length;
  if (f === "default") return status(k) === "default";
  return true;
}
function row(k) {
  const d = DATA[k], sel = pick(k);
  const cands = d.candidates.map((c, i) => `<div class="cand ${sel === c.i ? "sel" : ""}" data-k="${esc(k)}" data-i="${c.i}" title="${esc(c.remote)}">
    <img loading="lazy" src="${esc(c.file)}"><span class="n">${i + 1}. ${esc(c.source)}</span>
    <span><span class="badge" style="display:inline-block">${c.commercial === false ? "NC?" : "commercial OK"}</span> ${esc(c.attribution.license)}</span><span>${esc(c.attribution.author)}</span>
    <span>${c.width}x${c.height}, ${(c.bytes / 1000).toFixed(0)} KB</span>
    <span><a href="${esc(c.attribution.page)}" target="_blank" rel="noopener">page</a> · <a href="#" class="hide" data-k="${esc(k)}" data-i="${c.i}">hide</a></span></div>`).join("");
  const rs = researchOf(k);
  const research = `<div class="research"><label><input type="checkbox" class="rs-on" data-k="${esc(k)}" ${rs !== undefined ? "checked" : ""}> Search again</label>
    <input type="text" class="rs-q" data-k="${esc(k)}" placeholder="better English query" value="${esc(rs || "")}" ${rs === undefined ? "disabled" : ""}></div>`;
  const none = `<div class="cand none ${sel === null ? "sel" : ""}" data-k="${esc(k)}" data-i="none"><button>no picture</button></div>`;
  return `<section class="concept" tabindex="0" id="r-${esc(k)}" data-k="${esc(k)}">
    <div class="info"><b>${esc(d.en)}</b> ${reviewed(k) ? '<span class="done-mark">✓</span>' : ""}<br>${esc(d.ru)}<br><span class="mut">${esc(d.pos || "-")} · ${esc(d.sets.join(", "))}<br>${esc(k)}</span></div>
    <div class="cands">${cands}${none}${research}${d.candidates.length ? "" : '<span class="mut">no candidates</span>'}</div></section>`;
}
function render() {
  const shown = keys.filter(visible);
  $("list").innerHTML = shown.map(row).join("");
  const c = {picked: 0, none: 0, empty: 0};
  keys.forEach(k => { if (!DATA[k].candidates.length) c.empty++; else if (pick(k) === null) c.none++; else c.picked++; });
  const left = keys.filter(k => !reviewed(k)).length;
  $("rv-start").textContent = `Review mode (${left} left)`;
  $("counts").textContent = `${keys.length} concepts · ${keys.length - left} reviewed · ${c.picked} with a picture · ${c.none} no picture · ${c.empty} without candidates · showing ${shown.length}`;
}
function set(k, v) {
  if (v === undefined) delete choices[k]; else choices[k] = v;
  save();
  const el = $("r-" + CSS.escape(k));
  if (el) { const tmp = document.createElement("div"); tmp.innerHTML = row(k); el.replaceWith(tmp.firstElementChild); $("r-" + CSS.escape(k)).focus(); }
}
$("list").addEventListener("change", e => {
  const t = e.target, k = t.dataset.k; if (!k) return;
  if (t.classList.contains("rs-on")) { if (t.checked) { research[k] = ""; rseen[k] = DATA[k].total; } else { delete research[k]; delete rseen[k]; } saveS(); }
  else if (t.classList.contains("rs-q")) research[k] = t.value.trim();
  saveR(); set(k, choices[k]);
});
$("list").addEventListener("click", e => {
  const h = e.target.closest("a.hide"); if (h) { e.preventDefault(); return hideCandidate(h.dataset.k, Number(h.dataset.i)); }
  const a = e.target.closest("a"); if (a) return;
  const c = e.target.closest(".cand"); if (!c) return;
  set(c.dataset.k, c.dataset.i === "none" ? null : Number(c.dataset.i));
});
document.addEventListener("keydown", e => {
  if (!$("review").hidden) return reviewKey(e);
  if (e.target.matches("select,input,textarea")) return;
  const cur = document.activeElement.closest(".concept");
  const move = d => { const all = [...document.querySelectorAll(".concept")]; const i = cur ? all.indexOf(cur) : -1; const n = all[Math.max(0, Math.min(all.length - 1, i + d))]; if (n) { n.focus(); n.scrollIntoView({block: "center"}); } e.preventDefault(); };
  if (e.key === "j" || e.key === "ArrowDown") return move(1);
  if (e.key === "k" || e.key === "ArrowUp") return move(-1);
  if (!cur) return;
  const k = cur.dataset.k;
  if (e.key === "0" || e.key === "x") set(k, null);
  else if (e.key === "d") set(k, undefined);
  else if (/^[1-6]$/.test(e.key) && DATA[k].candidates[Number(e.key) - 1]) set(k, DATA[k].candidates[Number(e.key) - 1].i);
});
["fset", "fstatus", "fpos"].forEach(id => $(id).addEventListener("change", () => $("review").hidden ? render() : reviewShow()));

// MARK: review mode — one word at a time, only the ones not reviewed yet
let current = null;
const skipped = new Set(), history = [];
const inFilter = k => { const d = DATA[k], s = $("fset").value, p = $("fpos").value; return (!s || d.sets.includes(s)) && (!p || (d.pos || "(none)") === p); };
const queue = () => keys.filter(k => inFilter(k) && !reviewed(k));
function reviewNext() { current = queue().find(k => !skipped.has(k)) ?? null; reviewShow(); }
function reviewStart() { $("list").hidden = true; $("review").hidden = false; try { localStorage.setItem("wi-library-mode", "review") } catch (e) {} skipped.clear(); reviewNext(); }
function reviewExit() { $("review").hidden = true; $("list").hidden = false; try { localStorage.setItem("wi-library-mode", "list") } catch (e) {} render(); }
function reviewShow() {
  if (current !== null && (!inFilter(current) || (reviewed(current) && history.at(-1)?.k !== current))) current = null;
  if (current === null) current = queue().find(k => !skipped.has(k)) ?? null;
  const all = keys.filter(inFilter), left = queue().length;
  $("rv-prog").max = all.length || 1; $("rv-prog").value = all.length - left;
  $("rv-count").textContent = `${all.length - left} of ${all.length} reviewed · ${left} left` + (skipped.size ? ` · ${skipped.size} skipped` : "");
  if (current === null) {
    $("rv-card").innerHTML = `<div class="rv-empty">${left ? "Only skipped words are left." : "Everything in this filter is reviewed."}<br><br>
      ${left ? '<button id="rv-unskip">Show the skipped ones</button> ' : ""}<button id="rv-export">Export choices</button></div>`;
    $("rv-unskip")?.addEventListener("click", () => { skipped.clear(); reviewNext(); });
    $("rv-export").addEventListener("click", () => $("export").click());
    return;
  }
  const k = current, d = DATA[k], back = cameBack(k), from = rseen[k] ?? 0;
  const cards = d.candidates.map((c, i) => `<div class="rv-c ${i === 0 ? "def" : ""}" data-i="${c.i}" title="${esc(c.remote)}">
    <img src="${esc(c.file)}"><span class="num">${i + 1}</span>${back && c.i >= from ? '<span class="new">new</span>' : ""}
    <span><b style="color:var(--fg)">${esc(c.source)}</b> · ${esc(c.attribution.license)}</span>
    <span>${esc(c.attribution.author)}</span>
    <span>${c.width}x${c.height} · <a href="${esc(c.attribution.page)}" target="_blank" rel="noopener">page</a> · <a href="#" class="hide" data-i="${c.i}">hide</a></span></div>`).join("");
  $("rv-card").innerHTML = `<div class="rv-word">${esc(d.en)}</div><div class="rv-ru">${esc(d.ru)}</div>
    <div class="rv-meta">${esc(d.pos || "-")} · ${esc(d.sets.join(", "))} · ${esc(k)}</div>
    ${back ? `<div class="rv-note">Searched again${researchOf(k) ? ` for “${esc(researchOf(k))}”` : ""}: the new pictures are marked</div>` : ""}
    <div class="rv-grid">${cards || '<div class="mut">No candidates for this word.</div>'}</div>
    <div class="rv-actions"><button id="rv-none">No picture (0)</button>
      <input id="rv-q" type="text" placeholder="better English query (optional)" value="${esc(researchOf(k) || "")}"><button id="rv-rs">Search again (r)</button>
      <button id="rv-skip">Skip for now (s)</button><button id="rv-back" ${history.length ? "" : "disabled"}>Previous word (u)</button></div>`;
  $("rv-card").querySelectorAll(".rv-c").forEach(el => el.addEventListener("click", e => {
    const h = e.target.closest("a.hide"); if (h) { e.preventDefault(); return hideCandidate(k, Number(h.dataset.i)); }
    if (!e.target.closest("a")) reviewDecide("pick", Number(el.dataset.i)); }));
  $("rv-none").onclick = () => reviewDecide("none");
  $("rv-rs").onclick = () => reviewDecide("research");
  $("rv-q").onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); reviewDecide("research"); } if (e.key === "Escape") e.target.blur(); };
  $("rv-skip").onclick = () => { skipped.add(k); reviewNext(); };
  $("rv-back").onclick = reviewBack;
  // warm the next word's pictures
  const n = queue().find(x => x !== k && !skipped.has(x)); if (n) DATA[n].candidates.forEach(c => { new Image().src = c.file; });
}
// "Hide": an indecent or gory picture, out of this word's choices for good (tools/images/hidden.json)
async function hideCandidate(k, i) {
  if (!SERVER) { saved("hiding needs the served page (library.py review)", true); return; }
  try { const r = await fetch("hide", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({key: k, i, reason: "indecent"})}); if (!r.ok) throw new Error(r.status); }
  catch (err) { saved("could not hide: is library.py review still running?", true); return; }
  DATA[k].candidates = DATA[k].candidates.filter(c => c.i !== i);
  if (choices[k] === i) { delete choices[k]; save(); }
  if (!$("review").hidden) reviewShow(); else set(k, choices[k]);
}
function reviewDecide(what, i) {
  const k = current; if (k === null) return;
  history.push({k, had: k in choices, choice: choices[k], research: researchOf(k), seen: rseen[k]});
  if (what === "research") {
    research[k] = $("rv-q").value.trim(); rseen[k] = DATA[k].total;
    if (!(k in choices)) choices[k] = pick(k);
  } else {
    choices[k] = what === "none" ? null : i;
    delete research[k]; delete rseen[k];
  }
  save(); saveR(); saveS(); reviewNext();
}
function reviewBack() {
  const h = history.pop(); if (!h) return;
  if (h.had) choices[h.k] = h.choice; else delete choices[h.k];
  if (h.research === undefined) delete research[h.k]; else research[h.k] = h.research;
  if (h.seen === undefined) delete rseen[h.k]; else rseen[h.k] = h.seen;
  save(); saveR(); saveS(); skipped.delete(h.k); current = h.k; reviewShow();
}
function reviewKey(e) {
  if (e.target.matches("input,select,textarea")) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const k = current;
  if (e.key === "Escape") { e.preventDefault(); return reviewExit(); }
  if (e.key === "u" || e.key === "Backspace") { e.preventDefault(); return reviewBack(); }
  if (k === null) return;
  if (e.key === "s") { skipped.add(k); return reviewNext(); }
  if (e.key === "0" || e.key === "x") return reviewDecide("none");
  if (e.key === "r") { e.preventDefault(); $("rv-q").focus(); return; }
  if (e.key === "Enter" && DATA[k].candidates.length) return reviewDecide("pick", DATA[k].candidates[0].i);
  if (/^[1-9]$/.test(e.key) && DATA[k].candidates[Number(e.key) - 1]) return reviewDecide("pick", DATA[k].candidates[Number(e.key) - 1].i);
}
$("rv-start").addEventListener("click", reviewStart);
$("rv-exit").addEventListener("click", reviewExit);
$("export").addEventListener("click", () => {
  // only the reviewed words: an untouched one gets its default pick from apply, and stays unreviewed on import
  const out = stateOut();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 1)], {type: "application/json"}));
  a.download = "choices.json"; a.click();
});
$("import").addEventListener("change", async e => {
  const f = e.target.files[0]; if (!f) return;
  try { stateIn(JSON.parse(await f.text())); save(); saveR(); saveS(); $("review").hidden ? render() : reviewShow(); } catch (err) { alert("Bad file: " + err); }
});
(async () => {
  if (SERVER) {
    try {
      const o = await (await fetch("state", {cache: "no-store"})).json();
      if (Object.keys(o).length) { choices = {}; research = {}; rseen = {}; stateIn(o); }  // the file wins over the browser
      loaded = true; save(); saveR(); saveS();
    } catch (err) { saved("could not read choices.json — working from the browser only", true); }
  } else saved("opened as a file: progress is kept in this browser only — run library.py review to keep it in the repo", true);
  render();
  try { if (localStorage.getItem("wi-library-mode") === "review") reviewStart(); } catch (e) {}
})();
</script></body></html>
"""


def cmd_gallery(_args):
    library = json.loads(CANDIDATES_JSON.read_text(encoding="utf-8"))
    # hidden candidates (cmd_screen) are left out; `i` keeps each one's index, which choices use
    by_hand = hidden_by_hand()
    for key, entry in library.items():
        entry["total"] = len(entry["candidates"])
        entry["candidates"] = [{**c, "i": i} for i, c in enumerate(entry["candidates"])
                               if not c.get("hidden") and str(i) not in by_hand.get(key, {})]
    payload = json.dumps(library, ensure_ascii=False).replace("</", "<\\/")
    page = CACHE / "index.html"
    page.write_text(GALLERY.replace("__DATA__", payload), encoding="utf-8")
    print(page)


def cmd_review(args):
    """Serve the gallery on localhost; the page writes every choice to tools/images/choices.json."""
    import http.server
    cmd_gallery(args)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(CACHE), **kw)

        def log_message(self, *a):
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/state":
                body = REVIEW_JSON.read_bytes() if REVIEW_JSON.exists() else b"{}"
                return self.reply(200, body)
            if path in ("/", "/index.html"):
                cmd_gallery(args)  # a reload shows what a running fetch has added since
            return super().do_GET()

        def do_POST(self):
            if self.path == "/hide":
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                    key, index, reason = body["key"], int(body["i"]), str(body.get("reason") or "indecent")
                except (ValueError, KeyError, TypeError):
                    return self.reply(400, b"{}")
                hidden = hidden_by_hand()
                hidden.setdefault(key, {})[str(index)] = reason
                partial = HIDDEN_JSON.with_suffix(".json.partial")
                partial.write_text(json.dumps(hidden, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
                partial.replace(HIDDEN_JSON)
                return self.reply(200, b"{}")
            if self.path != "/state":
                return self.reply(404, b"{}")
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                data = json.loads(raw)
                assert isinstance(data, dict)
            except (ValueError, AssertionError):
                return self.reply(400, b"{}")
            partial = REVIEW_JSON.with_suffix(".json.partial")
            partial.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            partial.replace(REVIEW_JSON)
            self.reply(200, b"{}")

        def reply(self, code, body):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"{url}  (choices go to {REVIEW_JSON.relative_to(ROOT)}; Ctrl-C to stop)", flush=True)
    if not args.no_open:
        subprocess.run(["open", url], check=False)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# MARK: - apply


def normalise_text(text):
    """The key of images/index.json: lowercased, trimmed, inner whitespace collapsed, NFC."""
    return unicodedata.normalize("NFC", " ".join(text.lower().split()))


def write_picture_index(root, concepts, chosen):
    """images/index.json: English text -> the library's pictures for it, for the app to look a
    word up by when it is not in a set. One entry per concept with a picture."""
    pictures = {}
    for key, pick in sorted(chosen.items()):
        if pick.get("none") or key not in concepts:
            continue
        concept = concepts[key]
        entry = {"pos": concept["pos"] or None, "file": pick["path"], "source": pick["source"],
                 "attribution": pick["attribution"]}
        pictures.setdefault(normalise_text(concept["en"]), []).append(entry)
    for entries in pictures.values():
        entries.sort(key=lambda e: e["file"])
    (root / "images").mkdir(exist_ok=True)
    lines = [f"    {json.dumps(text, ensure_ascii=False)}: {json.dumps(entries, ensure_ascii=False, sort_keys=True)}"
             for text, entries in sorted(pictures.items())]
    body = '{\n  "pictures": {\n' + ",\n".join(lines) + ('\n  },\n' if lines else '},\n') + '  "version": 1\n}\n'
    (root / "images" / "index.json").write_text(body, encoding="utf-8")
    return len(pictures)


def cmd_apply(args):
    root = Path(args.root).resolve() if args.root else ROOT
    choices = json.loads(Path(args.choices).read_text(encoding="utf-8"))
    library = json.loads(CANDIDATES_JSON.read_text(encoding="utf-8"))
    concepts = collect_concepts(root)
    by_pair = {(v["en"], v["pos"]): k for k, v in concepts.items()}
    images = root / "images"
    chosen, copied = {}, 0
    for key, entry in library.items():
        default = first_visible(entry)
        pick = choices[key] if key in choices else default
        if isinstance(pick, dict):  # marked for a re-search: its own pick, else the default
            pick = pick.get("pick", default)
        if isinstance(pick, int) and str(pick) in hidden_by_hand().get(key, {}):
            sys.exit(f"{key}: candidate {pick} was hidden by hand; run screen, review it again")
        if isinstance(pick, int) and pick < len(entry["candidates"]) and entry["candidates"][pick].get("hidden"):
            sys.exit(f"{key}: candidate {pick} is hidden ({entry['candidates'][pick]['hidden']}); run screen, review it again")
        if pick is None:
            chosen[key] = {"none": True}
            continue
        if not isinstance(pick, int) or pick >= len(entry["candidates"]):
            sys.exit(f"{key}: no candidate {pick!r}")
        candidate = entry["candidates"][pick]
        images.mkdir(exist_ok=True)
        shutil.copyfile(CACHE / candidate["file"], images / f"{key}.webp")
        copied += 1
        chosen[key] = {"path": f"images/{key}.webp", "source": candidate["remote"],
                       "attribution": candidate["attribution"]}
    changed = 0
    for words_json in sorted((root / "sets").glob("*/words.json")):
        data = json.loads(words_json.read_text(encoding="utf-8"))
        words = data["words"] if isinstance(data, dict) else data
        touched = False
        for word in words:
            en = word["forms"].get("en")
            if not en:
                continue
            pos = en.get("partOfSpeech", "") if isinstance(en, dict) else ""
            key = by_pair.get((form_text(en), pos))
            if key in chosen and word.get("image") != chosen[key]:
                if "image" in word and not isinstance(word["image"], dict):
                    continue  # a hand-made picture of the set's own stays
                word["image"] = chosen[key]
                touched = True
                changed += 1
        if touched:
            words_json.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    index = write_picture_index(root, concepts, chosen)
    print(f"{copied} pictures copied to {images}, {changed} words updated, {index} texts in images/index.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch", help="collect and download candidates")
    fetch.add_argument("sets", nargs="*")
    fetch.add_argument("--research", metavar="CHOICES", help="choices.json: re-search the concepts marked there")
    sub.add_parser("gallery", help="write the review page")
    sub.add_parser("screen", help="hide candidates sensitive.json rules out (keeps indices)")
    review = sub.add_parser("review", help="serve the gallery; choices are saved to tools/images/choices.json")
    review.add_argument("--port", type=int, default=8765)
    review.add_argument("--no-open", action="store_true")
    apply = sub.add_parser("apply", help="apply choices.json")
    apply.add_argument("choices")
    apply.add_argument("--root", help="repository root to change (default: this one)")
    args = parser.parse_args()
    {"fetch": cmd_fetch, "gallery": cmd_gallery, "screen": cmd_screen, "review": cmd_review, "apply": cmd_apply}[args.command](args)


if __name__ == "__main__":
    main()
