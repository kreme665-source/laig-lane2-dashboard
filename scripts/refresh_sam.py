#!/usr/bin/env python3
"""
refresh_sam.py — regenerate the SAM.gov Sources Sought / RFI feed inside index.html.

Runs SERVER-SIDE ONLY (GitHub Actions). The API key is read from the SAM_API_KEY
environment variable, which is populated from a GitHub Secret. The key is never
written into index.html and must never be committed to the repo.

Design notes that matter:

  RETENTION — notices are never dropped when they close. An outbound email sent
  three weeks ago may cite a notice that has since closed; if this script removed
  it, that email's link would break. Closed notices are kept for RETENTION_DAYS
  past their close date and render with a grey CLOSED badge at 50% opacity.

  MERGE, NOT REPLACE — the existing SAM_NOTICES array is parsed out of index.html
  and merged with the fresh API pull, keyed on solicitation number. Fresh data
  wins on conflict; anything the API no longer returns survives until it ages out.

  MANUAL NOTES PRESERVED — the `note` field is hand-authored (e.g. "Sole source
  intent — incumbent: American Systems Corporation") and is carried across
  refreshes rather than overwritten with an empty string.
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

API_BASE = "https://api.sam.gov/opportunities/v2/search"

# Capture scope: IT services, professional services, engineering.
# Anything outside GovCon capture positioning stays out per the SOP.
NAICS_SCOPE = [
    "541511",  # Custom Computer Programming Services
    "541512",  # Computer Systems Design Services
    "541513",  # Computer Facilities Management
    "541519",  # Other Computer Related Services
    "518210",  # Data Processing, Hosting
    "541330",  # Engineering Services
    "541611",  # Admin & General Management Consulting
    "541612",  # HR Consulting
    "541618",  # Other Management Consulting
    "541690",  # Other Scientific & Technical Consulting
    "541715",  # R&D in Physical, Engineering & Life Sciences
]

# Procurement types: r = Sources Sought, s = Special Notice (where most RFIs live).
PTYPES = "r,s"

LOOKBACK_DAYS = 30    # how far back to pull newly posted notices
RETENTION_DAYS = 45   # keep closed notices this long so old email links still resolve
PAGE_LIMIT = 1000
REQUEST_PAUSE = 0.4   # be polite to the API between calls

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.normpath(os.path.join(HERE, "..", "index.html"))


# ─────────────────────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────────────────────
def fetch_naics(api_key, ncode, posted_from, posted_to):
    """Fetch all notices for one NAICS code, following pagination."""
    out, offset = [], 0
    while True:
        params = {
            "api_key": api_key,
            "postedFrom": posted_from,   # MM/dd/yyyy — required format
            "postedTo": posted_to,
            "ncode": ncode,
            "ptype": PTYPES,
            "limit": str(PAGE_LIMIT),
            "offset": str(offset),
        }
        url = API_BASE + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:400]
            # Never echo the URL — it contains the API key.
            raise SystemExit(f"SAM.gov API HTTP {e.code} for NAICS {ncode}: {body}")
        except Exception as e:
            raise SystemExit(f"SAM.gov API call failed for NAICS {ncode}: {e}")

        batch = payload.get("opportunitiesData") or []
        out.extend(batch)
        total = int(payload.get("totalRecords") or 0)
        offset += len(batch)
        if not batch or offset >= total:
            break
        time.sleep(REQUEST_PAUSE)
    return out


def norm_date(v):
    """SAM returns ISO-ish stamps; we want plain YYYY-MM-DD or ''."""
    if not v:
        return ""
    s = str(v)[:10]
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return ""


def classify(rec):
    """Map SAM's notice type onto the two labels the dashboard renders."""
    t = (rec.get("type") or "").strip()
    title = (rec.get("title") or "").lower()
    if t.lower().startswith("sources sought"):
        return "Sources Sought"
    if "request for information" in title or re.search(r"\brfi\b", title):
        return "RFI"
    if t.lower().startswith("special notice"):
        return "RFI"
    return t or "Sources Sought"


def to_notice(rec):
    sol = (rec.get("solicitationNumber") or rec.get("noticeId") or "").strip()
    if not sol:
        return None
    return {
        "solNum": sol,
        "title": (rec.get("title") or "").strip(),
        "agency": (rec.get("fullParentPathName") or "").split(".")[-1].strip()
                  or (rec.get("fullParentPathName") or "").strip(),
        "naics": (rec.get("naicsCode") or "").strip(),
        "type": classify(rec),
        "postedDate": norm_date(rec.get("postedDate")),
        "closeDate": norm_date(rec.get("responseDeadLine")),
        "samLink": (rec.get("uiLink") or "").strip(),
        "note": "",
    }


