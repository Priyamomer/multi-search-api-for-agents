"""baseline_ddg — the original single-source web_search, kept here as the
comparison baseline.

This is the *before* picture: scrape one source (DuckDuckGo's HTML endpoint)
with one regex, zero dependencies. It is what `search.py` improves on, and what
`compare.py` measures against. Preserved verbatim so the comparison stays honest
and reproducible for anyone who clones this repo (the live agent system may have
already swapped its own copy for the new engine).

Zero dependencies: stdlib urllib against DuckDuckGo's HTML endpoint, parsed with
regex. The endpoint is overridable via WEB_SEARCH_URL. Every failure path
returns {"error": ...}.
"""

import html
import os
import re
import urllib.error
import urllib.parse
import urllib.request

NAME = "web_search"
TIER = "external_read"
PURPOSE = "Search the public web for current information."
DESCRIPTION = (
    "Search the web and get back a short list of results (title, url, snippet). "
    "Use it when the user asks about something outside the agent's own data — "
    "current events, facts, documentation. Returns at most `max_results` hits. "
    "This sends the query to an external search engine."
)
SPEC = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "the search query"},
        "max_results": {"type": "integer",
                        "description": "how many results to return (1-10, default 5)"},
    },
    "required": ["query"],
}

_ENDPOINT = os.environ.get("WEB_SEARCH_URL", "https://html.duckduckgo.com/html/")
_UA = "Mozilla/5.0 (compatible; sync-agent/1.0)"
_RESULT_RE = re.compile(
    r'result__a[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r'(?:.*?result__snippet[^>]*>(?P<snippet>.*?)</a>)?',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    return html.unescape(_TAG_RE.sub("", s or "")).strip()


def _real_url(href: str) -> str:
    """DuckDuckGo wraps results in a redirect (/l/?uddg=<encoded>); unwrap to the
    real target when present, otherwise return the href as-is."""
    if "uddg=" in href:
        q = urllib.parse.urlparse(href).query
        params = urllib.parse.parse_qs(q)
        if params.get("uddg"):
            return params["uddg"][0]
    return href if href.startswith("http") else "https:" + href


def _search(query: str, n: int) -> dict:
    data = urllib.parse.urlencode({"q": query}).encode("utf-8")
    req = urllib.request.Request(_ENDPOINT, data=data,
                                 headers={"User-Agent": _UA}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return {"error": f"search backend HTTP {e.code}"}
    except urllib.error.URLError as e:
        return {"error": f"could not reach search backend: {e.reason}"}
    except Exception as e:                            # noqa: BLE001 — never raise into loop
        return {"error": f"search failed: {type(e).__name__}: {e}"}
    results = []
    for m in _RESULT_RE.finditer(body):
        title = _clean(m.group("title"))
        if not title:
            continue
        results.append({"title": title,
                        "url": _real_url(m.group("url")),
                        "snippet": _clean(m.group("snippet"))})
        if len(results) >= n:
            break
    return {"query": query, "results": results}


def build_handler(ctx):
    def handle(args: dict) -> dict:
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "query is required"}
        try:
            n = int(args.get("max_results") or 5)
        except (TypeError, ValueError):
            n = 5
        return _search(query, max(1, min(n, 10)))

    return handle
