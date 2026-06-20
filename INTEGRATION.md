# Integration handoff — switch the search engine to SearXNG

This document is written for another AI / developer who needs to point a system
at the SearXNG-backed search engine in this folder. Everything needed is below:
endpoints, parameters, env vars, the tool contract, and drop-in steps.

---

## 1. The SearXNG endpoint (the actual search engine)

A local SearXNG instance is running in Docker and exposes a JSON search API.

- **Base URL:** `http://localhost:8888`
- **Search endpoint:** `GET http://localhost:8888/search`
- **Required query params:**
  - `q` — the search query (URL-encoded)
  - `format=json` — return JSON (SearXNG also serves `html`, `csv`, `rss`)
- **Optional params:**
  - `categories=general` (or `news`, `images`, `science`, …)
  - `language=en`
  - `pageno=1`
  - `engines=google,bing,brave,duckduckgo` — restrict which upstream engines

### Raw request example
```bash
curl -A "Mozilla/5.0" \
  "http://localhost:8888/search?q=donald+trump+india+relations&format=json"
```
(Sending a normal User-Agent is good practice, though not strictly required.)

### If you get 0 results: check the upstream engines
SearXNG forwards to Google/Bing/DuckDuckGo/etc. Under heavy use those suspend
your IP (CAPTCHA / "too many requests"), so SearXNG returns an empty list even
though it's healthy. The response tells you exactly which:
```bash
curl -s "http://localhost:8888/search?q=test&format=json" \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('unresponsive_engines'))"
# e.g. [["google","Suspended: CAPTCHA"], ["duckduckgo","CAPTCHA"]]
```
Fix: wait for the suspension to clear (minutes–1h), pace your requests, or limit
to engines that aren't blocked via `&engines=bing,mojeek,wikipedia`.

### Response shape (trimmed)
```json
{
  "query": "donald trump india relations",
  "number_of_results": 0,
  "results": [
    {
      "url": "https://example.com/article",
      "title": "Trump and India ...",
      "content": "snippet text ...",
      "engine": "google",
      "score": 1.0,
      "category": "general"
    }
  ]
}
```
Read `title`, `url`, and `content` (the snippet) from each item in `results`.

> NOTE: SearXNG ships with the JSON format **disabled** by default. This
> instance has it enabled via `searxng-config/settings.yml`
> (`search.formats: [html, json]`). A vanilla SearXNG will return HTTP 403 for
> `format=json` until that setting is added.

---

## 2. Docker lifecycle (managing the engine)

```bash
docker start searxng        # start it (auto-starts on boot: restart=unless-stopped)
docker stop searxng         # stop it
docker restart searxng      # restart
docker logs -f searxng      # watch logs
docker ps --filter name=searxng     # check status
```
Config lives in `./searxng-config/settings.yml`; edit it then `docker restart searxng`.
Requires Docker Desktop to be running (enable "start at login" on macOS).

---

## 3. The wrapper engine: `search.py`

`search.py` in this folder is a higher-level engine that queries SearXNG **plus**
DuckDuckGo and Wikipedia in parallel, de-duplicates, and reranks with Reciprocal
Rank Fusion. **Use this rather than calling SearXNG directly** if you want
resilience + ranking. It already defaults to the local SearXNG.

### Programmatic use (Python)
```python
import search
res = search._search("donald trump india relations", 5)
# res = {
#   "query": str,
#   "results": [{"title", "url", "snippet", "source"}],  # source = which backend
#   "providers_used": ["searxng", "wikipedia", ...],
#   "cached": bool,
# }

# with full page-text extraction:
res = search._search("query", 3, fetch_content=True)   # each result gains "content"
```

### CLI
```bash
python3 search.py donald trump india relations
python3 search.py --content donald trump india relations
```

---

## 4. Configuration (environment variables)

| Var | Default | Meaning |
|---|---|---|
| `SEARXNG_URL` | `http://localhost:8888` | SearXNG base URL. **Set this to switch the engine.** |
| `WEB_SEARCH_TIMEOUT` | `8` | Per-provider timeout (seconds) |
| `WEB_SEARCH_CACHE_TTL` | `300` | In-memory result cache TTL (seconds) |
| `WEB_SEARCH_CONTENT_CHARS` | `2000` | Max chars of extracted page text |
| `BRAVE_API_KEY` | (unset) | Enables Brave provider if set |

To point at a **different / remote** SearXNG, just set:
```bash
export SEARXNG_URL=https://my-searxng.example.com
```

---

## 5. Drop-in for the multi-agent-adpative-sync-system

That system loads `core/tools/web_search.py` and calls `_search(query, n)` plus
`build_handler(ctx)`. `search.py` here exposes the **identical contract**
(`NAME`, `TIER`, `PURPOSE`, `DESCRIPTION`, `SPEC`, `build_handler`, `_search`),
so it is a literal drop-in:

```bash
cp /Users/fsi/Desktop/CODES/web-search-api/search.py \
   /Users/fsi/Desktop/CODES/multi-agent-adpative-sync-system/core/tools/web_search.py
```

No other code changes are required. The tool name stays `web_search`; the agent
calls it exactly as before, now backed by SearXNG + fallbacks.

### Tool SPEC the agent sees
```json
{
  "type": "object",
  "properties": {
    "query":        {"type": "string"},
    "max_results":  {"type": "integer", "description": "1-10, default 5"},
    "fetch_content":{"type": "boolean", "description": "also return cleaned page text"}
  },
  "required": ["query"]
}
```

### Alternative: keep the original file, just swap the backend
The *original* `web_search.py` honors a `WEB_SEARCH_URL` env var that overrides
its single endpoint. But SearXNG's response is JSON, and the original parses
HTML with regex — so pointing `WEB_SEARCH_URL` at SearXNG will **not** work.
Use the drop-in copy above instead (it speaks SearXNG's JSON natively).

---

## 6. Quick verification after switching

```bash
# 1. engine reachable + JSON enabled (note the User-Agent):
curl -s -A "Mozilla/5.0" "http://localhost:8888/search?q=test&format=json" | head -c 200

# 2. wrapper sees SearXNG:
python3 -c "import search; print(search._search('test query',3)['providers_used'])"
# expect a list that includes 'searxng'

# 3. side-by-side sanity:
python3 compare.py donald trump india relations
```
If `providers_used` includes `searxng`, the switch is live.
