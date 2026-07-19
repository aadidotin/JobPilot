#!/usr/bin/env bash
# Curation-time slug verifier: fetches every board in config/companies.yaml
# and prints a red/green line per company. Run this from a residential IP
# (laptop) — datacenter IPs hit Cloudflare 1020 on Greenhouse.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run python - <<'EOF'
import json
import sys
import urllib.request

import yaml

URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}

with open("config/companies.yaml") as f:
    companies = (yaml.safe_load(f) or {}).get("companies") or []

if not companies:
    print("companies.yaml is empty — add your 30 companies first.")
    sys.exit(1)

failures = 0
for c in companies:
    name, ats, slug = c["name"], c["ats"], c["slug"]
    url = URLS[ats].format(slug=slug)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        n = len(data.get("jobs", data) if isinstance(data, dict) else data)
        print(f"  OK   {name:<30} {ats}/{slug}  ({n} postings)")
    except Exception as e:
        failures += 1
        print(f"  FAIL {name:<30} {ats}/{slug}  {e}")

print(f"\n{len(companies) - failures}/{len(companies)} boards verified.")
sys.exit(1 if failures else 0)
EOF
