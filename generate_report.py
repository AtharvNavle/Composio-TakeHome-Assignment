"""Generate a standalone report.html from research_results.json + template.html.

Priority: research_results.json (complete 100-app flat dataset) > data/raw/ (per-app pipeline output).
"""
import json, re
from pathlib import Path

ROOT = Path(__file__).parent

def main():
    # --- Load data ---
    results_path = ROOT / "research_results.json"
    raw_dir = ROOT / "data" / "raw"

    apps_data = []

    # Primary: research_results.json (flat format, matches template directly)
    if results_path.exists():
        with open(results_path, "r", encoding="utf-8") as f:
            apps_data = json.load(f)
        print(f"Loaded {len(apps_data)} apps from research_results.json")
    else:
        print("research_results.json not found, checking data/raw/...")
        # Fallback: data/raw/ per-app JSON files
        for f in sorted(raw_dir.glob("*.json")):
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                if isinstance(data, dict) and data.get("json_valid") and data.get("record"):
                    record = data["record"]
                    if isinstance(record, dict):
                        apps_data.append(record)
            except Exception as e:
                print(f"  Skipping {f.name}: {e}")

    if not apps_data:
        print("ERROR: No data found to generate report.")
        return

    # Sort by ID
    apps_data.sort(key=lambda x: x.get("id", 0))

    # --- Load template ---
    template_path = ROOT / "template.html"
    if not template_path.exists():
        print("ERROR: template.html not found.")
        return

    html = template_path.read_text(encoding="utf-8")

    # --- Replace inline data ---
    json_str = json.dumps(apps_data, indent=2)
    html = re.sub(
        r'const INLINE_APPS_DATA\s*=\s*\[.*?\];',
        lambda m: f'const INLINE_APPS_DATA = {json_str};',
        html,
        flags=re.DOTALL
    )

    # --- Write output ---
    out_path = ROOT / "report.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"Generated {out_path} with {len(apps_data)} apps.")

if __name__ == "__main__":
    main()
