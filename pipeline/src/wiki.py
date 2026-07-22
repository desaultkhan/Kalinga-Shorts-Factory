from __future__ import annotations
"""
wiki.py — a tiny Wikipedia connector (stdlib only, no `requests`).

Grounds a channel's `llm` researcher in real, citable facts (opt in per channel
with `wiki_grounding: true`): it resolves a topic to the right Wikipedia page
and pulls the plain-text extract + key metadata (the lead section, the
description, the canonical URL). The researcher then hands that extract to the
LLM as GROUND TRUTH alongside a live web search, so dates and numbers come from
a real source instead of being hallucinated.

Every call DEGRADES GRACEFULLY (returns None/{}/[]) on a network error, a
missing page, or a disambiguation, so research never hard-fails on Wikipedia.

Wikimedia asks for a descriptive User-Agent; we send one.
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://en.wikipedia.org/w/api.php"
REST = "https://en.wikipedia.org/api/rest_v1"
UA = "KalingaShortsFactory/1.0 (Wikipedia grounding; contact via channel)"

# How much article text to ground the LLM on. A rich topic runs 20k-80k chars
# and the WHOLE arc matters (the payoff/legacy often lives in the second
# half), so a small cap silently truncates most of it. This is generous
# enough to carry a full article for the default Opus context, with a ceiling only
# to bound a pathological mega-article.
FULL_EXTRACT_CHARS = 100_000


def _get(url: str, timeout: int = 20, quiet_404: bool = True):
    """GET JSON. A 404 is an EXPECTED miss while resolving a title (we then fall
    back to search), so it's swallowed silently by default — only real failures
    (network, 5xx, bad JSON) print. Returns None on any failure."""
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:          # subclass of URLError — first
        if not (quiet_404 and e.code == 404):
            print(f"  ! wikipedia request failed: HTTP {e.code}", file=sys.stderr)
        return None
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        print(f"  ! wikipedia request failed: {str(e)[:120]}", file=sys.stderr)
        return None


def search(query: str, limit: int = 5) -> list:
    """Best-matching page titles for a free-text query (most relevant first)."""
    if not query:
        return []
    qs = urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": query,
        "srlimit": limit, "format": "json"})
    data = _get(f"{API}?{qs}")
    try:
        return [h["title"] for h in data["query"]["search"]]
    except (TypeError, KeyError):
        return []


def summary(title: str) -> dict:
    """REST page summary: {title, description, extract, url, thumbnail, image,
    type}. `image` is the FULL-resolution lead photo (originalimage), falling
    back to the smaller thumbnail — a lead image the visual pipeline can work
    off of. `type` == 'disambiguation' signals the title wasn't specific."""
    if not title:
        return {}
    data = _get(f"{REST}/page/summary/{urllib.parse.quote(title)}")
    if not data:
        return {}
    thumb = (data.get("thumbnail") or {}).get("source")
    original = (data.get("originalimage") or {}).get("source")
    return {
        "title": data.get("title"),
        "description": data.get("description"),
        "extract": data.get("extract"),
        "url": (data.get("content_urls", {}).get("desktop", {}) or {}).get("page"),
        "thumbnail": thumb,
        "image": original or thumb,
        "type": data.get("type"),
    }


def extract(title: str, intro_only: bool = False,
            chars: int = FULL_EXTRACT_CHARS) -> str:
    """Plain-text article extract (the full body, or just the lead with
    intro_only), capped to `chars` (generous — the LLM gets the whole story to
    ground on, not a snippet). The grounding text for the researcher."""
    if not title:
        return ""
    params = {"action": "query", "prop": "extracts", "explaintext": 1,
              "redirects": 1, "titles": title, "format": "json"}
    if intro_only:
        params["exintro"] = 1
    data = _get(f"{API}?{urllib.parse.urlencode(params)}")
    try:
        pages = data["query"]["pages"]
        page = next(iter(pages.values()))
        return (page.get("extract") or "")[:chars]
    except (TypeError, KeyError, StopIteration):
        return ""


def page(query: str, chars: int = FULL_EXTRACT_CHARS) -> dict:
    """Resolve a name/company to its best page and return the grounding bundle:
    {title, description, extract (full plain text), url, thumbnail, sources}.
    Returns {} when nothing usable is found (caller falls back to web/LLM only).

    Resolution: try the query verbatim first (a clean title is exact); if it's
    missing or a disambiguation, fall back to the top search hit."""
    title = (query or "").strip()
    s = summary(title) if title else {}
    if not s or s.get("type") == "disambiguation" or not s.get("extract"):
        hits = search(query, limit=1)
        if hits:
            s = summary(hits[0])
    if not s or not s.get("extract"):
        return {}
    body = extract(s["title"], chars=chars) or s.get("extract") or ""
    out = {
        "title": s.get("title"),
        "description": s.get("description"),
        "extract": body,
        "url": s.get("url"),
        "thumbnail": s.get("thumbnail"),
        "image": s.get("image"),
    }
    out["sources"] = [out["url"]] if out.get("url") else []
    return out


def _img_ext(url: str, ctype: str) -> str:
    """Pick a sensible file extension from the URL, then the Content-Type."""
    base = (url or "").lower().split("?")[0]
    for cand in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if base.endswith(cand):
            return ".jpg" if cand == ".jpeg" else cand
    for key, ext in (("png", ".png"), ("webp", ".webp"), ("gif", ".gif")):
        if key in (ctype or ""):
            return ext
    return ".jpg"


def fetch_image(url: str, dest_stem, timeout: int = 20):
    """Download an image `url` to `dest_stem` + the right extension (e.g.
    "<run>/lead" → "<run>/lead.jpg"). Returns the saved Path, or None on
    any failure — best-effort, never fatal (mirrors the rest of this module).

    The host isn't pinned (Wikipedia serves images off upload.wikimedia.org),
    but the URL comes from a Wikipedia summary we fetched, not user input."""
    if not url:
        return None
    from pathlib import Path
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            blob = r.read()
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        print(f"  ! wikipedia image download failed: {str(e)[:120]}",
              file=sys.stderr)
        return None
    if not blob:
        return None
    dest = Path(dest_stem).with_suffix(_img_ext(url, ctype))
    try:
        dest.write_bytes(blob)
    except OSError as e:
        print(f"  ! could not save wikipedia image: {str(e)[:120]}",
              file=sys.stderr)
        return None
    return dest


if __name__ == "__main__":
    import pprint
    q = " ".join(sys.argv[1:]) or "Krakatoa"
    p = page(q)
    pprint.pprint({k: (v[:200] + "…" if isinstance(v, str) and len(v) > 200
                       else v) for k, v in p.items()})
