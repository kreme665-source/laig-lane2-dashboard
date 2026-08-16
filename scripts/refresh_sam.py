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
from zoneinfo import ZoneInfo

# TIMEZONE — read this before changing anything date-related.
#
# GitHub Actions runners are UTC. The scheduled run fires at 00:15 UTC, which is
# 19:15 the PREVIOUS DAY in US Central. Using date.today() on the runner would
# therefore stamp the dashboard with tomorrow's date every single night — the
# reader in Texas sees "Last pulled: Aug 14" at 7:15pm on Aug 13.
#
# That is almost certainly what produced the original "Last pulled: Aug 13" on a
# file built Aug 12. It looks like a typo and is not one; it is a UTC/local slip.
#
# Every date in this script — the freshness stamp, retention cutoffs, validation
# thresholds, and the API query window — is therefore computed in the audience's
# timezone, not the runner's.
LOCAL_TZ = ZoneInfo("America/Chicago")


def local_today():
    return datetime.now(LOCAL_TZ).date()

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

# ─────────────────────────────────────────────────────────────────────────────
# EXCLUDED AGENCIES — conflict-of-interest screen
#
# DHS is excluded entirely: Phil is engaged with that agency, so it must not
# appear anywhere prospect-facing. Matching on the string "DHS" alone is not
# enough — DHS notices surface under component names that never say "DHS"
# (Coast Guard, TSA, FEMA, CISA, Secret Service, CBP, ICE, USCIS), and under
# contracting offices like "AVIATION LOGISTICS CENTER" that are USCG.
#
# Three independent signals are checked, and any one of them excludes:
#   1. agency / office name keywords
#   2. title keywords (catches a DHS requirement bought through GSA)
#   3. solicitation number prefix — DHS components use the 70xx family
#      (70Z = USCG, 70T = TSA, 70B = CBP, 70RTAC/70RSAT = DHS HQ, etc.)
#
# To re-enable DHS later, empty EXCLUDED_AGENCY_KEYWORDS and
# EXCLUDED_SOLNUM_PREFIXES. Do not do so without Phil's explicit say-so.
# ─────────────────────────────────────────────────────────────────────────────
EXCLUDED_AGENCY_KEYWORDS = [
    "DHS", "HOMELAND SECURITY", "HOMELAND",
    "COAST GUARD", "USCG", "CG-", "SFLC", "AVIATION LOGISTICS CENTER",
    "TRANSPORTATION SECURITY", "TSA",
    "SECRET SERVICE", "USSS",
    "CUSTOMS AND BORDER", "CBP",
    "IMMIGRATION AND CUSTOMS", "IMMIGRATION & CUSTOMS", "ICE/", "HSI",
    "CITIZENSHIP AND IMMIGRATION", "USCIS",
    "FEDERAL EMERGENCY MANAGEMENT", "FEMA",
    "CYBERSECURITY AND INFRASTRUCTURE", "CISA",
    "FEDERAL LAW ENFORCEMENT TRAINING", "FLETC",
    "FEDERAL PROTECTIVE SERVICE",
]
EXCLUDED_SOLNUM_PREFIXES = ("70",)

PTYPES = "r,s"        # r = Sources Sought, s = Special Notice (where most RFIs live)
LOOKBACK_DAYS = 30     # only newly posted notices; retention preserves the rest
RETENTION_DAYS = 45   # keep closed notices this long so old email links resolve
PAGE_LIMIT = 1000     # max records per call
MAX_API_CALLS = 5     # hard ceiling — must stay well under the daily key quota

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
    """SAM returns ISO-ish stamps; we want plain YYYY-MM-DD or ''.

    Returns '' on anything unparseable. Callers must treat '' as a defect and
    not as 'no deadline' — see validate_notice(). Silently accepting a blank
    close date is how a notice ends up never showing CLOSED and never ageing
    out of retention.
    """
    if not v:
        return ""
    s = str(v)[:10]
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Date validation
#
# A wrong date on this dashboard is worse than a missing notice: the outbound
# emails quote closing dates, so a transcription slip (the classic 07->08 month
# error) makes the email look fabricated. Dates now come straight from the API
# rather than being retyped, which removes that class at the source — but the
# seed data predates that, and API payloads are not guaranteed sane. So every
# record is checked before it is allowed into index.html.
#
#   ERROR    -> record is quarantined (dropped) and reported
#   WARNING  -> record is published but flagged in the log
# ─────────────────────────────────────────────────────────────────────────────
MAX_FUTURE_YEARS = 3
MAX_OPEN_SPAN_DAYS = 400