# ─────────────────────────────────────────────────────────────────────────────
# index.html read / write
# ─────────────────────────────────────────────────────────────────────────────
NOTICES_RE = re.compile(
    r"(/\* SAM_NOTICES_START \*/\n).*?(\n  /\* SAM_NOTICES_END \*/)", re.S)
PULLED_RE = re.compile(
    r"(/\* SAM_LAST_PULLED_START \*/\n).*?(\n  /\* SAM_LAST_PULLED_END \*/)", re.S)


def read_existing(html):
    """Parse the current SAM_NOTICES array so we can retain + preserve notes."""
    m = NOTICES_RE.search(html)
    if not m:
        raise SystemExit(
            "SAM_NOTICES markers not found in index.html. "
            "Expected /* SAM_NOTICES_START */ ... /* SAM_NOTICES_END */")
    block = m.group(0)
    body = block[block.index("["): block.rindex("]") + 1]
    # The array is JS object-literal syntax with unquoted keys; quote them for JSON.
    jsonish = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', body)
    jsonish = re.sub(r",\s*\]", "]", jsonish)
    try:
        return json.loads(jsonish)
    except json.JSONDecodeError as e:
        raise SystemExit(f"Could not parse existing SAM_NOTICES: {e}")


def js_array(notices):
    def esc(s):
        return json.dumps(s if s is not None else "", ensure_ascii=False)
    rows = []
    for n in notices:
        rows.append(
            "    {solNum:%s,title:%s,agency:%s,naics:%s,type:%s,"
            "postedDate:%s,closeDate:%s,samLink:%s,note:%s}" % (
                esc(n.get("solNum")), esc(n.get("title")), esc(n.get("agency")),
                esc(n.get("naics")), esc(n.get("type")), esc(n.get("postedDate")),
                esc(n.get("closeDate")), esc(n.get("samLink")), esc(n.get("note"))))
    return "  const SAM_NOTICES = [\n" + ",\n".join(rows) + "\n  ];"


# ─────────────────────────────────────────────────────────────────────────────
def main():
    api_key = os.environ.get("SAM_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "SAM_API_KEY is not set. In GitHub Actions this comes from a repository "
            "secret. Never hardcode the key in this file or in index.html.")

    today = date.today()
    posted_from = (today - timedelta(days=LOOKBACK_DAYS)).strftime("%m/%d/%Y")
    posted_to = today.strftime("%m/%d/%Y")

    html = open(INDEX, encoding="utf-8").read()
    existing = read_existing(html)
    print(f"existing notices in index.html : {len(existing)}")

    fresh = {}
    for ncode in NAICS_SCOPE:
        recs = fetch_naics(api_key, ncode, posted_from, posted_to)
        kept = 0
        for rec in recs:
            n = to_notice(rec)
            if n and n["solNum"] not in fresh:
                fresh[n["solNum"]] = n
                kept += 1
        print(f"  NAICS {ncode}: {len(recs)} returned, {kept} new")
        time.sleep(REQUEST_PAUSE)
    print(f"fresh unique notices from API  : {len(fresh)}")

    # ---- merge: fresh data wins, but keep hand-authored notes ----
    by_sol = {}
    for n in existing:
        by_sol[n.get("solNum", "")] = dict(n)
    for sol, n in fresh.items():
        if sol in by_sol and by_sol[sol].get("note"):
            n = dict(n, note=by_sol[sol]["note"])   # preserve manual annotation
        by_sol[sol] = n

    # ---- retention: drop only what has been closed longer than RETENTION_DAYS ----
    cutoff = today - timedelta(days=RETENTION_DAYS)
    merged, aged_out = [], 0
    for n in by_sol.values():
        cd = n.get("closeDate") or ""
        if cd:
            try:
                if datetime.strptime(cd, "%Y-%m-%d").date() < cutoff:
                    aged_out += 1
                    continue
            except ValueError:
                pass
        merged.append(n)

    # ---- sort: soonest closing first; blank close dates last ----
    merged.sort(key=lambda n: (n.get("closeDate") or "9999-99-99", n.get("title") or ""))

    still_open = sum(
        1 for n in merged
        if not n.get("closeDate")
        or datetime.strptime(n["closeDate"], "%Y-%m-%d").date() >= today)
    print(f"retained (incl. closed)        : {len(merged)}  "
          f"({still_open} open, {len(merged)-still_open} closed, {aged_out} aged out)")

    stamp = today.strftime("%b %-d, %Y") if os.name != "nt" else today.strftime("%b %d, %Y")
    html = NOTICES_RE.sub(
        lambda m: m.group(1) + js_array(merged) + m.group(2), html, count=1)
    html = PULLED_RE.sub(
        lambda m: m.group(1) + f"  const SAM_LAST_PULLED = '{stamp}';" + m.group(2),
        html, count=1)

    open(INDEX, "w", encoding="utf-8").write(html)
    print(f"index.html updated. Last pulled: {stamp}")


if __name__ == "__main__":
    main()
