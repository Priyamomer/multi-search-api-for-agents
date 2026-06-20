"""search — a better web-search engine, drop-in compatible with the
multi-agent-adpative-sync-system `web_search` tool.

Same contract as the original (`NAME`, `TIER`, `SPEC`, `build_handler`, and a
module-level `_search(query, n)`), so it can replace `core/tools/web_search.py`
verbatim. Still **zero third-party dependencies** — stdlib only.

What it does better than the original single-source DDG scraper:

1.  MULTI-PROVIDER FAN-OUT. Queries several backends at once (DuckDuckGo HTML,
    DuckDuckGo Lite, Wikipedia) and merges. Optional keyed providers (Brave,
    SearXNG) light up automatically when their env vars are set. One backend
    being down or rate-limited no longer means zero results.

2.  CONCURRENCY + FALLBACK. Providers run in parallel threads with a short
    per-provider timeout; the slowest never holds up the answer, and an empty
    or erroring provider is simply ignored.

3.  DEDUP. Results pointing at the same page (after URL normalisation —
    scheme/host/trailing-slash/utm params stripped) collapse into one, keeping
    the longest snippet.

4.  RELEVANCE RERANK. Merged hits are scored by query-term coverage in
    title+snippet, title-position, domain authority, and a small
    cross-provider-agreement bonus, then sorted — instead of trusting a single
    engine's order.

5.  TTL CACHE. Identical queries inside the cache window return instantly
    without touching the network.

Every failure path returns data (never raises), so an agent loop reads it as a
normal tool result and copes.
"""

import concurrent.futures
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# ── tool contract (matches the original) ────────────────────────────────────
NAME = "web_search"
TIER = "external_read"
PURPOSE = "Search the public web for current information."
DESCRIPTION = (
    "Search the web and get back a short, de-duplicated, relevance-ranked list "
    "of results (title, url, snippet) drawn from several search backends at "
    "once. Use it when the user asks about something outside the agent's own "
    "data — current events, facts, documentation. Returns at most "
    "`max_results` hits. This sends the query to external search engines."
)
SPEC = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "the search query"},
        "max_results": {"type": "integer",
                        "description": "how many results to return (1-10, default 5)"},
        "fetch_content": {"type": "boolean",
                          "description": "if true, also fetch and return cleaned "
                          "readable page text for the top results (slower, but "
                          "gives the model grounded content instead of snippets)"},
    },
    "required": ["query"],
}

# ── config ──────────────────────────────────────────────────────────────────
_UA = "Mozilla/5.0 (compatible; sync-agent/2.0)"
_PROVIDER_TIMEOUT = float(os.environ.get("WEB_SEARCH_TIMEOUT", "8"))
_CACHE_TTL = float(os.environ.get("WEB_SEARCH_CACHE_TTL", "300"))  # seconds
_OVERFETCH = 12  # pull more per provider than asked, so reranking has choices

# Domains we trust a little more, used as a small reranking tie-breaker.
_AUTHORITY = {
    "wikipedia.org": 3, "github.com": 2, "stackoverflow.com": 2,
    "python.org": 2, "developer.mozilla.org": 2, "docs.python.org": 2,
    ".gov": 2, ".edu": 2, "anthropic.com": 1, "arxiv.org": 2,
}

_STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is",
         "are", "was", "were", "how", "what", "who", "why", "when", "near",
         "best", "with", "by", "do", "does"}

_TAG_RE = re.compile(r"<[^>]+>")


# ── small helpers ────────────────────────────────────────────────────────────
def _clean(s: str) -> str:
    return html.unescape(_TAG_RE.sub("", s or "")).strip()


def _real_url(href: str) -> str:
    """Unwrap DuckDuckGo's /l/?uddg=<encoded> redirect to the real target."""
    if not href:
        return ""
    if "uddg=" in href:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        if params.get("uddg"):
            return params["uddg"][0]
    if href.startswith("//"):
        return "https:" + href
    return href


def _norm_url(url: str) -> str:
    """Canonical key for dedup: drop scheme, leading www, tracking params, and
    trailing slash, lowercase the host."""
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return url.lower()
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    keep = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
            if not k.lower().startswith("utm_") and k.lower() not in ("ref", "fbclid", "gclid")]
    query = urllib.parse.urlencode(sorted(keep))
    path = (p.path or "/").rstrip("/") or "/"
    return f"{host}{path}" + (f"?{query}" if query else "")


def _http(url, *, data=None, timeout=None, headers=None):
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"User-Agent": _UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout or _PROVIDER_TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


# ── providers ─────────────────────────────────────────────────────────────────
# Each provider is `f(query, n) -> list[{title,url,snippet,source}]`. They must
# never raise; on any trouble they return []. The runner enforces the timeout.

_DDG_HTML_RE = re.compile(
    r'result__a[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r'(?:.*?result__snippet[^>]*>(?P<snippet>.*?)</a>)?',
    re.IGNORECASE | re.DOTALL)


