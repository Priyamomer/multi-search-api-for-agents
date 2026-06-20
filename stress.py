#!/usr/bin/env python3
"""stress — high-volume reliability probe.

Runs N cache-bypassed searches and records, per run:
  - whether the fused NEW engine returned anything (availability)
  - which providers returned >0 results (per-provider survival)
  - whether the OLD single-source engine returned anything
  - latency

Reports availability, per-provider zero-rates, and latency percentiles, plus a
running log so you can watch engines degrade under load in real time.

Usage:  python3 stress.py [N] [query...]
        python3 stress.py 1000 rust vs go performance
"""
import importlib.util, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
OLD_PATH = os.path.expanduser(
    "~/Desktop/CODES/multi-agent-adpative-sync-system/core/tools/web_search.py")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def pct(x, t):
    return f"{100.0*x/t:.1f}%" if t else "—"


def percentiles(xs):
    if not xs:
        return "—"
    s = sorted(xs)
    g = lambda p: s[min(len(s)-1, int(p*len(s)))]
    return f"p50={g(.5):.2f}s p90={g(.9):.2f}s p99={g(.99):.2f}s max={s[-1]:.2f}s"


def main():
    args = sys.argv[1:]
    N = 1000
    if args and args[0].isdigit():
        N = int(args[0]); args = args[1:]
    query = " ".join(args) or "rust vs go performance"

    new = _load(os.path.join(HERE, "search.py"), "search_new")
    try:
        old = _load(OLD_PATH, "search_old")
    except Exception:
        old = None

    providers = {p.__name__.replace("_p_", ""): p for p in new._PROVIDERS}
    prov_zero = {name: 0 for name in providers}
    prov_seen = {name: 0 for name in providers}  # times we actually attempted it
    new_zero = old_zero = 0
    lat_new, lat_old = [], []
    t0 = time.time()

    print(f"stress: N={N}  query={query!r}  providers={list(providers)}\n")
    for i in range(1, N + 1):
        # Call each provider once; NEW is "up" if ANY provider returned >0.
        # This is exactly what the fused engine depends on, without the double
        # network hit of also calling _search().
        t = time.time()
        any_hit = False
        for name, fn in providers.items():
            try:
                hits = len(fn(query, 8))
            except Exception:
                hits = 0
            prov_seen[name] += 1
            if hits == 0:
                prov_zero[name] += 1
            else:
                any_hit = True
        lat_new.append(time.time() - t)
        if not any_hit:
            new_zero += 1

        # OLD engine
        if old:
            t = time.time(); o = old._search(query, 5); ot = time.time() - t
            lat_old.append(ot)
            if not o.get("results"):
                old_zero += 1

        if i % 25 == 0 or i == N:
            alive = ",".join(f"{n}={pct(prov_seen[n]-prov_zero[n], prov_seen[n])}"
                             for n in providers)
            print(f"  {i:>4}/{N}  NEW_up={pct(i-new_zero,i)}  "
                  f"OLD_up={pct(i-old_zero,i)}  | per-provider up: {alive}")

    el = time.time() - t0
    print("\n── SUMMARY ──")
    print(f"runs: {N}   elapsed: {el:.0f}s   ({N/el:.1f} runs/s)")
    print(f"NEW availability:  {pct(N-new_zero, N)}   ({N-new_zero}/{N} answered)")
    print(f"OLD availability:  {pct(N-old_zero, N)}   ({N-old_zero}/{N} answered)")
    print("per-provider availability (returned >0):")
    for n in providers:
        print(f"   {n:10} {pct(prov_seen[n]-prov_zero[n], prov_seen[n])}")
    print(f"NEW latency: {percentiles(lat_new)}")
    print(f"OLD latency: {percentiles(lat_old)}")


if __name__ == "__main__":
    main()
