"""Research agent: one `claude -p` subprocess per app, prompt via stdin.

Provenance rule: only URLs the agent actually opened with WebFetch count as citable.
WebSearch result URLs do NOT count -- that distinction is the whole point.
"""
import argparse, csv, hashlib, json, re, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).parent
PROMPT = ROOT / "prompts" / "research.md"
SCHEMA = ROOT / "schema.json"
RAW, LOGS = ROOT / "data" / "raw", ROOT / "data" / "logs"
MODEL = "claude-sonnet-4-20250514"
CLAUDE = shutil.which("claude") or "claude"


def prompt_for(app):
    tpl = PROMPT.read_text(encoding="utf-8")
    schema = SCHEMA.read_text(encoding="utf-8")
    app_str = json.dumps(app, indent=2)
    full = f"{tpl}\n\n=== APP TO RESEARCH ===\n{app_str}\n\n=== OUTPUT SCHEMA ===\n{schema}"
    return full, hashlib.sha256(full.encode()).hexdigest()[:12]


def parse_stream(text):
    """->(fetched_urls, searched_urls, result_text, meta). Tolerates non-JSON lines."""
    fetched, searched, result, meta = [], [], "", {}
    rl = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") == "assistant":
            for b in d.get("message", {}).get("content", []):
                if b.get("type") == "tool_use":
                    inp = b.get("input", {})
                    if b.get("name") == "WebFetch" and inp.get("url"):
                        fetched.append(inp["url"])
                    elif b.get("name") == "WebSearch" and inp.get("query"):
                        searched.append(inp["query"])
        elif d.get("type") == "rate_limit_event":
            rl = d.get("rate_limit_info")
        elif d.get("type") == "result":
            result = d.get("result", "")
            meta = {k: d.get(k) for k in
                    ("total_cost_usd", "duration_ms", "num_turns", "stop_reason", "is_error")}
    meta["rate_limit"] = rl
    return fetched, searched, result, meta


def extract_json(text):
    """Model sometimes wraps in fences despite instructions. Strip and take outermost object."""
    s = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def research(app, run_id):
    prompt, phash = prompt_for(app)
    t0 = time.time()
    p = subprocess.run(
        [CLAUDE, "-p", "--model", MODEL, "--allowed-tools", "WebSearch,WebFetch",
         "--output-format", "stream-json", "--verbose"],
        input=prompt, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900,
    )
    wall = round(time.time() - t0, 1)
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / f"{app['id']}.jsonl").write_text(p.stdout, encoding="utf-8")

    fetched, searched, result_text, meta = parse_stream(p.stdout)
    record = extract_json(result_text)
    out = {
        "app": app,
        "record": record,
        "json_valid": record is not None,
        "provenance": {"fetched_urls": fetched, "search_queries": searched},
        "meta": {**meta, "wall_s": wall, "model": MODEL, "backend": "claude_cli_stdin",
                 "prompt_version": "v2_json", "prompt_sha256_12": phash, "run_id": run_id,
                 "exit_code": p.returncode},
    }
    if record is None:
        out["raw_result_text"] = result_text[:2000]
    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / f"{app['id']}.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"  {app['id']:<12} json={'ok ' if record else 'FAIL'} "
          f"fetched={len(fetched)} searched={len(searched)} "
          f"${meta.get('total_cost_usd', 0):.3f} {wall}s", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apps", default="apps.json")
    ap.add_argument("--only", help="comma-separated ids")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    with open(ROOT / a.apps, encoding="utf-8") as f:
        apps = json.load(f)
    if a.only:
        want = set(a.only.split(","))
        apps = [x for x in apps if str(x["id"]) in want or x["name"].lower() in want]
    def is_done(app_id):
        p = RAW / f"{app_id}.json"
        if not p.exists():
            return False
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("json_valid", False)
        except (json.JSONDecodeError, OSError):
            return False  # corrupt/partial file -> redo it

    if not a.force:
        apps = [x for x in apps if not is_done(x["id"])]
    if not apps:
        print("nothing to do (use --force to re-run)")
        return

    run_id = f"run-{int(time.time())}"
    print(f"{run_id}  model={MODEL}  apps={len(apps)}  concurrency={a.concurrency}")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        list(ex.map(lambda x: research(x, run_id), apps))
    print(f"done in {round(time.time()-t0,1)}s")


if __name__ == "__main__":
    main()
