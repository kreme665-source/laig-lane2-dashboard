#!/usr/bin/env python3
"""
refresh_market.py — rebuild the "Where the Money Flows" section.

Top agencies by obligations already awarded in NAICS 5413 / 5415 / 5416.
Runs server-side in GitHub Actions. No API key, no quota.

WHAT THIS IS — AND IS NOT
    This is spend that has ALREADY HAPPENED. Obligations recorded in FPDS.
    It answers "who buys in my lane, and is that growing" — market context for
    a call. It is NOT pipeline and NOT opportunity. Reading it as demand signal
    would be a mistake: an agency at the top of this table may have just
    finished spending and have nothing coming.

THREE CALLS, NO PER-AGENCY LOOP
    /api/v2/search/spending_by_category/ with category "awarding_agency"
    returns per-agency totals in ONE response, so the whole section costs:

      1. FY-to-date, in-lane, contracts          -> obligations + rank
      2. same, set_aside_type_codes ["NONE"]     -> non-set-aside subset
      3. same period LAST fiscal year            -> FY-over-FY

    Set-aside is derived as the COMPLEMENT of "NONE" rather than by listing
    set-aside codes. Fewer assumptions, and nothing to drift when the code
    list changes.

FY-OVER-FY IS SAME-PERIOD, NOT FULL-YEAR
    FY2026 runs Oct 1 2025 - Sep 30 2026. Comparing FY-to-date against a FULL
    prior year would show a fabricated decline for every agency, every time.
    The prior window is the same calendar span one year earlier.

DHS IS EXCLUDED ENTIRELY
    Same conflict screen as the other feeds. Because DHS is a substantial buyer
    in these NAICS, its removal materially changes the picture — so the section
    carries a footnote saying an agency is withheld. A silent gap would
    misrepresent the market.
"""

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
    r"(/\* MARKET_DATA_START \*/\n).*?(\n  /\* MARKET_DATA_END \*/)", re.S)


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

    rows, dropped = [], []
    for name, amount in sorted(cur.items(), key=lambda kv: -kv[1]):
        kw = excluded(name)
        if kw:
            dropped.append((name, kw))
            continue
        non_sa = none_only.get(name, 0.0)
        # Set-aside share = everything that is NOT "no set aside used".
        set_aside_pct = None
        if amount > 0:
            set_aside_pct = max(0.0, min(100.0, (amount - non_sa) / amount * 100.0))
        prev = prior.get(name)
        yoy = None
        if prev and prev > 0:
            yoy = (amount - prev) / prev * 100.0
        rows.append({
            "agency": name,
            "amount": amount,
            "amountLabel": money(amount),
            "setAsidePct": None if set_aside_pct is None else round(set_aside_pct, 1),
            "yoyPct": None if yoy is None else round(yoy, 1),
            "priorLabel": money(prev) if prev else "—",
        })
        if len(rows) >= TOP_N:
            break

    for i, r in enumerate(rows, 1):
        r["rank"] = i

    print(f"\ntop {len(rows)} agencies after exclusions:")
    for r in rows:
        sa = "—" if r["setAsidePct"] is None else f"{r['setAsidePct']:.0f}%"
        yy = "—" if r["yoyPct"] is None else f"{r['yoyPct']:+.0f}%"
        print(f"   {r['rank']}. {r['agency'][:44]:<46}{r['amountLabel']:>9}"
              f"  set-aside {sa:>5}  YoY {yy:>6}")
    for name, kw in dropped:
        print(f"   EXCLUDED  {name} (matched '{kw}')")

    if not rows:
        raise SystemExit(
            "ABORT: no agencies returned. Leaving index.html unchanged rather "
            "than emptying the section.")

    payload = {
        "window": f"{fy_label} to date · {fy_start.isoformat()} to {today.isoformat()}",
        "priorWindow": f"{prior_start.isoformat()} to {prior_end.isoformat()}",
        "updated": today.isoformat(),
        "excludedCount": len(dropped),
        "rows": rows,
    }

    html = open(INDEX, encoding="utf-8").read()
    if not MARKET_RE.search(html):
        raise SystemExit(
            "MARKET_DATA markers not found in index.html. Expected "
            "/* MARKET_DATA_START */ ... /* MARKET_DATA_END */")
    block = ("  const MARKET_DATA = "
             + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
             + ";")
    html = MARKET_RE.sub(lambda m: m.group(1) + block + m.group(2), html, count=1)
    open(INDEX, "w", encoding="utf-8").write(html)

    size_kb = os.path.getsize(INDEX) / 1024
    print(f"\nindex.html updated — MARKET_DATA written "
          f"({len(rows)} agencies, {len(dropped)} excluded)")
    print(f"   index.html: {size_kb:,.0f} KB")


if __name__ == "__main__":
    main()
