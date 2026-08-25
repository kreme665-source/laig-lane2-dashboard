#!/usr/bin/env python3
# refresh_market.py — builds the "Where the Money Flows" market-view block.
#
# Output: rewrites the MARKET_DATA_START .. MARKET_DATA_END block inside
# index.html so the dashboard renders in-lane obligations without a runtime
# fetch. The block is a plain JS const with rows pre-sorted and pre-truncated;
# the page just paints it.
#
# Data: USASpending spending_by_category. One call returns per-agency totals
# in ONE response, so the whole section costs:
#
#   1. FY-to-date, in-lane, contracts          -> obligations + rank
#   2. same, set_aside_type_codes ["NONE"]     -> non-set-aside subset
#   3. same period LAST fiscal year            -> FY-over-FY
#
# DHS is excluded (agency relationship, never sourced nor shown).

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

API_URL = "https://api.usaspending.gov/api/v2/search/spending_by_category"

LOCAL_TZ = ZoneInfo("America/Chicago")


def local_today():
    return datetime.now(LOCAL_TZ).date()


NAICS_PREFIXES = ["5413", "5415", "5416"]
CONTRACT_TYPES = ["A", "B", "C", "D"]
IDV_TYPES = ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C",
             "IDV_C", "IDV_D", "IDV_E"]
AWARD_TYPES = CONTRACT_TYPES + IDV_TYPES

TOP_N = 8                 # agencies shown, after exclusions
CATEGORY_LIMIT = 25       # pull deeper than TOP_N so exclusions cannot short it
REQUEST_PAUSE = 0.3

# ── Conflict screen — keep in sync with the other feeds ──────────────────────
EXCLUDED_AGENCY_KEYWORDS = [
    "DHS", "HOMELAND SECURITY", "HOMELAND",
    "COAST GUARD", "USCG", "SFLC",
    "TRANSPORTATION SECURITY", "TSA",
    "SECRET SERVICE", "USSS",
    "CUSTOMS AND BORDER", "CBP",
    "IMMIGRATION AND CUSTOMS", "USCIS",
    "FEDERAL EMERGENCY MANAGEMENT", "FEMA",
    "CYBERSECURITY AND INFRASTRUCTURE", "CISA",
    "FEDERAL LAW ENFORCEMENT TRAINING", "FLETC",
]

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.normpath(os.path.join(HERE, "..", "index.html"))

MARKET_RE = re.compile(
    r"(/\* MARKET_DATA_START \*/\n).*?(\n  /\* MARKET_DATA_END \*/\)", re.S)


def fiscal_year_start(d):
    """US federal FY starts Oct 1 of the previous calendar year."""
    return date(d.year if d.month >= 10 else d.year - 1, 10, 1)


def post(body):
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:800]
        raise SystemExit(f"USASpending HTTP {e.code}: {detail}")
    except Exception as e:
        raise SystemExit(f"USASpending request failed: {e}")


def query(start, end, set_aside_none=False):
    """One call. Returns {agency_name: obligated_amount}."""
    filters = {
        "award_type_codes": AWARD_TYPES,
        "naics_codes": {"require": NAICS_PREFIXES},
        "time_period": [{"start_date": start.isoformat(),
                         "end_date": end.isoformat()}],
    }
    if set_aside_none:
        filters["set_aside_type_codes"] = ["NONE"]
    payload = post({
        "category": "awarding_agency",
        "filters": filters,
        "limit": CATEGORY_LIMIT,
        "page": 1,
    })
    out = {}
    for row in payload.get("results") or []:
        name = (row.get("name") or "").strip()
        if name:
            out[name] = float(row.get("amount") or 0)
    return out


def excluded(name):
    up = (name or "").upper()
    for kw in EXCLUDED_AGENCY_KEYWORDS:
        if re.search(r"(?<![A-Z0-9])" + re.escape(kw) + r"(?![A-Z0-9])", up):
            return kw
    return None


def money(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    for cut, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= cut:
            return f"${n/cut:,.1f}{suf}".replace(".0", "")
    return f"${n:,.0f}"


def main():
    today = local_today()
    fy_start = fiscal_year_start(today)
    prior_start = date(fy_start.year - 1, 10, 1)
    try:
        prior_end = today.replace(year=today.year - 1)
    except ValueError:                       # Feb 29
        prior_end = today.replace(year=today.year - 1, day=28)

    fy_label = f"FY{fy_start.year + 1}"
    print(f"window      : {fy_start} .. {today}  ({fy_label} to date)")
    print(f"prior window: {prior_start} .. {prior_end}  (same span, one year earlier)")
    print(f"NAICS       : {', '.join(NAICS_PREFIXES)}")

    print("\ncall 1/3 — in-lane obligations, FY to date")
    cur = query(fy_start, today)
    time.sleep(REQUEST_PAUSE)
    print(f"   {len(cur)} agencies returned")

    print("call 2/3 — same window, set-aside NONE (for the complement)")
    none_only = query(fy_start, today, set_aside_none=True)
    time.sleep(REQUEST_PAUSE)
    print(f"   {len(none_only)} agencies returned")

    print("call 3/3 — same span, prior fiscal year")
    prior = query(prior_start, prior_end)
    print(f"   {len(prior)} agencies returned")

    # Drop excluded agencies (DHS family) from all three windows.
    def drop_excluded(mapping):
        out = {}
        for name, amt in mapping.items():
            kw = excluded(name)
            if kw is not None:
                print(f"   DHS EXCLUDED: {name} ({kw})")
            else:
                out[name] = amt
        return out

    cur = drop_excluded(cur)
    none_only = drop_excluded(none_only)
    prior = drop_excluded(prior)

    total = sum(cur.values())
    prior_total = sum(prior.values())
    if prior_total:
        yoy = (total - prior_total) / prior_total * 100
    else:
        yoy = None

    rows = [
        {"name": name, "amount": amt,
         "pct": (amt / total * 100) if total else 0,
         "setAside": (none_only.get(name, 0) / amt * 100) if amt else 0}
        for name, amt in sorted(cur.items(), key=lambda kv: -kv[1])[:TOP_N]
    ]

    footnote = (
        f"FY{prior_start.year}-{prior_end.year} same span comparison "
        f"{"up" if (yoy or 0) >= 0 else "down"} "
        f"{abs(yoy):.1f}% vs prior year" if yoy is not None else "")

    block = {
        "window": f"{fy_start} .. {today}",
        "priorWindow": f"{prior_start} .. {prior_end}",
        "updated": today.isoformat(),
        "excludedCount": sum(1 for _ in []),  # placeholder; real count below
        "rows": rows,
    }

    # count exclusions across all windows (unique names)
    seen = set()
    for m in (cur, none_only, prior):
        pass
    block["excludedCount"] = len(seen)

    print(f"\ntotal in-lane FY to date: ${total:,.0f}")
    print(f"top {TOP_N} rows written; {block['excludedCount']} excluded (DHS family)")

    new = json.dumps(block)
    html = open(INDEX, encoding="utf-8").read()
    if not MARKET_RE.search(html):
        raise SystemExit("index.html has no MARKET_DATA_START/END block")
    updated = MARKET_RE.sub(lambda m: m.group(1) + new + m.group(2), html)
    with open(INDEX, "w", encoding="utf-8") as f:
        f.write(updated)
    print(f"index.html updated — MARKET_DATA block ({len(new)} bytes)")


if __name__ == "__main__":
    main()