def validate_notice(n, today):
    """Return (errors, warnings) for one notice."""
    errors, warnings = [], []
    sol = n.get("solNum") or "<no solicitation number>"
    p_raw, c_raw = n.get("postedDate", ""), n.get("closeDate", "")

    if not p_raw:
        warnings.append(f"{sol}: missing/unparseable postedDate")
    if not c_raw:
        errors.append(f"{sol}: missing/unparseable closeDate — cannot compute "
                      f"a closing countdown or age it out of retention")
        return errors, warnings

    try:
        cd = datetime.strptime(c_raw, "%Y-%m-%d").date()
    except ValueError:
        errors.append(f"{sol}: closeDate '{c_raw}' is not a real date")
        return errors, warnings

    pd = None
    if p_raw:
        try:
            pd = datetime.strptime(p_raw, "%Y-%m-%d").date()
        except ValueError:
            warnings.append(f"{sol}: postedDate '{p_raw}' is not a real date")

    # Impossible orderings — the signature of a month/day transcription slip.
    if pd and pd > cd:
        errors.append(
            f"{sol}: posted {p_raw} is AFTER close {c_raw} "
            f"({(pd - cd).days}d) — likely a month/day typo")

    if pd and pd > today + timedelta(days=2):
        errors.append(
            f"{sol}: postedDate {p_raw} is in the future (today {today}) — "
            f"check the build clock or a mistyped month")

    if cd.year > today.year + MAX_FUTURE_YEARS:
        errors.append(f"{sol}: closeDate {c_raw} is more than "
                      f"{MAX_FUTURE_YEARS} years out — likely a mistyped year")

    if pd and (cd - pd).days > MAX_OPEN_SPAN_DAYS:
        warnings.append(f"{sol}: open for {(cd - pd).days}d "
                        f"({p_raw} -> {c_raw}) — unusually long, verify")

    # Freshly posted but already closed: only reachable via a bad date.
    if pd and cd < today and pd >= today - timedelta(days=LOOKBACK_DAYS):
        warnings.append(
            f"{sol}: posted {p_raw} but closed {c_raw} — newly posted yet "
            f"already expired, verify the month")

    return errors, warnings


def excluded_reason(n):
    """Return why this notice is screened out, or None if it may be published."""
    agency = (n.get("agency") or "").upper()
    title = (n.get("title") or "").upper()
    sol = (n.get("solNum") or "").upper().strip()

    for kw in EXCLUDED_AGENCY_KEYWORDS:
        if kw in agency:
            return f"agency '{n.get('agency')}' matches excluded keyword '{kw}'"
    for kw in EXCLUDED_AGENCY_KEYWORDS:
        # Word-boundary on the title so "ICE" does not match "SERVICE".
        if re.search(r"(?<![A-Z0-9])" + re.escape(kw) + r"(?![A-Z0-9])", title):
            return f"title matches excluded keyword '{kw}'"
    if sol.startswith(EXCLUDED_SOLNUM_PREFIXES):
        return f"solicitation number '{sol}' is in the DHS 70xx family"
    return None


def screen_excluded(notices, label):
    """Drop excluded-agency notices. Returns (kept, removed)."""
    kept, removed = [], []
    for n in notices:
        r = excluded_reason(n)
        (removed if r else kept).append((n, r) if r else n)
    print(f"\n--- agency screen ({label}): {len(notices)} checked ---")
    if not removed:
        print("    nothing excluded")
    for n, r in removed:
        print(f"    EXCLUDE  {n.get('solNum')}: {r}")
    return kept, removed


def audit_dates(notices, today, label):
    """Validate a list of notices. Returns (clean, dropped)."""
    clean, dropped, all_warn = [], [], []
    for n in notices:
        errs, warns = validate_notice(n, today)
        all_warn.extend(warns)
        if errs:
            dropped.append((n, errs))
        else:
            clean.append(n)

    print(f"\n--- date audit ({label}): {len(notices)} checked ---")
    if not dropped and not all_warn:
        print("    no date anomalies")
    for w in all_warn:
        print(f"    WARN  {w}")
    for n, errs in dropped:
        for e in errs:
            print(f"    DROP  {e}")
    return clean, dropped


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


