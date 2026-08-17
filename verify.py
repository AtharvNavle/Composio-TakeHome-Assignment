"""Tier 1 lints + independent quote verification (HTTP tier, then browser tier).

Quote checks re-fetch the cited URL independently. The research agent's WebFetch output is a
model-written summary, not source text, and is never used here.

Routing heuristic (NOT a judgement about the claim): a page whose extracted text is tiny, or
tiny relative to its HTML, is one this fetcher cannot read -- almost always a client-rendered
SPA. Those citations are routed to the browser tier for a real check. Routing says "this tier
cannot verify", never "the quote is wrong".
"""
import argparse, json, re, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
RAW = ROOT / "data" / "raw"
BROWSER_DIR = ROOT / "data" / "browser"
BROWSER_RESULTS = ROOT / "data" / "browser_results.json"

THIN_CHARS = 500      # below this, nothing useful was extracted
THIN_RATIO = 0.05     # text/html below this = SPA shell (server-rendered docs run 0.05-0.30)
PARTIAL_MIN = 40      # min fragment length before a partial match counts

ENUMS = {
    "access_model": {"Free Self-Serve", "Trial Self-Serve", "Paid Self-Serve",
                     "Admin Approval", "Contact Sales", "Partner / Application Required",
                     "No Public API", "Unknown"},
    "auth": {"OAuth2", "API Key", "Basic Auth", "Bearer Token", "Bot Token", "JWT", "HMAC", "Other", "Unknown"},
    "api_surface.protocols": {"REST", "GraphQL", "SOAP", "WebSocket", "CLI", "SDK", "Other", "Unknown"},
    "mcp.status": {"Official MCP", "Vendor-supported MCP", "Community MCP", "No MCP Found", "Unknown"},
    "mcp.evidence_type": {"Official vendor documentation", "Official repository", "Third-party repository", "Other", "None"},
    "buildability.verdict": {"Ready", "Limited", "Blocked", "Unknown"},
    "evidence.source_type": {"Official Docs", "Official Pricing", "Official Developer Portal", "Official Repository", "Third Party", "Other"},
    "confidence.overall": {"High", "Medium", "Low"}
}

NEEDS_EVIDENCE = ["auth", "access_model", "mcp", "api_surface", "buildability"]
SELF_SERVE = {"Free Self-Serve", "Trial Self-Serve"}
GATED = {"Admin Approval", "Contact Sales", "Partner / Application Required", "No Public API"}
GROUNDED = {"quote_found", "quote_found_partial"}



def norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def host(url):
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1).lower().removeprefix("www.") if m else ""


def extract_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return norm(soup.get_text(" "))


def needs_browser(text_len, html_len):
    """Routing heuristic. True = this tier cannot read the page; check it in a browser."""
    if text_len < THIN_CHARS:
        return True
    return html_len > 0 and (text_len / html_len) < THIN_RATIO


def match_quote(quote, page):
    q = norm(quote)
    if not q:
        return "quote_not_found"
    if q in page:
        return "quote_found"
    frag = max(re.split(r"[.;\n]", quote or ""), key=len, default="").strip()
    if len(norm(frag)) > PARTIAL_MIN and norm(frag) in page:
        return "quote_found_partial"
    return "quote_not_found"