def _p_ddg_html(query, n):
    try:
        body = _http("https://html.duckduckgo.com/html/",
                     data=urllib.parse.urlencode({"q": query}).encode())
    except Exception:
        return []
    out = []
    for m in _DDG_HTML_RE.finditer(body):
        title = _clean(m.group("title"))
        if not title:
            continue
        out.append({"title": title, "url": _real_url(m.group("url")),
                    "snippet": _clean(m.group("snippet")), "source": "ddg_html"})
        if len(out) >= n:
            break
    return out


_DDG_LITE_RE = re.compile(
    r'<a[^>]*href="(?P<url>[^"]+)"[^>]*class=[\'"]result-link[\'"][^>]*>(?P<title>.*?)</a>'
    r'.*?class=[\'"]result-snippet[\'"][^>]*>(?P<snippet>.*?)</td>',
    re.IGNORECASE | re.DOTALL)


def _p_ddg_lite(query, n):
    try:
        body = _http("https://lite.duckduckgo.com/lite/",
                     data=urllib.parse.urlencode({"q": query}).encode())
    except Exception:
        return []
    out = []
    for m in _DDG_LITE_RE.finditer(body):
        title = _clean(m.group("title"))
        if not title:
            continue
        out.append({"title": title, "url": _real_url(m.group("url")),
                    "snippet": _clean(m.group("snippet")), "source": "ddg_lite"})
        if len(out) >= n:
            break
    return out


def _p_wikipedia(query, n):
    """Authoritative, structured, never rate-limits us — great recall anchor."""
    try:
        params = urllib.parse.urlencode({
            "action": "query", "list": "search", "srsearch": query,
            "format": "json", "srlimit": min(n, 10)})
        body = _http(f"https://en.wikipedia.org/w/api.php?{params}")
        data = json.loads(body)
    except Exception:
        return []
    out = []
    for hit in data.get("query", {}).get("search", []):
        title = hit.get("title", "")
        out.append({
            "title": title,
            "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
            "snippet": _clean(hit.get("snippet", "")),
            "source": "wikipedia"})
    return out


def _p_brave(query, n):
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        return []
    try:
        params = urllib.parse.urlencode({"q": query, "count": min(n, 20)})
        body = _http(f"https://api.search.brave.com/res/v1/web/search?{params}",
                     headers={"X-Subscription-Token": key, "Accept": "application/json"})
        data = json.loads(body)
    except Exception:
        return []
    return [{"title": _clean(r.get("title", "")), "url": r.get("url", ""),
             "snippet": _clean(r.get("description", "")), "source": "brave"}
            for r in data.get("web", {}).get("results", [])]


def _p_searx(query, n):
    # Defaults to a local SearXNG (the one set up alongside this project). If
    # nothing is listening there, _http fails fast (connection refused) and we
    # return [] — so this is safe even when SearXNG isn't running.
    base = os.environ.get("SEARXNG_URL", "http://localhost:8888")
    if not base:
        return []
    try:
        params = urllib.parse.urlencode({"q": query, "format": "json"})
        body = _http(f"{base.rstrip('/')}/search?{params}")
        data = json.loads(body)
    except Exception:
        return []
    return [{"title": _clean(r.get("title", "")), "url": r.get("url", ""),
             "snippet": _clean(r.get("content", "")), "source": "searxng"}
            for r in data.get("results", [])[:n]]


_PROVIDERS = [_p_ddg_html, _p_ddg_lite, _p_wikipedia, _p_brave, _p_searx]


# ── ranking ───────────────────────────────────────────────────────────────────
def _terms(q):
    return [t for t in re.findall(r"[a-z0-9]+", q.lower()) if t not in _STOP]


def _authority(url):
    host = urllib.parse.urlsplit(url).netloc.lower()
    return sum(b for dom, b in _AUTHORITY.items() if dom in host)


_RRF_K = 60  # standard RRF damping constant (Cormack et al.)


def _lexical(r, terms):
    """0..1 query-term coverage over title+snippet — the sparse/lexical signal
    in the hybrid blend. Title hits weigh double."""
    if not terms:
        return 0.0
    title, snip = r["title"].lower(), r["snippet"].lower()
    in_title = sum(1 for t in terms if t in title)
    in_snip = sum(1 for t in terms if t in snip)
    return (in_title * 2 + in_snip) / (len(terms) * 3)


def _merge_rank(per_provider, terms, n):
    """Reciprocal Rank Fusion across providers, then a small hybrid tie-break.

    RRF (frontier-standard for fusing heterogeneous rankers) scores each result
    by Σ 1/(k + rank_in_that_provider), so a page ranked highly by several
    engines floats up without any engine's raw score being comparable to
    another's. We then add a lightweight lexical-coverage + domain-authority
    nudge so that, among RRF-equivalent hits, the more on-topic/trusted one wins.
    """
    by_key, rrf, agree = {}, {}, {}
    for results in per_provider:
        for rank, r in enumerate(results):
            if not r.get("url") or not r.get("title"):
                continue
            key = _norm_url(r["url"])
            rrf[key] = rrf.get(key, 0.0) + 1.0 / (_RRF_K + rank)
            agree[key] = agree.get(key, 0) + 1
            cur = by_key.get(key)
            if cur is None:
                by_key[key] = dict(r)
            elif len(r.get("snippet", "")) > len(cur.get("snippet", "")):
                cur["snippet"] = r["snippet"]  # keep richest snippet

    def final(r):
        key = _norm_url(r["url"])
        # RRF dominates; lexical+authority+agreement break ties (all small).
        return (rrf[key]
                + _lexical(r, terms) * 0.02
                + _authority(r["url"]) * 0.005
                + (agree[key] - 1) * 0.003)

    ranked = sorted(by_key.values(), key=final, reverse=True)
    return [{"title": r["title"], "url": r["url"], "snippet": r["snippet"],
             "source": r.get("source", "")} for r in ranked[:n]]


