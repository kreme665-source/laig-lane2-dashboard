#!/usr/bin/env python3
"""
refresh_sam.py — regenerate the SAM.gov Sources Sought / RFI feed inside index.html.

Runs SERVER-SIDE ONLY (GitHub Actions). The API key is read from the SAM_API_KEY
environment variable, populated from a GitHub Secret. The key is never written
into index.html and must never be committed to the repo.

QUOTA IS THE BINDING CONSTRAINT
    Personal (non-federal) SAM.gov keys are capped near 10 requests/day on the
    Opportunities API; system accounts get ~1,000. The quota is metered per key,
    resets at 00:00 UTC, and has no sliding window — once spent, it is spent.
    If the outbound email agent uses the same key, both systems draw on one
    budget. This script therefore:

      - makes ONE query (no per-NAICS loop) and filters NAICS locally
      - caps itself at MAX_API_CALLS pages, hard
      - looks back only LOOKBACK_DAYS, since retention keeps older notices anyway
      - treats quota exhaustion as a soft stop: leaves index.html untouched and
        exits 0, so a spent quota does not turn into a daily red X that trains
        everyone to ignore real failures

RETENTION
    Notices are never dropped when they close. An outbound email sent three weeks
    ago may cite a notice that has since closed; removing it would break that
    link. Closed notices are kept RETENTION_DAYS past their close date and render
    with a grey CLOSED badge.

MERGE, NOT REPLACE
    The existing SAM_NOTICES array is parsed out of index.html and merged with
    the fresh pull, keyed on solicitation number. Fresh data wins on conflict;
    anything the API no longer returns survives until it ages out. Hand-authored
    `note` fields are carried across refreshes.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

API_BASE = "https://api.sam.gov/opportunities/v2/search"

# Capture scope: IT services, professional services, engineering.
# Applied LOCALLY after the pull — filtering server-side would cost one call each.
NAICS_SCOPE = {
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
}

PTYPES = "r,s"        # r = Sources Sought, s = Special Notice (where most RFIs live)
LOOKBACK_DAYS = 7     # only newly posted notices; retention preserves the rest
RETENTION_DAYS = 45   # keep closed notices this long so old email links resolve
PAGE_LIMIT = 1000     # max records per call
MAX_API_CALLS = 3     # hard ceiling — must stay well under the daily key quota

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.normpath(os.path.join(HERE, "..", "index.html"))


class QuotaExhausted(Exception):
    """Raised on HTTP 429 so main() can stop cleanly without failing the build."""


# ─────────────────────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────────────────────
def fetch_all(api_key, posted_from, posted_to):
    """One query, paginated, capped at MAX_API_CALLS. Returns (records, calls)."""
    records, offset, calls = [], 0, 0

    while calls < MAX_API_CALLS:
        params = {
            "api_key": api_key,
            "postedFrom": posted_from,     # MM/dd/yyyy — required format
            "postedTo": posted_to,
            "ptype": PTYPES,
            "limit": str(PAGE_LIMIT),
            "offset": str(offset),
        }
        url = API_BASE + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        calls += 1

        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # Never echo the URL anywhere — it carries the API key.
            body = e.read().decode("utf-8", "replace")[:500]
            if e.code == 429:
                raise QuotaExhausted(body)
            if e.code in (401, 403):
                raise SystemExit(
                    f"SAM.gov rejected the API key (HTTP {e.code}). "
                    f"Check the SAM_API_KEY secret. Response: {body}")
            raise SystemExit(f"SAM.gov API HTTP {e.code}: {body}")
        except Exception as e:
            raise SystemExit(f"SAM.gov API call failed: {e}")

        batch = payload.get("opportunitiesData") or []
        records.extend(batch)
        total = int(payload.get("totalRecords") or 0)
        offset += len(batch)

        print(f"  call {calls}: {len(batch)} records "
              f"(offset now {offset} of {total} total)")

        if not batch or offset >= total:
            return records, calls, False

    return records, calls, True   # True = hit the call ceiling, may be truncated


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
    path = (rec.get("fullParentPathName") or "").strip()
    return {
        "solNum": sol,
        "title": (rec.get("title") or "").strip(),
        "agency": path.split(".")[-1].strip() or path,
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
    m = NOTICES_RE.search(html)
    if not m:
        raise SystemExit(
            "SAM_NOTICES markers not found in index.html. Expected "
            "/* SAM_NOTICES_START */ ... /* SAM_NOTICES_END */")
    block = m.group(0)
    body = block[block.index("["): block.rindex("]") + 1]
    jsonish = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', body)
    jsonish = re.sub(r",\s*\]", "]", jsonish)
    try:
        return json.loads(jsonish)
    except json.JSONDecodeError as e:
        raise SystemExit(f"Could not parse existing SAM_NOTICES: {e}")


def js_array(notices):
    def esc(s):
        return json.dumps(s if s is not None else "", ensure_ascii=False)
    rows = [
        "    {solNum:%s,title:%s,agency:%s,naics:%s,type:%s,"
        "postedDate:%s,closeDate:%s,samLink:%s,note:%s}" % (
            esc(n.get("solNum")), esc(n.get("title")), esc(n.get("agency")),
            esc(n.get("naics")), esc(n.get("type")), esc(n.get("postedDate")),
            esc(n.get("closeDate")), esc(n.get("samLink")), esc(n.get("note")))
        for n in notices
    ]
    return "  const SAM_NOTICES = [\n" + ",\n".join(rows) + "\n  ];"


# ─────────────────────────────────────────────────────────────────────────────
def main():
    api_key = os.environ.get("SAM_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "SAM_API_KEY is not set. In GitHub Actions this comes from a "
            "repository secret. Never hardcode the key.")

    today = date.today()
    posted_from = (today - timedelta(days=LOOKBACK_DAYS)).strftime("%m/%d/%Y")
    posted_to = today.strftime("%m/%d/%Y")

    html = open(INDEX, encoding="utf-8").read()
    existing = read_existing(html)
    print(f"existing notices in index.html : {len(existing)}")
    print(f"querying {posted_from} .. {posted_to}  "
          f"(max {MAX_API_CALLS} API calls)")

    try:
        records, calls, truncated = fetch_all(api_key, posted_from, posted_to)
    except QuotaExhausted as e:
        # Soft stop. The dashboard keeps its existing notices and stays correct;
        # closing countdowns are computed in the browser, so nothing goes stale
        # in a misleading way. Exit 0 so this does not read as a broken build.
        print()
        print("=" * 62)
        print("SAM.gov DAILY QUOTA EXHAUSTED — index.html left unchanged.")
        print(str(e)[:400])
        print()
        print("The quota is metered per API key and resets at 00:00 UTC.")
        print("If the outbound email agent shares this key, both systems draw")
        print("on the same daily budget. Options: run this job just after the")
        print("UTC reset, or issue the dashboard its own key.")
        print("=" * 62)
        return

    print(f"API calls used                 : {calls}")
    if truncated:
        print("WARNING: hit the MAX_API_CALLS ceiling — results may be partial. "
              "Retention still protects existing notices.")

    fresh = {}
    skipped_scope = 0
    for rec in records:
        n = to_notice(rec)
        if not n:
            continue
        if n["naics"] not in NAICS_SCOPE:
            skipped_scope += 1
            continue
        fresh.setdefault(n["solNum"], n)
    print(f"in-scope notices from API      : {len(fresh)} "
          f"({skipped_scope} outside NAICS scope, discarded)")

    # ---- merge: fresh wins, but keep hand-authored notes ----
    by_sol = {n.get("solNum", ""): dict(n) for n in existing}
    for sol, n in fresh.items():
        if sol in by_sol and by_sol[sol].get("note"):
            n = dict(n, note=by_sol[sol]["note"])
        by_sol[sol] = n

    # ---- retention: drop only what closed longer ago than RETENTION_DAYS ----
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

    merged.sort(key=lambda n: (n.get("closeDate") or "9999-99-99",
                               n.get("title") or ""))

    still_open = sum(
        1 for n in merged
        if not n.get("closeDate")
        or datetime.strptime(n["closeDate"], "%Y-%m-%d").date() >= today)
    print(f"retained (incl. closed)        : {len(merged)}  "
          f"({still_open} open, {len(merged)-still_open} closed, "
          f"{aged_out} aged out)")

    try:
        stamp = today.strftime("%b %-d, %Y")
    except ValueError:      # Windows strftime lacks %-d
        stamp = today.strftime("%b %d, %Y").replace(" 0", " ")

    html = NOTICES_RE.sub(lambda m: m.group(1) + js_array(merged) + m.group(2),
                          html, count=1)
    html = PULLED_RE.sub(
        lambda m: m.group(1) + f"  const SAM_LAST_PULLED = '{stamp}';" + m.group(2),
        html, count=1)

    open(INDEX, "w", encoding="utf-8").write(html)
    print(f"index.html updated. Last pulled: {stamp}")


if __name__ == "__main__":
    main()