def lint(doc):
    """-> list of (severity, message). 'hard' means re-run the app."""
    out = []
    rec = doc.get("record")
    if not rec:
        return [("hard", "no valid JSON record")]

    fetched = set(doc["provenance"]["fetched_urls"])
    ev = rec.get("evidence") or []

    for e in ev:
        if e.get("url") not in fetched:
            out.append(("hard", f"UNFETCHED CITATION {e.get('field')} -> {e.get('url')}"))
        if e.get("source_type") not in ENUMS["evidence.source_type"]:
            out.append(("hard", f"bad enum evidence.source_type={e.get('source_type')!r}"))

    def get_val(key):
        d = rec
        for p in key.split('.'):
            if not isinstance(d, dict): return None
            d = d.get(p)
        return d

    for f, allowed in ENUMS.items():
        if f == "evidence.source_type": continue
        v = get_val(f)
        if isinstance(v, list):
            for item in v:
                if item not in allowed:
                    out.append(("hard", f"bad enum in {f}={item!r}"))
        elif v is not None and v not in allowed:
            out.append(("hard", f"bad enum {f}={v!r}"))

    cited = {e.get("field") for e in ev}
    for f in NEEDS_EVIDENCE:
        if f == "auth":
            v = rec.get("auth")
            if v == ["Unknown"] or not v: continue
        elif f == "api_surface":
            if rec.get("api_surface", {}).get("protocols") == ["Unknown"]: continue
        else:
            v = get_val(f)
            if v in ("Unknown", "No MCP Found") or (isinstance(v, dict) and get_val(f + ".status") == "Unknown"):
                continue
        if f not in cited and not any(c.startswith(f) for c in cited if isinstance(c, str)):
            out.append(("hard", f"no evidence for {f}"))

    b = get_val("buildability.verdict")
    am = rec.get("access_model")
    if b == "Ready" and am in GATED:
        out.append(("hard", f"incoherent: buildability={b} but access_model={am}"))
    if am == "Unknown" and b not in (None, "Unknown", "Blocked"):
        out.append(("hard", f"buildability={b} asserted while access_model=Unknown"))

    if am in SELF_SERVE:
        ok = [e for e in ev if e.get("field") == "access_model" and e.get("url") in fetched]
        if not ok:
            out.append(("hard", f"access_model={am} without fetched evidence"))

    mcp_stat = get_val("mcp.status")
    if mcp_stat in ("Official MCP", "Vendor-supported MCP"):
        vend = host(doc["app"]["hint_url"])
        base = vend.split(".")[-2] if vend.count(".") >= 1 else vend
        ok = [e for e in ev if "mcp" in str(e.get("field")) and e.get("url") in fetched
              and (base in host(e["url"]) or "github.com" in host(e["url"]))]
        if not ok:
            out.append(("hard", f"mcp.status={mcp_stat} without fetched vendor-owned evidence"))

    return out


def check_quotes_http(doc, client):
    """HTTP tier. -> list of per-citation dicts."""
    results = []
    for i, e in enumerate((doc.get("record", {}) or {}).get("evidence", []) or []):
        url, quote = e.get("url"), e.get("quote", "")
        r = {"app": doc["app"]["id"], "idx": i, "field": e.get("field"),
             "url": url, "quote": quote}
        try:
            resp = client.get(url, follow_redirects=True, timeout=25,
                              headers={"User-Agent": "Mozilla/5.0 (compatible; composio-research)"})
            r["status"] = resp.status_code
            if resp.status_code >= 400:
                r["verdict"] = "url_dead"
            else:
                page = extract_text(resp.text)
                r["text_chars"], r["html_bytes"] = len(page), len(resp.text)
                r["ratio"] = round(len(page) / max(len(resp.text), 1), 4)
                r["verdict"] = ("route_to_browser_tier"
                                if needs_browser(len(page), len(resp.text))
                                else match_quote(quote, page))
        except Exception as ex:
            r["status"], r["verdict"], r["error"] = None, "route_to_browser_tier", type(ex).__name__
        results.append(r)
    return results


def browser_verify(pending, workers=4):
    """Playwright tier, sharded across `workers` independent browsers.

    Sync Playwright objects are not thread-safe, so each worker owns its own
    sync_playwright()/browser rather than sharing contexts. Artifacts are keyed by
    app+idx+field, so shards never collide and results are re-sorted before returning --
    output is identical regardless of worker count.
    """
    BROWSER_DIR.mkdir(parents=True, exist_ok=True)
    shards = [pending[i::workers] for i in range(workers)]
    out = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for chunk in ex.map(_browser_worker, [s for s in shards if s]):
            out.extend(chunk)
    return sorted(out, key=lambda r: (r["app"], r["idx"]))


