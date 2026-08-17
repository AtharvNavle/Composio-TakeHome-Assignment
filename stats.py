"""Run-report statistics over data/raw + data/browser_results.json.

Descriptive only -- no scoring, no verdict changes. Feeds the post-run report.
"""
import collections, csv, json, sys
from pathlib import Path

ROOT = Path(__file__).parent
RAW = ROOT / "data" / "raw"
GROUNDED = {"quote_found", "quote_found_partial"}


def load():
    with open(ROOT / "apps.json", encoding="utf-8") as f:
        apps_list = json.load(f)
    apps = {str(r["id"]): r for r in apps_list}
    docs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(RAW.glob("*.json"))]
    return apps, docs


def bar(counter, total, width=34):
    for k, v in counter.most_common():
        print(f"    {str(k):<32} {v:>4}  {v/total*100:5.1f}%  {'#' * int(v/total*width)}")


def main():
    apps, docs = load()
    n = len(docs)
    if not n:
        sys.exit("no records")
    ok = [d for d in docs if d.get("record")]
    recs = [d["record"] for d in ok]

    print(f"{'='*72}\nDATASET: {n}/100 records, {len(ok)} with valid JSON\n{'='*72}")

    # cost / time / rate limit
    cost = sum(d["meta"].get("total_cost_usd") or 0 for d in docs)
    wall = [d["meta"].get("wall_s") or 0 for d in docs]
    rls = [d["meta"].get("rate_limit") for d in docs if d["meta"].get("rate_limit")]
    print(f"\ncost: ${cost:.2f} total | ${cost/n:.3f}/app")
    print(f"research wall: mean {sum(wall)/n:.1f}s | max {max(wall):.1f}s | sum {sum(wall)/60:.1f} min")
    if rls:
        st = collections.Counter(r.get("status") for r in rls)
        print(f"rate-limit statuses seen: {dict(st)} | type={rls[-1].get('rateLimitType')} "
              f"| overage={rls[-1].get('overageStatus')}")
    else:
        print("rate-limit: no rate_limit_event emitted")

    # provenance
    tot_ev = tot_fetch = 0
    for d in ok:
        f = set(d["provenance"]["fetched_urls"])
        ev = d["record"].get("evidence") or []
        tot_ev += len(ev)
        tot_fetch += sum(1 for e in ev if e.get("url") in f)
    print(f"\nprovenance: {tot_fetch}/{tot_ev} = {tot_fetch/max(tot_ev,1)*100:.1f}% "
          f"[denom: all citations]")
    print(f"citations/app: {tot_ev/max(len(ok),1):.1f}")

    def get_val(rec, key):
        d = rec
        for p in key.split('.'):
            if not isinstance(d, dict): return None
            d = d.get(p)
        return d

    # unknown rate
    fields = [("auth", lambda r: get_val(r, "auth") == ["Unknown"] or not get_val(r, "auth")),
              ("access_model", lambda r: get_val(r, "access_model") in ("Unknown", None)),
              ("api_surface.protocols", lambda r: get_val(r, "api_surface.protocols") == ["Unknown"]),
              ("mcp.status", lambda r: get_val(r, "mcp.status") in ("Unknown", None)),
              ("buildability.verdict", lambda r: get_val(r, "buildability.verdict") in ("Unknown", None))]
    
    print(f"\n--- UNKNOWN RATE (per field, denom={len(recs)} valid records) ---")
    for fname, cond in fields:
        u = sum(1 for r in recs if cond(r))
        print(f"    {fname:<25} {u:>4} / {len(recs)}  {u/len(recs)*100:5.1f}%")
        
    fully = sum(1 for r in recs if get_val(r, "access_model") == "Unknown"
                and get_val(r, "buildability.verdict") == "Unknown")
    print(f"    {'apps unknown on both access+buildability':<40} {fully}")

    for field in ["buildability.verdict", "access_model", "mcp.status", "confidence.overall"]:
        c = collections.Counter(get_val(r, field) for r in recs)
        print(f"\n--- {field.upper()} (denom={len(recs)}) ---")
        bar(c, len(recs))

    # auth is multi-label
    auth = collections.Counter()
    for r in recs:
        for a in (get_val(r, "auth") or ["Unknown"]):
            auth[a] += 1
    print(f"\n--- AUTH METHODS (multi-label; {sum(auth.values())} labels over {len(recs)} apps) ---")
    bar(auth, len(recs))

    proto = collections.Counter()
    for r in recs:
        for a in (get_val(r, "api_surface.protocols") or ["Unknown"]):
            proto[a] += 1
    print(f"\n--- API PROTOCOLS (multi-label) ---")
    bar(proto, len(recs))

    # category x access / buildability
    SELF = {"Free Self-Serve", "Trial Self-Serve", "Paid Self-Serve"}
    PAID = {"Paid Self-Serve"}
    print(f"\n--- CATEGORY x ACCESS (self-serve / paid / gated / unknown) ---")
    print(f"    {'category':<36} {'self':>5} {'paid':>5} {'gated':>6} {'unk':>5}")
    for cat in dict.fromkeys(a["category"] for a in apps.values()):
        rs = [d["record"] for d in ok if apps[str(d["app"]["id"])]["category"] == cat]
        s = sum(1 for r in rs if get_val(r, "access_model") in SELF)
        p = sum(1 for r in rs if get_val(r, "access_model") in PAID)
        u = sum(1 for r in rs if get_val(r, "access_model") == "Unknown")
        print(f"    {cat:<36} {s:>5} {p:>5} {len(rs)-s-u:>6} {u:>5}")

    print(f"\n--- CATEGORY x BUILDABILITY (ready / limited / blocked / unknown) ---")
    print(f"    {'category':<36} {'rdy':>5} {'lim':>5} {'blk':>5} {'unk':>5}")
    for cat in dict.fromkeys(a["category"] for a in apps.values()):
        rs = [d["record"] for d in ok if apps[str(d["app"]["id"])]["category"] == cat]
        c = collections.Counter(get_val(r, "buildability.verdict") for r in rs)
        print(f"    {cat:<36} {c['Ready']:>5} {c['Limited']:>5} "
              f"{c['Blocked']:>5} {c['Unknown']:>5}")

    # quote grounding, if browser tier has run
    br = ROOT / "data" / "browser_results.json"
    if br.exists():
        b = json.loads(br.read_text(encoding="utf-8"))
        g = sum(1 for r in b if r["verdict"] in GROUNDED)
        err = sum(1 for r in b if r["verdict"] == "browser_error")
        print(f"\n--- BROWSER TIER ---")
        print(f"    routed/checked : {len(b)}   errors: {err}")
        print(f"    grounded       : {g}/{len(b)-err} = "
              f"{g/max(len(b)-err,1)*100:.1f}%  [denom: routed minus errors]")


if __name__ == "__main__":
    main()