# ── content extraction (the "context-ready content" frontier feature) ─────────
_SCRIPT_RE = re.compile(r"<(script|style|noscript|svg|head)[^>]*>.*?</\1>",
                        re.IGNORECASE | re.DOTALL)
_BLOCK_RE = re.compile(r"</(p|div|section|article|li|h[1-6]|br|tr)>",
                       re.IGNORECASE)
_CONTENT_CHARS = int(os.environ.get("WEB_SEARCH_CONTENT_CHARS", "2000"))


def _extract(url, limit=_CONTENT_CHARS):
    """GET a page and return cleaned, readable plain text (scripts/markup
    stripped, whitespace collapsed), truncated to `limit` chars. Returns "" on
    any failure — content fetch must never break a search."""
    try:
        body = _http(url, timeout=min(_PROVIDER_TIMEOUT, 6))
    except Exception:
        return ""
    body = _SCRIPT_RE.sub(" ", body)
    body = _BLOCK_RE.sub("\n", body)           # keep paragraph breaks
    text = html.unescape(_TAG_RE.sub(" ", body))
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text).strip()
    return text[:limit]


def _add_content(results, k=3):
    """Fetch readable text for the top-k results concurrently, in place."""
    top = results[:k]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(top))) as ex:
        futs = {ex.submit(_extract, r["url"]): r for r in top}
        for fut in concurrent.futures.as_completed(futs):
            try:
                futs[fut]["content"] = fut.result()
            except Exception:
                futs[fut]["content"] = ""
    return results


# ── cache ─────────────────────────────────────────────────────────────────────
_cache = {}
_cache_lock = threading.Lock()


def _cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if item and (time.time() - item[0]) < _CACHE_TTL:
            return item[1]
        if item:
            _cache.pop(key, None)
    return None


def _cache_put(key, value):
    with _cache_lock:
        _cache[key] = (time.time(), value)


# ── public entry points ───────────────────────────────────────────────────────
def _search(query: str, n: int, fetch_content: bool = False) -> dict:
    """Drop-in replacement for the original `_search`. Same shape:
    {"query", "results": [{title,url,snippet}]} or {"error": ...}.
    If `fetch_content`, each top result also carries a "content" field with
    cleaned readable page text."""
    n = max(1, min(int(n or 5), 10))
    ckey = f"{n}\x00{int(bool(fetch_content))}\x00{query.strip().lower()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return {**cached, "cached": True}

    per_provider, errors = [], 0
    fetch = max(n, _OVERFETCH)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(_PROVIDERS)) as ex:
        futs = {ex.submit(p, query, fetch): p for p in _PROVIDERS}
        for fut in concurrent.futures.as_completed(futs, timeout=_PROVIDER_TIMEOUT + 2):
            try:
                res = fut.result()
                if res:
                    per_provider.append(res)
            except Exception:
                errors += 1

    if not per_provider:
        return {"error": "all search backends failed or returned nothing",
                "query": query}

    results = _merge_rank(per_provider, _terms(query), n)
    if fetch_content and results:
        _add_content(results)
    out = {"query": query, "results": results,
           "providers_used": sorted({item["source"]
                                     for plist in per_provider for item in plist
                                     if item.get("source")})}
    _cache_put(ckey, out)
    return {**out, "cached": False}


def build_handler(ctx):
    def handle(args: dict) -> dict:
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "query is required"}
        try:
            n = int(args.get("max_results") or 5)
        except (TypeError, ValueError):
            n = 5
        return _search(query, n, fetch_content=bool(args.get("fetch_content")))
    return handle


# ── CLI for manual testing ────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    argv = [a for a in sys.argv[1:] if a != "--content"]
    want_content = "--content" in sys.argv
    q = " ".join(argv) or "latest claude opus model"
    t = time.time()
    res = _search(q, 5, fetch_content=want_content)
    dt = time.time() - t
    print(f"query: {q!r}   ({dt:.2f}s)")
    print("providers:", res.get("providers_used"), " cached:", res.get("cached"))
    if res.get("error"):
        print("ERROR:", res["error"])
    for i, r in enumerate(res.get("results", []), 1):
        print(f"\n{i}. {r['title']}\n   {r['url']}\n   [{r['source']}] {r['snippet'][:140]}")
        if r.get("content"):
            print(f"   ── content ({len(r['content'])} chars): {r['content'][:200]}…")