def _browser_worker(pending):
    from playwright.sync_api import sync_playwright
    out = []
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        for c in pending:
            r = {k: c[k] for k in ("app", "idx", "field", "url", "quote")}
            try:
                pg = b.new_page(viewport={"width": 1400, "height": 1000})
                pg.goto(c["url"], timeout=45000, wait_until="domcontentloaded")
                pg.wait_for_timeout(3500)  # let client-side render settle
                try:
                    pg.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                text = norm(pg.inner_text("body"))
                stem = f"{c['app']}__{c['idx']}_{c['field']}"
                (BROWSER_DIR / f"{stem}.txt").write_text(text, encoding="utf-8")
                pg.screenshot(path=str(BROWSER_DIR / f"{stem}.png"), full_page=True)
                r.update(rendered_chars=len(text), verdict=match_quote(c["quote"], text),
                         artifact=stem)
                pg.close()
            except Exception as ex:
                r.update(verdict="browser_error", error=f"{type(ex).__name__}: {str(ex)[:120]}")
            out.append(r)
            print(f"  BROWSER {r['verdict']:<20} {r['field']:<14} {r['url'][:60]}", flush=True)
        b.close()
    return out


def demo():
    """Self-checks for lint(), the routing heuristic, and quote matching."""
    def doc(rec, fetched):
        return {"app": {"id": "t", "hint_url": "https://vendor.com"},
                "record": rec, "provenance": {"fetched_urls": fetched}}
    hard = lambda d: [m for s, m in lint(d) if s == "hard"]

    ok = {"auth_methods": ["api_key"], "access_model": "free_self_serve",
          "mcp_status": "none_found", "buildability": "buildable_now",
          "evidence": [{"claim_field": "auth_methods", "url": "https://vendor.com/a", "source_tier": 1},
                       {"claim_field": "access_model", "url": "https://vendor.com/p", "source_tier": 1}]}
    urls = ["https://vendor.com/a", "https://vendor.com/p"]
    assert hard(doc(ok, urls)) == [], hard(doc(ok, urls))
    assert any("UNFETCHED" in m for m in hard(doc(ok, ["https://vendor.com/a"])))
    bad = {**ok, "evidence": [{"claim_field": "auth_methods", "url": "https://vendor.com/a", "source_tier": 1},
                              {"claim_field": "access_model", "url": "https://vendor.com/p", "source_tier": 5}]}
    assert any("without fetched tier" in m for m in hard(doc(bad, urls)))
    unk = {"auth_methods": ["unknown"], "access_model": "unknown", "mcp_status": "none_found",
           "buildability": "unknown", "evidence": [], "unknowns": [{"field": "access_model", "reason": "x"}]}
    assert hard(doc(unk, [])) == [], hard(doc(unk, []))
    assert any("incoherent" in m for m in hard(doc({**ok, "access_model": "partnership_contact_sales",
                                                   "buildability": "buildable_now"}, urls)))
    # access_model unknown must not carry a confident buildability (the fanbasis warning)
    assert any("access_model=unknown" in m
               for m in hard(doc({**unk, "buildability": "blocked"}, [])))

    # routing heuristic
    assert needs_browser(120, 5000) is True            # too little text
    assert needs_browser(4674, 201880) is True         # SPA shell: ratio 0.023
    assert needs_browser(11733, 120000) is False       # server-rendered doc page
    assert needs_browser(600, 1000) is False           # small but dense page
    assert needs_browser(0, 0) is True

    # quote matching
    page = norm("Click the User icon at the top-right of your screen and select Profile. "
                "Scroll down to the API Key section.")
    assert match_quote("Click the User icon at the top-right of your screen and select Profile.",
                       page) == "quote_found"
    assert match_quote("totally absent sentence that is nowhere on this page", page) == "quote_not_found"
    # synthesized quote: one real span + an invented tail -> partial, not full credit
    assert match_quote("Click the User icon at the top-right of your screen and select Profile. "
                       "AND AN INVENTED TAIL", page) == "quote_found_partial"
    # a real but SHORT fragment (<PARTIAL_MIN) earns no credit -- weak evidence stays ungrounded
    assert match_quote("select Profile. INVENTED", page) == "quote_not_found"
    assert match_quote("", page) == "quote_not_found"
    print("self-check: PASS (lint, routing heuristic, quote matching)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["lint", "quotes", "browser", "report", "demo"])
    a = ap.parse_args()
    if a.cmd == "demo":
        return demo()

    docs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(RAW.glob("*.json"))]
    if not docs:
        sys.exit("no records in data/raw")

    tot_ev = tot_fetched = hard_total = warn_total = 0
    http_results = []
    for d in docs:
        rec = d.get("record") or {}
        ev = rec.get("evidence") or []
        fetched = set(d["provenance"]["fetched_urls"])
        tot_ev += len(ev)
        tot_fetched += sum(1 for e in ev if e.get("url") in fetched)

        print(f"\n=== {d['app']['id']} ===")
        print(f"  json_valid={d['json_valid']}  citations={len(ev)}  "
              f"cost=${d['meta'].get('total_cost_usd',0):.3f}  wall={d['meta'].get('wall_s')}s")
        if rec:
            print(f"  access_model={rec.get('access_model')}  mcp={rec.get('mcp_status')}  "
                  f"build={rec.get('buildability')}  conf={rec.get('overall_confidence')}")
        issues = lint(d)
        hard_total += sum(1 for s, _ in issues if s == "hard")
        warn_total += sum(1 for s, _ in issues if s == "warn")
        for s, m in issues:
            print(f"  [{s.upper()}] {m}")
        if not issues:
            print("  [OK] no lint findings")

    if a.cmd in ("quotes", "report", "browser"):
        print(f"\n--- HTTP TIER: {tot_ev} citations ---")
        with httpx.Client() as client:
            with ThreadPoolExecutor(max_workers=8) as ex:
                for chunk in ex.map(lambda d: check_quotes_http(d, client), docs):
                    http_results.extend(chunk)
        http_results.sort(key=lambda r: (r["app"], r["idx"]))
        routed = sum(1 for r in http_results if r["verdict"] == "route_to_browser_tier")
        print(f"  readable by httpx: {len(http_results)-routed}   routed to browser: {routed}")

    browser_results = []
    pending = [r for r in http_results if r["verdict"] == "route_to_browser_tier"]
    if a.cmd in ("browser", "report") and pending:
        print(f"\n--- BROWSER TIER: {len(pending)} routed citations ---")
        browser_results = browser_verify(pending)
        BROWSER_RESULTS.write_text(json.dumps(browser_results, indent=2), encoding="utf-8")

    # ---- metrics, denominators spelled out ----
    http_checkable = [r for r in http_results if r["verdict"] != "route_to_browser_tier"]
    http_grounded = [r for r in http_checkable if r["verdict"] in GROUNDED]
    b_grounded = [r for r in browser_results if r["verdict"] in GROUNDED]
    b_errors = [r for r in browser_results if r["verdict"] == "browser_error"]
    b_checked = [r for r in browser_results if r["verdict"] != "browser_error"]

    pct = lambda n, d_: f"{(n/d_*100):.1f}%" if d_ else "n/a"
    print(f"\n{'='*72}")
    print(f"PROVENANCE           : {tot_fetched}/{tot_ev} = {pct(tot_fetched,tot_ev)}"
          f"   [denom: all citations]")
    print(f"JSON VALIDITY        : {sum(d['json_valid'] for d in docs)}/{len(docs)}")
    print(f"HARD FAILURES        : {hard_total}   WARNINGS: {warn_total}")
    print(f"HTTP-VERIFIABLE      : {len(http_grounded)}/{len(http_checkable)} = "
          f"{pct(len(http_grounded),len(http_checkable))}"
          f"   [denom: citations httpx could read]")
    print(f"BROWSER-ROUTED       : {len(pending)}   [citations httpx could NOT read]")
    print(f"BROWSER GROUNDING    : {len(b_grounded)}/{len(b_checked)} = "
          f"{pct(len(b_grounded),len(b_checked))}   [denom: browser-routed minus errors]")
    print(f"BROWSER ERRORS       : {len(b_errors)}")
    combined_n = len(http_grounded) + len(b_grounded)
    print(f"COMBINED (strict)    : {combined_n}/{tot_ev} = {pct(combined_n,tot_ev)}"
          f"   [denom: ALL citations; browser errors count as NOT grounded]")
    checked = len(http_checkable) + len(b_checked)
    print(f"COMBINED (checked)   : {combined_n}/{checked} = {pct(combined_n,checked)}"
          f"   [denom: citations actually checked by either tier]")
    if a.cmd in ("quotes", "lint"):
        print("\n(browser tier not run in this mode -- use `report` or `browser`)")


if __name__ == "__main__":
    main()
