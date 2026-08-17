# Composio Take-Home: 100 Apps Strategic Feasibility

Automated research pipeline that evaluates **100 SaaS applications** for AI agent toolkit integration — covering auth methods, API surfaces, self-serve access, MCP server availability, and buildability.

## Results

Open **[`report.html`](report.html)** in any browser. Fully self-contained, no server needed.

| Metric | Count |
|--------|-------|
| Total Apps Evaluated | **100** |
| Ready (buildable now) | **57** |
| Limited (needs workaround) | **22** |
| Blocked (no viable path) | **21** |
| Has MCP Server | **42** |
| Free/Trial Self-Serve | **80** |

## Pipeline

```
research.py → verify.py → stats.py → generate_report.py → report.html
```

| Script | Purpose |
|--------|---------|
| `research.py` | Runs Claude CLI against each app with web search to gather structured API/auth data |
| `verify.py` | Schema linting + independent quote verification (HTTP + Playwright browser tiers) |
| `stats.py` | Aggregates metrics across all apps |
| `generate_report.py` | Embeds data into `template.html` to produce the final `report.html` |

## How to Run

```bash
# Research (requires Claude CLI)
python research.py

# Validate output
python verify.py lint

# Full verification with browser-based quote checking
python verify.py report

# Stats dashboard
python stats.py

# Regenerate HTML report
python generate_report.py
```

## Project Structure

```
├── report.html              ← Final deliverable
├── research_results.json    ← Complete 100-app dataset
├── apps.json                ← Input app definitions
├── schema.json              ← Output JSON schema
├── prompts/research.md      ← Research prompt template
└── data/
    ├── raw/                 ← Per-app research JSON
    ├── logs/                ← Claude session logs
    ├── browser/             ← Browser verification artifacts
    └── browser_results.json ← Quote verification results
```

## Verification Approach

Each app's research output goes through two independent verification tiers:

1. **HTTP Tier** — `httpx` fetches cited URLs, searches for quoted text in rendered HTML
2. **Browser Tier** — Playwright renders JS-heavy/SPA pages that HTTP couldn't read, then verifies quotes

This ensures claims like "OAuth2 required" or "free tier available" are backed by actual documentation, not hallucinated.
