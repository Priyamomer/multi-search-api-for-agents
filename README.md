# multi-search-api-for-agents

A **resilient, zero-dependency web-search engine for LLM agents.** It fans a
single query out to several search backends in parallel (SearXNG → Google/Bing,
DuckDuckGo, Wikipedia), de-duplicates, reranks the merged hits with **Reciprocal
Rank Fusion**, caches results, and can optionally return full extracted page
text for grounding.

It exists because the common pattern — *scrape one search engine and hope* —
silently returns **zero results** the moment that engine rate-limits you. For an
autonomous agent, "sometimes returns nothing" is a real bug. This engine removes
the single point of failure.

> Pure Python standard library. **No pip install. No API key required.**

---

## Table of contents
- [Why](#why)
- [Quick start](#quick-start)
- [Optional: SearXNG backend (free Google/Bing results)](#optional-searxng-backend-free-googlebing-results)
- [Usage examples](#usage-examples)
- [How it works](#how-it-works)
- [Providers](#providers)
- [Benchmarks & detailed analysis](#benchmarks--detailed-analysis)
- [Configuration](#configuration)
- [Drop-in for an existing agent](#drop-in-for-an-existing-agent)
- [Limitations (honest)](#limitations-honest)

---

## Why

| | Typical single-source scraper | This engine |
|---|---|---|
| Sources | 1 (e.g. DuckDuckGo) | up to 5, in parallel |
| One source fails | **0 results** | falls back to the others |
| Ranking | that engine's raw order | Reciprocal Rank Fusion across engines |
| Duplicates | none removed | URL-normalised dedup |
| Repeat queries | re-hit network | in-memory TTL cache (instant) |
| Output | snippets only | snippets **or** full extracted page text |
| Dependencies | varies | **none** (stdlib only) |

The payoff is reliability. In a 100-query stress test (below), the single-source
baseline answered **9%** of the time; this engine answered **65%** — and ~100%
at realistic request pacing.

---

## Quick start

```bash
git clone https://github.com/Priyamomer/multi-search-api-for-agents.git
cd multi-search-api-for-agents

# search the web (works immediately — DuckDuckGo + Wikipedia, no setup)
python3 search.py latest claude opus model release

# with full page-text extraction for the top results
python3 search.py --content reciprocal rank fusion explained
```

Requires only Python 3.8+. That's it for the basic engine.

---

## Optional: SearXNG backend (free Google/Bing results)

DuckDuckGo + Wikipedia work out of the box, but for top-tier resilience and
Google/Bing-quality results, run a local **SearXNG** (a free, self-hosted
metasearch engine). The engine auto-detects it at `http://localhost:8888`.

```bash
docker run -d --name searxng --restart unless-stopped \
  -p 8888:8080 \
  -v "$(pwd)/searxng-config:/etc/searxng" \
  searxng/searxng

# verify it's up (JSON API is enabled in searxng-config/settings.yml)
curl -s "http://localhost:8888/search?q=test&format=json" | head -c 120
```

Now `search.py` automatically includes SearXNG — no env var, no code change. To
point at a remote instance instead:

```bash
export SEARXNG_URL=https://my-searxng.example.com
```

SearXNG turns one query into a polite, parallel fan-out to Google, Bing, Brave,
etc., so you get top-engine results for free with no API key and no per-query
cost. See [INTEGRATION.md](INTEGRATION.md) for the full endpoint reference.

---

## Usage examples

### Command line
```bash
python3 search.py rust vs go performance          # search
python3 search.py --content climate change oceans # + readable page text
```

### As a Python library
```python
import search

res = search._search("who won the 2026 super bowl", max_results=5)
# {
#   "query": "who won the 2026 super bowl",
#   "results": [
#     {"title": "...", "url": "...", "snippet": "...", "source": "searxng"},
#     ...
#   ],
#   "providers_used": ["searxng", "wikipedia", "ddg_lite"],
#   "cached": False,
# }

for r in res["results"]:
    print(r["title"], "—", r["url"], f"[{r['source']}]")

# grounded mode: each result also gets a cleaned "content" field
res = search._search("python asyncio tutorial", 3, fetch_content=True)
print(res["results"][0]["content"][:500])
```

### Side-by-side comparison tool
```bash
python3 compare.py climate change effects on oceans
```
```
  QUERY: 'climate change effects on oceans'

OLD  (ddg-scrape)   1.04s                  │ NEW  (ddg-lite+searx+wikipedia)  1.08s
─────────────────────────────────────────  │ ───────────────────────────────────────
5 results                                  │ 5 results · providers: ddg_lite,searx,wikipedia
1. How is climate change impacting ...     │ 1. Effects of climate change on oceans
   un.org/...                              │    en.wikipedia.org/...
...                                        │ ...
  ── metrics ──
  results returned   OLD=5   NEW=5
  backends           OLD=1   NEW=3 (ddg_lite,searxng,wikipedia)
  unique urls NEW found that OLD missed: 1
```

### Reliability stress test
```bash
python3 stress.py 100 your query here     # per-provider survival + latency percentiles
```

---

## How it works

```
                       query
                         │
        ┌────────────────┼────────────────┬───────────────┐
        ▼                ▼                ▼               ▼
   ddg_html         ddg_lite         wikipedia        searxng ──► Google/Bing/Brave…
   (scrape)         (scrape)         (JSON API)       (JSON API)
        └────────────────┴────────────────┴───────────────┘
                         │  parallel threads, per-provider timeout
                         ▼
                  de-duplicate (URL-normalised)
                         ▼
              Reciprocal Rank Fusion  Σ 1/(k + rank)
              + lexical / authority / agreement tie-break
                         ▼
                  top-N results  (+ optional page-text fetch)
                         ▼
                    TTL cache
```

**Reciprocal Rank Fusion (RRF)** is the standard way to merge rankings from
different engines whose scores aren't comparable. Each result gets
`Σ 1/(k + rank_in_that_engine)` (k=60), so a page several engines rank highly
floats to the top — no engine's raw score needs to mean anything. A small
lexical-coverage + domain-authority + cross-engine-agreement term breaks ties.

---

## Providers

| Source label | What it is | Needs |
|---|---|---|
| `ddg_html` | DuckDuckGo full HTML results | nothing |
| `ddg_lite` | DuckDuckGo Lite (text-only endpoint, same index) | nothing |
| `wikipedia` | Wikipedia search API (authoritative facts) | nothing |
| `searxng` | Self-hosted SearXNG → Google/Bing/Brave/etc. | local Docker (optional) |
| `brave` | Brave Search API | `BRAVE_API_KEY` (optional) |

`ddg_html` and `ddg_lite` hit the **same** DuckDuckGo index through two different
endpoints — they don't fail together, giving a cheap second shot at the data.
Any provider that isn't configured (no key / no URL) is simply skipped.

---

## Benchmarks & detailed analysis

All numbers below were measured on a real machine during development. Your
mileage varies with network and how aggressively engines are throttling.

### 1. Quality + latency, healthy network (5 queries)

| Query | OLD (DDG only) | NEW (multi + RRF) |
|---|---|---|
| latest claude opus release | 5 in 1.03s | 5 in 1.06s |
| python sort a list by key | 5 in 1.03s | 5 in 0.96s |
| who won 2026 super bowl | 5 in 0.99s | 5 in 1.03s |
| climate change effects on oceans | **0 in 0.22s** | **5 in 0.72s** |
| rust vs go performance | **0 in 0.21s** | **5 in 0.62s** |

**Read:** when DuckDuckGo is healthy, both return similar top-5 at similar speed
(providers run concurrently, so 4 backends cost ~1 backend's time). When DDG is
throttled, OLD returns nothing in ~0.2s (that's *failure* speed) while NEW still
answers via fallback. RRF gives a modest reranking edge, not a dramatic one — the
headline win is availability, not ordering.

### 2. Reliability under load — 20 runs, cache bypassed

| Setup | OLD availability | NEW availability |
|---|---|---|
| NEW **without** SearXNG | 15% | 50% |
| NEW **with** SearXNG | 5% | **100%** |

SearXNG is the independent backend that doesn't share DuckDuckGo's rate-limiter.
Adding it took NEW from 50% → 100% availability while the single-source baseline
sat at 5%. In that run NEW answered **19 of the 20** queries where OLD returned
nothing.

### 3. Sustained abuse — 100 runs, cache bypassed, ~0.5 runs/s

```
NEW availability:  65.0%   (65/100 answered)
OLD availability:   9.0%   ( 9/100 answered)

per-provider availability (returned >0):
   ddg_html    6.0%      ← collapses fast under load
   ddg_lite    6.0%
   wikipedia  40.0%      ← throttles slower (different operator)
   searxng    50.0%      ← the workhorse
   brave       0.0%      ← DISABLED (no API key) — not a failure

NEW latency: p50=1.51s p90=2.47s p99=4.75s
OLD latency: p50=0.12s p90=0.71s p99=1.09s   (fast because it's returning nothing)
```

**Read carefully — two things make NEW look worse than reality here:**

1. **The cache is deliberately disabled.** Every one of the 100 queries hits the
   network. In real use, repeated/similar queries return instantly from the TTL
   cache and never touch an upstream engine.
2. **`brave = 0%` is a red herring** — it's not failing, it's *off* (no API key),
   so it returns `[]` by design and drags the per-provider table down.

The percentages are also **cumulative averages that hide a decline**: the first
25 runs were ~100% available; by runs 75–100 the test had CAPTCHA-suspended the
machine's IP across Google/Bing/DDG, dropping even SearXNG to ~50%. That is the
*artificial-load* effect of firing 100 cache-bypassed queries in 190 seconds — at
real agent pacing (seconds–minutes apart, cache on) you stay near the top.

**Bottom line:** under deliberate abuse the engine degrades *gracefully*
(100% → 65%) where the single-source baseline is effectively dead (9%). At normal
pace it is effectively always-on.

---

## Configuration

All optional. Set via environment variables.

| Var | Default | Meaning |
|---|---|---|
| `SEARXNG_URL` | `http://localhost:8888` | SearXNG base URL (set to use a remote one) |
| `BRAVE_API_KEY` | *(unset)* | Enables the Brave provider |
| `WEB_SEARCH_TIMEOUT` | `8` | Per-provider timeout (seconds) |
| `WEB_SEARCH_CACHE_TTL` | `300` | Result cache lifetime (seconds) |
| `WEB_SEARCH_CONTENT_CHARS` | `2000` | Max chars of extracted page text |

---

## Drop-in for an existing agent

The engine exposes a tool contract (`NAME`, `TIER`, `SPEC`, `build_handler`,
`_search`) compatible with simple agent tool loops. To replace a single-source
`web_search.py` tool, copy this file over it:

```bash
cp search.py /path/to/your-agent/core/tools/web_search.py
```

The tool the agent sees:

```json
{
  "name": "web_search",
  "input_schema": {
    "type": "object",
    "properties": {
      "query":         {"type": "string"},
      "max_results":   {"type": "integer", "description": "1-10, default 5"},
      "fetch_content": {"type": "boolean", "description": "also return cleaned page text"}
    },
    "required": ["query"]
  }
}
```

Full endpoint + integration reference: [INTEGRATION.md](INTEGRATION.md).

---

## Limitations (honest)

- **Not magic immunity.** Hammer it thousands of times and Google/Bing will
  CAPTCHA-suspend your IP across *all* engines, including SearXNG's upstreams.
  It degrades gracefully instead of dying, but it isn't infinite. Pace requests.
- **Content extraction is regex-based** readable-text stripping (to stay
  zero-dependency), so some pages leak nav/boilerplate. A readability library
  would sharpen it at the cost of a dependency.
- **No true semantic / vector search.** RRF + lexical coverage approximates
  relevance; it doesn't embed queries. The agent's own LLM can rerank the fused
  list if you need semantic precision.
- **SearXNG needs Docker running.** After a reboot: `docker start searxng`
  (already set to `--restart unless-stopped`, so it returns once Docker is up).

---

## Repository layout

```
search.py            the engine (providers, RRF fusion, cache, content extraction)
compare.py           side-by-side old-vs-new with metrics
stress.py            high-volume reliability probe with per-provider telemetry
searxng-config/      local SearXNG settings (JSON API enabled)
README.md            this file
INTEGRATION.md       endpoint reference + how to switch a system to SearXNG
```

## License

MIT — use it freely.
