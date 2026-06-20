#!/usr/bin/env python3
"""compare — run the OLD engine and the NEW engine on the same query and show
the results side by side, with metrics.

Usage:
    python3 compare.py <your query>
    python3 compare.py "who won the 2026 super bowl"
    python3 compare.py --content rust vs go performance     # also extract page text
    python3 compare.py --repeat 20 rust vs go performance   # reliability stress test

The OLD engine is the multi-agent system's original single-source DDG scraper.
The NEW engine is ./search.py (multi-provider + RRF fusion + cache + content).
"""

import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# The comparison baseline is the original single-source DDG scraper, bundled in
# this repo so the comparison is reproducible anywhere. Override with the
# BASELINE_PATH env var to compare against a different engine.
OLD_PATH = os.environ.get("BASELINE_PATH", os.path.join(HERE, "baseline_ddg.py"))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _col(rows, width):
    """Pad/truncate each string to exactly `width` for a fixed column."""
    return [(r[:width - 1] + "…") if len(r) > width else r.ljust(width) for r in rows]


def _block(res, label, width):
    """Render one engine's result as a list of fixed-width lines."""
    lines = [label, "─" * width]
    if res.get("error"):
        lines.append(f"⚠ ERROR: {res['error']}")
    rs = res.get("results", [])
    lines.append(f"{len(rs)} results"
                 + (f"  · providers: {','.join(res['providers_used'])}"
                    if res.get("providers_used") else "")
                 + (f"  · cached={res.get('cached')}" if "cached" in res else ""))
    lines.append("")
    for i, r in enumerate(rs, 1):
        lines.append(f"{i}. {r['title']}")
        lines.append(f"   {r['url']}")
        snip = (r.get("snippet") or "").replace("\n", " ")
        lines.append(f"   {snip[:width-6]}" if snip else "   (no snippet)")
        if r.get("content"):
            lines.append(f"   ⤷ content: {len(r['content'])} chars")
        lines.append("")
    return _col(lines, width)


def _pct(x, total):
    return f"{(100.0 * x / total):.0f}%" if total else "—"


def _stats(times):
    if not times:
        return "—"
    s = sorted(times)
    return (f"min={s[0]:.2f}s  med={s[len(s)//2]:.2f}s  max={s[-1]:.2f}s")


def stress(old, new, query, n, repeat):
    """Run the same query `repeat` times against each engine (NEW cache cleared
    every call so it really hits the network) and report how often each one
    returns ZERO results — the failure mode that matters for an agent."""
    print(f"\n  RELIABILITY STRESS TEST · query={query!r} · {repeat} runs each\n")
    rows = [("run", "OLD n", "OLD s", "NEW n", "NEW s")]
    old_zero = new_zero = 0
    old_times, new_times = [], []
    for i in range(1, repeat + 1):
        if old:
            t = time.time(); o = old._search(query, n); ot = time.time() - t
            on = len(o.get("results", []))
        else:
            on, ot = 0, 0.0
        new._cache.clear()
        t = time.time(); ns = new._search(query, n); nt = time.time() - t
        nn = len(ns.get("results", []))
        old_zero += (on == 0); new_zero += (nn == 0)
        old_times.append(ot); new_times.append(nt)
        flag = "  ← OLD empty" if on == 0 and nn > 0 else ""
        rows.append((str(i), str(on), f"{ot:.2f}", str(nn), f"{nt:.2f}"))
        print(f"  {i:>3}.  OLD {on} ({ot:.2f}s)   NEW {nn} ({nt:.2f}s){flag}")
    print("\n  ── summary ──")
    print(f"  zero-result runs   OLD={old_zero}/{repeat} ({_pct(old_zero, repeat)})"
          f"   NEW={new_zero}/{repeat} ({_pct(new_zero, repeat)})")
    print(f"  latency  OLD  {_stats(old_times)}")
    print(f"  latency  NEW  {_stats(new_times)}")
    avail_old = _pct(repeat - old_zero, repeat)
    avail_new = _pct(repeat - new_zero, repeat)
    print(f"  availability       OLD={avail_old}   NEW={avail_new}")
    if old_zero > new_zero:
        print(f"  ➜ NEW answered {old_zero - new_zero} time(s) where OLD returned nothing.")
    elif old_zero == new_zero == 0:
        print("  ➜ both fully available on this run (DDG wasn't throttling).")


def main():
    argv = [a for a in sys.argv[1:] if a != "--content"]
    want_content = "--content" in sys.argv
    repeat = 0
    if "--repeat" in argv:
        idx = argv.index("--repeat")
        try:
            repeat = int(argv[idx + 1])
            del argv[idx:idx + 2]
        except (IndexError, ValueError):
            del argv[idx:idx + 1]
            repeat = 10
    query = " ".join(argv).strip() or "latest claude opus model release"
    n = 5

    new = _load(os.path.join(HERE, "search.py"), "search_new")
    try:
        old = _load(OLD_PATH, "search_old")
    except Exception as e:
        old = None
        old_err = str(e)

    if repeat:
        if not old:
            print(f"could not load old engine: {old_err}")
        stress(old, new, query, n, repeat)
        return

    # OLD
    if old:
        t = time.time()
        old_res = old._search(query, n)
        old_t = time.time() - t
    else:
        old_res, old_t = {"error": f"could not load old engine: {old_err}"}, 0.0

    # NEW (uncached run, so the comparison is fair)
    new._cache.clear()
    t = time.time()
    new_res = new._search(query, n, fetch_content=want_content)
    new_t = time.time() - t

    W = 58
    prov = new_res.get("providers_used", [])
    new_label = "+".join(p.replace("ddg_", "ddg-").replace("searxng", "searx")
                         for p in prov) or "no-providers"
    left = _block(old_res, f"OLD  (ddg-scrape)   {old_t:.2f}s", W)
    right = _block(new_res, f"NEW  ({new_label})  {new_t:.2f}s", W)

    print(f"\n  QUERY: {query!r}"
          + ("   [+content]" if want_content else "") + "\n")
    for l, r in zip(
            left + [""] * (len(right) - len(left)),
            right + [""] * (len(left) - len(right))):
        print(f"{(l or '').ljust(W)} │ {r or ''}")

    # ── verdict ────────────────────────────────────────────────────────────
    on, nn = len(old_res.get("results", [])), len(new_res.get("results", []))
    old_urls = {r["url"] for r in old_res.get("results", [])}
    new_urls = {r["url"] for r in new_res.get("results", [])}
    print("\n  ── metrics ──")
    print(f"  results returned   OLD={on}   NEW={nn}")
    print(f"  latency            OLD={old_t:.2f}s   NEW={new_t:.2f}s")
    print(f"  backends           OLD=1 (duckduckgo)   "
          f"NEW={len(new_res.get('providers_used', []))} "
          f"({','.join(new_res.get('providers_used', [])) or '-'})")
    print(f"  unique urls NEW found that OLD missed: "
          f"{len(new_urls - old_urls)}")
    if on == 0 and nn > 0:
        print("  ➜ OLD returned NOTHING; NEW still answered (fallback win).")


if __name__ == "__main__":
    main()