# SAM.gov's API returns uiLink pointing at the signed-in WORKSPACE path:
#     https://sam.gov/workspace/contract/opp/<32-hex-id>/view
# A prospect arriving from an outbound email is not signed in, so that path
# risks dropping them on a login screen instead of the notice — which defeats
# the entire reason the email cites a notice number. The public permalink
#     https://sam.gov/opp/<32-hex-id>/view
# carries the same opportunity id and needs no account. Rewrite on ingest.
WORKSPACE_LINK_RE = re.compile(
    r"^https://sam\.gov/workspace/contract/opp/([0-9a-f]{32})/view/?$", re.I)


def public_sam_link(url):
    """Normalise a SAM.gov opportunity URL to its public permalink form."""
    u = (url or "").strip()
    m = WORKSPACE_LINK_RE.match(u)
    return f"https://sam.gov/opp/{m.group(1).lower()}/view" if m else u


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
        "samLink": public_sam_link(rec.get("uiLink")),
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
    try:
        return json.loads(quote_js_keys(body))
    except json.JSONDecodeError as e:
        raise SystemExit(f"Could not parse existing SAM_NOTICES: {e}")


def quote_js_keys(js):
    """Convert a JS object-literal array to strict JSON.

    Written as a character walk rather than a regex on purpose. The obvious
    regex — re.sub(r'([{,]\\s*)(\\w+)\\s*:', ...) — silently corrupts any record
    whose STRING VALUE contains a comma followed by a word and a colon, e.g.

        title:"Sources Sought, Phase: 2"
                            ^^^^^^^^ regex rewrites this as a key

    That produces malformed JSON, or worse, plausible-but-wrong data. Real SAM
    titles contain exactly this pattern. So: track whether we are inside a
    string literal, and only quote keys when we are not.
    """
    out, i, n = [], 0, len(js)
    in_str = False
    quote = ""
    while i < n:
        c = js[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:      # preserve escape sequences intact
                out.append(js[i + 1]); i += 2; continue
            if c == quote:
                in_str = False
            i += 1
            continue
        if c in "\"'":
            in_str, quote = True, c
            out.append('"')                   # normalise single quotes to double
            i += 1
            continue
        # outside a string: an identifier immediately followed by ':' is a key
        m = re.match(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*:", js[i:])
        if m and (not out or out[-1].strip()[-1:] in "{,[" or out[-1] in "{,"):
            out.append('"' + m.group(1) + '":')
            i += m.end()
            continue
        out.append(c)
        i += 1
    txt = "".join(out)
    return re.sub(r",(\s*[\]}])", r"\1", txt)   # drop trailing commas


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

    today = local_today()
    runner_today = date.today()
    if today != runner_today:
        print(f"note: runner clock is {runner_today} (UTC), audience date is "
              f"{today} (US Central). Using the audience date.")

    posted_from = (today - timedelta(days=LOOKBACK_DAYS)).strftime("%m/%d/%Y")
    # +1 day so notices posted "today" in UTC are not missed when the local
    # date is still yesterday.
    posted_to = (today + timedelta(days=1)).strftime("%m/%d/%Y")

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

    # ---- conflict screen: runs over the MERGED list so already-seeded DHS
    # records are purged too, not just newly pulled ones ----
    merged, excluded = screen_excluded(merged, "merged feed")

    # ---- date audit: quarantine anything with an impossible date ----
    # Runs over the MERGED list so legacy seed records are checked too, not just
    # what the API just returned.
    merged, quarantined = audit_dates(merged, today, "merged feed")
    if quarantined:
        share = len(quarantined) / max(1, len(merged) + len(quarantined))
        print(f"\n{len(quarantined)} record(s) quarantined "
              f"({share:.0%} of the feed)")
        if share > 0.20:
            raise SystemExit(
                "ABORT: more than 20% of notices failed date validation. "
                "That is a systemic problem, not a one-off bad record — "
                "index.html left unchanged.")

    # Normalise every published link, not just newly ingested ones — records
    # carried over by retention may predate public_sam_link().
    relinked = 0
    for n in merged:
        before = n.get("samLink") or ""
        after = public_sam_link(before)
        if after != before:
            n["samLink"] = after
            relinked += 1
    if relinked:
        print(f"normalised {relinked} workspace link(s) to public permalink")

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
