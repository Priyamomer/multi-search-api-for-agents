# web-search-api

A better web-search engine — a **drop-in replacement** for the `web_search`
tool in `multi-agent-adpative-sync-system`. Stdlib only, no dependencies.

## Why it's better than the original

The original (`core/tools/web_search.py`) scrapes a single source — DuckDuckGo's
HTML endpoint — with one regex. When DDG rate-limits or changes its markup, it
returns **zero results** and the agent is blind.

This engine fixes that:

| | Original | This engine |
|---|---|---|
| Sources | DuckDuckGo HTML only | DDG HTML + DDG Lite + Wikipedia (+ Brave / SearXNG if keyed) |
| On a source failing | 0 results | falls back to the others |
| Execution | sequential | concurrent fan-out, per-provider timeout |
| Duplicates | none removed | URL-normalised dedup |
| Ordering | one engine's raw order | reranked by term-coverage + domain authority + cross-engine agreement |
| Ranking method | one engine's order | **Reciprocal Rank Fusion** + hybrid lexical/authority tie-break |
| Output | snippets only | snippets, or **full extracted page content** (`fetch_content`) |
| Repeat queries | re-hit network | TTL cache (instant) |

## Frontier techniques adopted (from 2026 RAG / search-API research)

The design tracks where production search APIs (Tavily, Exa, Brave LLM Context)
and RAG pipelines have moved, implemented stdlib-only:

- **Reciprocal Rank Fusion (RRF)** — the standard way to merge heterogeneous
  rankers: each result scored by `Σ 1/(k + rank)` across providers, so pages
  multiple engines agree on rise to the top. Reported to lift retrieval
  precision 15–30% vs single-ranker order. A light lexical-coverage +
  domain-authority term breaks ties (a hybrid sparse signal).
- **Context-ready content, not snippets** — the defining shift in LLM search
  APIs. `fetch_content=true` fetches the top results and returns cleaned,
  readable page text so the model grounds on real content, not 1-line snippets.
- **Multi-provider fusion** — resilience + recall, as above.

Deliberately left as hooks (need a model / are out of scope for a search
primitive): cross-encoder neural reranking (the agent's own LLM can rerank the
fused list), LLM query rewriting, and GraphRAG-style multi-step agentic
retrieval.

> Note: content extraction is regex-based readable-text stripping (zero-dep), so
> some pages include nav/boilerplate. Swapping in a readability library would
> sharpen it if a dependency is acceptable.

### Measured (5 queries, identical to the original's contract)

```
query                              OLD            NEW
latest claude opus model release   5 in 1.03s     5 in 1.06s  [ddg_html,ddg_lite,wikipedia]
python sort a list by key          5 in 1.03s     5 in 0.96s  [ddg_html,ddg_lite,wikipedia]
who won the 2026 super bowl        5 in 0.99s     5 in 1.03s  [ddg_lite,wikipedia]
climate change effects on oceans   0 in 0.22s     5 in 0.72s  [wikipedia]   <- old failed
rust vs go performance             0 in 0.21s     5 in 0.62s  [wikipedia]   <- old failed
```

Same speed when DDG is healthy; still answers when it isn't.

## Usage

CLI:
```bash
python3 search.py rust vs go performance
```

With full page content (frontier mode):
```bash
python3 search.py --content reciprocal rank fusion explained
```

As a library (same shape as the original):
```python
import search
search._search("query", 5)
# -> {"query", "results": [{title, url, snippet, source}], "providers_used", "cached"}

search._search("query", 3, fetch_content=True)
# each result additionally carries "content": cleaned readable page text
```

## Drop it into the multi-agent system

It exposes the identical contract (`NAME`, `TIER`, `SPEC`, `build_handler`,
`_search`), so:

```bash
cp search.py /Users/fsi/Desktop/CODES/multi-agent-adpative-sync-system/core/tools/web_search.py
```

## Optional providers (auto-enabled when set)

```bash
export BRAVE_API_KEY=...      # adds Brave Search
export SEARXNG_URL=https://…  # adds a SearXNG instance
export WEB_SEARCH_CACHE_TTL=300
export WEB_SEARCH_TIMEOUT=8
```
