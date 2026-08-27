#!/usr/bin/env python3
"""
refresh_usaspending.py — rebuild Recompete Watch from USASpending (FPDS).

Replaces the static DOE OSBP snapshot. Runs server-side in GitHub Actions.
No API key, no quota — USASpending is a fully open endpoint.

WHY CONTRACT END DATES, NOT FORECAST DATES
    Agency forecast dates are projections and they slip: in the Aug 2026
    Acquisition Gateway export, 1,400 of 1,504 rows were already past their own
    published solicitation date. A period-of-performance end date is a term the
    Government already committed to and reported to FPDS. It does not slip.

WHAT THIS SCRIPT WILL NOT DO
    It does not predict whether an option will be exercised. The Government may
    exercise or decline at its discretion, the RFO text at 17.204-1 relaxed the
    prior "most advantageous method" test, and the 5-year services ceiling is a
    default that agency supplements and approved acquisition strategies can
    exceed. Observed dates only, labelled as to which kind.

WHY ENRICHMENT EXISTS — the 99.7% problem
    The search endpoint returns exactly ONE end date per award. A status derived
    from a single date cannot separate "the option year ends here" from "the
    contract ends here", so it collapses into a restatement of award type:
    every contract got one label, every IDV the other, and the bridge branch was
    unreachable because no record ever held both dates.

    The award detail endpoint returns them together:
        period_of_performance.end_date           current period end
        period_of_performance.potential_end_date ultimate end (nullable)

    So the curated set is enriched after selection. 220 calls at ~0.25s is about
    a minute against an endpoint with no quota. Enriching all 4,336 would cost
    twenty minutes for records that never render.

CURATION — display is capped, data is not
    The top DISPLAY_CAP records by value then urgency render on the page. EVERY
    record is written to forecast-index.json, which the search box lazy-loads.
    This trims DISPLAY ONLY — a record cited in an outbound email stays findable
    whether or not it made the cut, marked archive rather than live.

FIELD NAMES ARE PER AWARD TYPE — this is what broke run #7
    "Period of Performance Current End Date" is in the RESPONSE schema but is
    NOT requestable, so sorting on it returns HTTP 400:
        Contracts (A,B,C,D) -> "End Date"
        IDVs                -> "Last Date to Order"   (no "End Date" at all)

API SHAPE — read before changing the query
    period_of_performance_current_end_date is NOT a valid `date_type`. The award
    search time_period filter accepts only action_date, date_signed,
    last_modified_date and new_awards_only. The end-date window is applied
    CLIENT-SIDE after sorting by end date descending. Do not "optimise" this
    into a server-side date filter; it does not exist.
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

API_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
AWARD_DETAIL_URL = "https://api.usaspending.gov/api/v2/awards/{}/"

LOCAL_TZ = ZoneInfo("America/Chicago")


def local_today():
    return datetime.now(LOCAL_TZ).date()


# ── Positioning windows ──────────────────────────────────────────────────────
# Two bands, two questions, one nightly pull.
#
#   Position Now      60-179 days   this contract's CURRENT PERIOD ends soon
#   Recompete Watch  180-545 days   this contract's COMPETITION POINT falls here
#
# The Recompete window is UNCHANGED and its records are untouched by the
# Position Now work.
#
# Why the near band had to be added rather than derived: before this change
# WINDOW_MIN_DAYS = 180 was the floor of the ENTIRE feed. Nothing ending sooner
# than 180 days was ever fetched, so no such record existed anywhere - not in
# the curated set, not in the 4,260-row USASPENDING archive (measured earliest
# end date: 187 days out). Position Now could not be rebuilt from stored data;
# the floor itself was the blocker.
WINDOW_MIN_DAYS = 180
WINDOW_MAX_DAYS = 545

# Half-open against the Recompete floor so a record can never land in both
# bands and be rendered twice.
POSITION_MIN_DAYS = 60
POSITION_MAX_DAYS = WINDOW_MIN_DAYS - 1        # 179

# POSITION_MIN_DAYS mirrors INELIGIBLE_DAYS = 60 in index.html. index.html
# recomputes daysOut from the visitor's clock and drops anything under 60, so a
# Position Now row ages out on its own between runs. Keep these two in sync: a
# lower floor here would write rows the page discards on arrival.

# ── Display curation ─────────────────────────────────────────────────────────
# Separate allocations, deliberately. The Recompete sort is value-descending,
# so a single shared cap would let long-dated high-value awards crowd out every
# near-term row - which is precisely the set Position Now exists to surface.
DISPLAY_CAP = 220              # Recompete Watch - unchanged
POSITION_CAP = 60              # Position Now - reserved, cannot be crowded out

# Position Now renders a 120-day band through a 60-row window. Taking simply
# the 60 soonest turned out to cover about two days of it - a section labelled
# 60-179 days showing 60-61. The label would be doing work the data does not
# support, which is the same failure as a baked date.
#
# So the cap is spread across the band in equal time strata, highest value
# first inside each, with unused slots from thin strata handed to full ones.
# Every rendered row is still an observed period end; only WHICH rows are shown
# changes. Set to 1 for the old soonest-first behaviour.
POSITION_STRATA = 4

# ── Enrichment ───────────────────────────────────────────────────────────────
ENRICH_CURATED = True
ENRICH_PAUSE = 0.25

NAICS_PREFIXES = ["5413", "5415", "5416"]
MIN_AWARD_AMOUNT = 250_000
ACTION_LOOKBACK_DAYS = 730

# The near band needs a longer memory than the recompete band. A contract
# ending in 90 days with no transaction in two years is not a stale record -
# it is the single most valuable Position Now case: a final period running out
# with no option left to exercise. A 730-day action_date lookback is most
# likely to drop exactly those. Widened for the near band only; the recompete
# band keeps 730 and its records are unchanged.
POSITION_ACTION_LOOKBACK_DAYS = 1825      # 5 years

CONTRACT_TYPES = ["A", "B", "C", "D"]
IDV_TYPES = ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C",
             "IDV_C", "IDV_D", "IDV_E"]

PAGE_LIMIT = 100
MAX_PAGES = 250

# THE binding constraint, and it is not ours. spending_by_award stops paging at
# 10,000 records: hasNext goes false at page 100 whatever MAX_PAGES says. The
# window is applied client-side (period_of_performance_current_end_date is not
# a valid date_type), so every out-of-window record still spends part of that
# 10,000.
#
# Measured on the unpartitioned query: walking end-date DESCENDING, the 10,000
# records span from the far-future tail down to roughly 180 days out - landing
# almost exactly on the recompete floor. Walking ASCENDING, they span 2001 to
# about 2019. The 60-179 day band falls in the gap between those two reachable
# windows, which is why neither direction could see it and why raising
# MAX_PAGES changed nothing.
#
# The fix is not a bigger walk, it is a smaller universe: partition the query
# so each slice fits inside 10,000, then walk each slice descending.
API_RECORD_CAP = 10_000

# Sub-partition tiers for any single agency that still exceeds the cap (DoD is
# the likely one). Applied only where needed, never pre-emptively.
AMOUNT_TIERS = [(250_000, 1_000_000), (1_000_000, 10_000_000),
                (10_000_000, 100_000_000), (100_000_000, None)]

# ── Band scoping ─────────────────────────────────────────────────────────────
# Position Now is CONTRACTS ONLY. An IDV's "Last Date to Order" is the close of
# an ordering window, not the end of a period of performance. Putting it under
# a heading that reads "current period of performance ends" would state one
# event and mean another, which is the same failure as asserting a competition
# point from a period end.
#
# IDVs are not dropped, they are scoped: they stay in Recompete Watch, where an
# ordering window closing IS the signal, and they are labelled as that.
#
# This also resolves a measured blind spot. Across v3 and both v4 runs the near
# band returned 5-6 IDVs while the recompete band held a steady 12-15% IDV
# share right down to its 180-day floor (203 IDVs in the 180-209 day bucket).
# A continuous population does not stop dead at a boundary; that leg was never
# reaching the band. Scoping removes the question rather than papering over it.
POSITION_AWARD_TYPES = CONTRACT_TYPES

# Sub-partition dimension by leg. Award-type code is the right axis for IDVs:
# eight codes, each a genuinely different population, versus amount which does
# not separate them. Amount stays the axis for contracts.
SUBPARTITION_BY_TYPE = "type"
SUBPARTITION_BY_AMOUNT = "amount"
REQUEST_PAUSE = 0.25
BRIDGE_MAX_DAYS = 183    # 52.217-8 caps the extension at six months

# ── Conflict screen — keep in sync with refresh_sam.py ───────────────────────
EXCLUDED_AGENCY_KEYWORDS = [
    "DHS", "HOMELAND SECURITY", "HOMELAND",
    "COAST GUARD", "USCG", "SFLC", "AVIATION LOGISTICS CENTER",
    "TRANSPORTATION SECURITY", "TSA",
    "SECRET SERVICE", "USSS",
    "CUSTOMS AND BORDER", "CBP",
    "IMMIGRATION AND CUSTOMS", "USCIS",
    "FEDERAL EMERGENCY MANAGEMENT", "FEMA",
    "CYBERSECURITY AND INFRASTRUCTURE", "CISA",
    "FEDERAL LAW ENFORCEMENT TRAINING", "FLETC",
]

AGENCY_MAP = {
    "Department of Veterans Affairs": "VA",
    "Department of Energy": "DOE",
    "Department of Transportation": "DOT",
    "Department of Defense": "DoD",
    "General Services Administration": "GSA",
    "Department of Health and Human Services": "HHS",
    "Department of State": "State",
    "Department of the Interior": "DOI",
    "Department of Agriculture": "USDA",
    "National Science Foundation": "NSF",
    "Department of Labor": "DOL",
}

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.normpath(os.path.join(HERE, "..", "index.html"))
INDEX_JSON = os.path.normpath(os.path.join(HERE, "..", "forecast-index.json"))

FEED_MARKER = "_USAS_"

# ── Search-index layers ──────────────────────────────────────────────────────
# Every index row carries an explicit source tag in field 10. Layers are
# identified POSITIVELY, never by exclusion.
#
# Why this matters: the retired label must never mask a stale LIVE feed.
# Inferring "anything that isn't mine is retired" would do exactly that — a SAM
# row or a market-view row carries no "_USAS_" marker, so a blanket stamp would
# brand a live feed retired and hide its staleness.
#
# Rule: a row is stamped retired ONLY if its tag is AG or absent (legacy
# Acquisition Gateway rows written before tagging existed). A row tagged with
# anything in LIVE_SOURCES can never be stamped, whatever its id looks like.
#
# ANY NEW WRITER MUST SET ITS OWN TAG AND ADD IT TO LIVE_SOURCES.
# Agency names observed to return at least one record during this run. Shared
# across legs so a leg with no records for an agency is not mistaken for a
# filter that does not match.
_RESOLVED_AGENCY_NAMES = set()

SOURCE_USASPENDING = "USASPENDING"
SOURCE_AG_RETIRED = "AG"
LIVE_SOURCES = {SOURCE_USASPENDING, "SAM", "MARKET"}
RETIRED_LABEL = ("Retired source — Acquisition Gateway export, Aug 13 2026, "
                 "no longer refreshed")


# ─────────────────────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────────────────────
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


def get_award_detail(award_key):
    """Fetch one award. Returns {} on any failure — enrichment is best-effort
    and must never abort the run over a single bad id."""
    if not award_key:
        return {}
    url = AWARD_DETAIL_URL.format(urllib.parse.quote(str(award_key), safe=""))
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}


def fetch_window(today, lo_days, hi_days, award_types, fields, sort_field, label,
                 lookback_days=None, agency=None, amount_tier=None, quiet=False):
    """Page one slice descending by end date, keeping the part inside [lo, hi].

    ALWAYS DESCENDING. Ascending was tried and is wrong: it enters at the
    oldest end dates, so the 10,000-record budget is spent on contracts that
    ended years ago and the walk never reaches the present.

    Descending is correct once the slice is small enough to fit the cap. It
    enters from the far future, crosses the window, and stops on the first full
    page below the floor - so the ancient tail costs nothing.

    Returns (kept, scanned, pages, reached_floor). reached_floor is the honest
    signal: False means the walk stopped for a reason other than clearing the
    window, so the result is a floor, not a measurement.
    """
    lo = today + timedelta(days=lo_days)
    hi = today + timedelta(days=hi_days)
    lookback = ACTION_LOOKBACK_DAYS if lookback_days is None else lookback_days
    kept, page, scanned, undated = [], 1, 0, 0
    reached_floor = False

    if amount_tier:
        low, high = amount_tier
        amounts = [{"lower_bound": max(low, MIN_AWARD_AMOUNT)}]
        if high is not None:
            amounts[0]["upper_bound"] = high
    else:
        amounts = [{"lower_bound": MIN_AWARD_AMOUNT}]

    filters = {
        "award_type_codes": award_types,
        "naics_codes": {"require": NAICS_PREFIXES},
        "award_amounts": amounts,
        "time_period": [{
            "start_date": (today - timedelta(days=lookback)).isoformat(),
            "end_date": (today + timedelta(days=1)).isoformat(),
            "date_type": "action_date",
        }],
    }
    # Server-side agency filter. This is the whole point of v4: the previous
    # versions sent no agency filter at all, pulled every federal agency, and
    # then discarded everything outside AGENCY_MAP in to_record(). The 10,000
    # budget was being spent largely on agencies the dashboard never renders.
    if agency:
        filters["agencies"] = [{"type": "awarding", "tier": "toptier",
                                "name": agency}]

    while page <= MAX_PAGES:
        body = {
            "subawards": False,
            "limit": PAGE_LIMIT,
            "page": page,
            "sort": sort_field,
            "order": "desc",
            "filters": filters,
            "fields": fields,
        }
        payload = post(body)
        results = payload.get("results") or []
        scanned += len(results)
        if not results:
            break

        # Records past the FAR edge of the direction of travel. Descending, that
        # is below lo; ascending, above hi. A full page of them means the walk
        # has left the window behind and can stop.
        below = 0
        for rec in results:
            end = parse_date(rec.get(sort_field))
            if not end:
                undated += 1          # counted, not silently swallowed
                continue
            if end > hi:
                continue
            if end < lo:
                below += 1
                continue
            kept.append(rec)

        if not quiet:
            print(f"  {label} page {page}: {len(results)} scanned, "
                  f"{len(kept)} in window so far")

        if below == len(results):
            reached_floor = True      # walked clear of the window: trustworthy
            break
        if not (payload.get("page_metadata") or {}).get("hasNext"):
            # hasNext false means one of two very different things, and
            # conflating them cries wolf on every small slice: either the slice
            # was genuinely exhausted (we saw everything, nothing was hidden),
            # or we ran into the API's 10,000-record ceiling mid-walk. Only the
            # second is a truncation. Distinguish by how much was scanned.
            reached_floor = scanned < API_RECORD_CAP - PAGE_LIMIT
            break
        page += 1
        time.sleep(REQUEST_PAUSE)

    if undated and not quiet:
        print(f"  {label}: {undated} record(s) had no '{sort_field}' and were skipped")
    return kept, scanned, page, reached_floor


def stratified_pick(records, cap, lo_days, hi_days, strata):
    """Spread `cap` rendered slots across the band instead of front-loading.

    Returns (shown, rest). Slots are divided evenly across `strata` equal time
    buckets; a bucket that cannot fill its share releases the remainder to the
    others, so a thin band still renders everything it has. Ordering inside a
    bucket is soonest-first, which is how the sorted input already arrives.
    """
    if strata <= 1 or len(records) <= cap:
        return records[:cap], records[cap:]

    width = (hi_days - lo_days) / strata
    buckets = [[] for _ in range(strata)]
    for r in records:
        d = r.get("daysOut") or lo_days
        i = min(int((d - lo_days) / width), strata - 1)
        buckets[max(i, 0)].append(r)

    share, shown = cap // strata, []
    # Two passes: everyone takes their share, then the leftover from thin
    # buckets is offered back round-robin so the cap is never under-spent.
    taken = [b[:share] for b in buckets]
    for t in taken:
        shown += t
    leftover = cap - len(shown)
    if leftover:
        pool = [b[len(t):] for b, t in zip(buckets, taken)]
        i = 0
        while leftover and any(pool):
            if pool[i % strata]:
                shown.append(pool[i % strata].pop(0))
                leftover -= 1
            i += 1
            if i > strata * cap:
                break
    shown.sort(key=lambda r: (r.get("daysOut") or 9999,
                              -(r.get("valueRank") or 0)))
    ids = {id(r) for r in shown}
    return shown, [r for r in records if id(r) not in ids]


def fetch_partitioned(today, lo_days, hi_days, award_types, fields, sort_field,
                      label, lookback_days=None, subpartition="amount"):
    """One bounded descending walk per agency in AGENCY_MAP.

    Each agency gets its own 10,000-record budget instead of sharing one across
    the whole federal government. Any agency that still hits the cap is split
    again by award-amount tier - applied only where measured, never
    pre-emptively.

    Prints a per-agency count table. Those counts have never been observed;
    this run is the measurement.
    """
    kept, rows = [], []
    for agency, code in AGENCY_MAP.items():
        # Names proven to resolve earlier in this run. A zero-scan slice for one
        # of them is DATA, not a broken filter.
        resolved = _RESOLVED_AGENCY_NAMES
        recs, scanned, pages, floor = fetch_window(
            today, lo_days, hi_days, award_types, fields, sort_field,
            f"{label}/{code}", lookback_days=lookback_days, agency=agency,
            quiet=True)

        # A zero-scan slice has two very different causes and the old wording
        # only admitted one of them. If this agency name has already returned
        # records in ANY leg of this run, the filter demonstrably works and a
        # zero here is the true state of the data - DoD, NSF and DOL genuinely
        # hold no IDVs in NAICS 5413/5415/5416, confirmed by direct API probe.
        # Calling that a name mismatch cries wolf on a clean zero, and a
        # warning that fires on correct data stops being read.
        if scanned == 0:
            if agency in resolved:
                print(f"  {code:<6} 0 records in this band — name resolves "
                      f"elsewhere in this run, so this is a true zero, "
                      f"not a broken filter")
                rows.append((code, 0, 0, 0, True, "none in band"))
            else:
                print(f"  {code:<6} 0 scanned, and '{agency}' has not resolved "
                      f"in ANY leg of this run — probable toptier NAME "
                      f"MISMATCH; this agency would be invisible to the feed.")
                rows.append((code, 0, 0, 0, False, "name-mismatch?"))
            continue
        resolved.add(agency)

        note = ""
        if not floor:
            # The walk stopped without clearing the window: either our page
            # ceiling or the API's record cap. Escalate on the axis that
            # actually separates this leg's population.
            axis = "award type code" if subpartition == "type" else "award amount"
            print(f"  {code:<6} hit the cap at {scanned:,} scanned — "
                  f"sub-partitioning by {axis}")
            slices = ([([t], t) for t in award_types] if subpartition == "type"
                      else [(award_types, (lo, hi)) for lo, hi in AMOUNT_TIERS])
            recs, scanned, pages, floor = [], 0, 0, True
            still = []
            for types_arg, key in slices:
                tier_arg = None if subpartition == "type" else key
                t_recs, t_scan, t_pages, t_floor = fetch_window(
                    today, lo_days, hi_days, types_arg, fields, sort_field,
                    f"{label}/{code}", lookback_days=lookback_days,
                    agency=agency, amount_tier=tier_arg, quiet=True)
                if subpartition == "type":
                    name = key
                else:
                    low, high = key
                    name = (f"${low/1e6:g}M+" if high is None
                            else f"${low/1e6:g}-{high/1e6:g}M")
                print(f"     {name:<12} {t_scan:>6,} scanned  "
                      f"{len(t_recs):>5,} in window"
                      f"{'  STILL CAPPED' if not t_floor else ''}")
                scanned += t_scan
                pages += t_pages
                if t_floor:
                    recs += t_recs
                else:
                    # A capped slice returns a PARTIAL set. Keeping it and then
                    # re-fetching the same slice split by amount would count its
                    # records twice. Drop the partial; the split supersedes it.
                    still.append((types_arg, name))
                time.sleep(REQUEST_PAUSE)

            # Second escalation: a single award type that still caps gets the
            # amount axis on top of it. Only ever reached where measured.
            if still and subpartition == "type":
                print(f"     escalating {len(still)} capped type(s) by amount")
                for types_arg, name in still:
                    for low, high in AMOUNT_TIERS:
                        t_recs, t_scan, t_pages, t_floor = fetch_window(
                            today, lo_days, hi_days, types_arg, fields,
                            sort_field, f"{label}/{code}",
                            lookback_days=lookback_days, agency=agency,
                            amount_tier=(low, high), quiet=True)
                        recs += t_recs
                        scanned += t_scan
                        pages += t_pages
                        floor = floor and t_floor
                        print(f"       {name} x "
                              f"{'$%gM+' % (low/1e6) if high is None else '$%g-%gM' % (low/1e6, high/1e6)}"
                              f"  {t_scan:>6,} scanned  {len(t_recs):>5,} in window"
                              f"{'  STILL CAPPED' if not t_floor else ''}")
                        time.sleep(REQUEST_PAUSE)
            elif still:
                # Amount was already the axis and some tier still capped: the
                # partials were dropped, so say the count is short.
                floor = False
            note = "split ok" if floor else "STILL CAPPED after split"

            # Belt and braces. Slices are disjoint by construction, so a
            # duplicate id means an assumption above is wrong; drop it rather
            # than inflate the count, and say so.
            seen, deduped = set(), []
            for r in recs:
                k = r.get("generated_internal_id") or r.get("Award ID")
                if k and k in seen:
                    continue
                if k:
                    seen.add(k)
                deduped.append(r)
            if len(deduped) != len(recs):
                print(f"     NOTE: dropped {len(recs) - len(deduped)} duplicate "
                      f"record(s) across slices")
            recs = deduped

        rows.append((code, scanned, pages, len(recs), floor, note))
        kept += recs
        time.sleep(REQUEST_PAUSE)

    print(f"\n  per-agency counts — {label}")
    print(f"     {'agency':<8}{'scanned':>10}{'pages':>7}{'in window':>11}"
          f"{'complete':>10}  note")
    for code, scanned, pages, n, floor, note in rows:
        print(f"     {code:<8}{scanned:>10,}{pages:>7}{n:>11,}"
              f"{('yes' if floor else 'NO'):>10}  {note}")
    total = sum(r[3] for r in rows)
    bad = [r[0] for r in rows if not r[4]]
    print(f"     {'TOTAL':<8}{sum(r[1] for r in rows):>10,}"
          f"{sum(r[2] for r in rows):>7}{total:>11,}")
    if bad:
        print(f"     WARNING - incomplete after sub-partitioning: "
              f"{', '.join(bad)}. Those counts are floors, not measurements.")
    return kept, rows


def parse_date(v):
    if not v:
        return None
    s = str(v)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def excluded(*parts):
    blob = " ".join(str(p or "") for p in parts).upper()
    for kw in EXCLUDED_AGENCY_KEYWORDS:
        if re.search(r"(?<![A-Z0-9])" + re.escape(kw) + r"(?![A-Z0-9])", blob):
            return kw
    return None


def money(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return ""
    for cut, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= cut:
            return f"${n/cut:,.1f}{suffix}".replace(".0", "")
    return f"${n:,.0f}"


def value_rank(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0
    return 5 if n >= 1e8 else 4 if n >= 1e7 else 3 if n >= 5e6 else \
        2 if n >= 2e6 else 1


def slug(agency, title, i):
    s = re.sub(r"[^A-Za-z0-9]+", "_", (title or "")[:26]).strip("_")
    return f"{agency}{FEED_MARKER}{s}_{i}"


def to_record(rec, today, is_idv, i, band="recompete"):
    agency_name = (rec.get("Awarding Agency") or "").strip()
    sub_agency = (rec.get("Awarding Sub Agency") or "").strip()
    title = (rec.get("Description") or "").strip() or (rec.get("Award ID") or "").strip()
    incumbent = (rec.get("Recipient Name") or "").strip()

    # Conflict screen runs FIRST, before the agency map, so DHS work bought
    # through a mapped agency cannot slip through as an ordinary record.
    kw = excluded(agency_name, sub_agency, title, incumbent)
    if kw:
        return None, f"excluded agency keyword '{kw}'"

    code = AGENCY_MAP.get(agency_name)
    if not code:
        return None, "agency not on dashboard"

    # Pre-enrichment: only one date is available per award type. Status is
    # provisional and says so; enrich_curated() replaces it with a real one.
    if is_idv:
        first_end = parse_date(rec.get("Last Date to Order"))
    else:
        first_end = parse_date(rec.get("End Date"))
    if not first_end:
        return None, "no usable end date"

    # ── Band assignment ──────────────────────────────────────────────────
    # Position Now carries ONE claim: the observed end of the current period of
    # performance, exactly as reported by the award record. No clause logic, no
    # 52.217-8/9 reading, no option-exercise judgment - the Government may
    # exercise or decline at its discretion and this feed asserts nothing about
    # that. optionEnd and competeEnd stay empty because we have not resolved
    # them and will not imply that we have.
    #
    # The wording matters: "current period of performance ends" is an observed
    # fact. "Recompetes on" would be a prediction, and a period end is not a
    # competition point.
    if band == "position":
        # Contracts only in this band, so the wording is safe to be specific.
        status = (f"Current period of performance ends "
                  f"{first_end.isoformat()} — as reported")
        label, position_now = "Position Now", True
    elif is_idv:
        status = "Ordering window — pending date resolution"
        label, position_now = "Recompete Watch", False
    else:
        status = "Recompete — pending date resolution"
        label, position_now = "Recompete Watch", False

    return {
        "id": slug(code, title, i),
        "title": title[:140],
        "org": sub_agency or agency_name,
        "status": status,
        "solDate": first_end.isoformat(),
        "daysOut": (first_end - today).days,
        "awardQtr": "",
        "value": money(rec.get("Award Amount")),
        "valueRank": value_rank(rec.get("Award Amount")),
        "naics": str(rec.get("NAICS") or "").strip()[:6],
        "naicsDesc": "",
        "setAside": "",
        "incumbent": incumbent,
        "label": label,
        "positionNow": position_now,
        "previouslyFeatured": False,
        "clearance": "",
        "location": "",
        "pocName": "",
        "pocEmail": "",
        "optionEnd": "",
        "competeEnd": "",
        "bridgeInferred": False,
        "awardId": (rec.get("Award ID") or "").strip(),
        "awardKey": (rec.get("generated_internal_id") or "").strip(),
        "vehicle": "IDV" if is_idv else "Contract",
        "source": "USASpending",
    }, None


def idv_status(status):
    """An IDV's date is the close of an ordering window, not the end of a
    period of performance. Same observed date, different event.

    The enrichment statuses draw an option-decision / final-period distinction
    that is a period-of-performance concept and does not map onto an ordering
    window at all. Flattening IDVs to one plain statement says less and asserts
    nothing untrue, which is the right trade.
    """
    return ("Ordering window closes — last date to order, as reported"
            if status.startswith(("Recompete", "Ordering")) else status)


def enrich_curated(records, today):
    """Resolve BOTH dates, then derive a status that actually means something.

        end_date           -> current period end    (option DECISION point)
        potential_end_date -> ultimate end          (MANDATORY competition point)

    Only with both in hand can the label distinguish the two. Records the API
    cannot resolve keep their provisional status and say so plainly.
    """
    stats = {"enriched": 0, "no_detail": 0, "no_potential": 0, "rewindowed": 0,
             "options_remain": 0, "final_period": 0, "bridge": 0, "unreported": 0}
    for r in records:
        detail = get_award_detail(r.get("awardKey") or r.get("awardId"))
        time.sleep(ENRICH_PAUSE)
        if not detail:
            stats["no_detail"] += 1
            r["status"] = "Recompete — period end (detail unavailable)"
            continue
        stats["enriched"] += 1

        pop = detail.get("period_of_performance") or {}
        cur = parse_date(pop.get("end_date"))
        pot = parse_date(pop.get("potential_end_date"))
        if not pot:
            stats["no_potential"] += 1

        if pot and cur and pot > cur:
            r["optionEnd"], r["competeEnd"] = cur.isoformat(), pot.isoformat()
            r["status"] = "Recompete — option decision first, competition by ultimate end"
            stats["options_remain"] += 1
            anchor = pot
        elif pot and cur and cur > pot and (cur - pot).days <= BRIDGE_MAX_DAYS:
            r["optionEnd"], r["competeEnd"] = cur.isoformat(), pot.isoformat()
            r["bridgeInferred"] = True
            r["status"] = "Recompete — possible bridge extension (follow-on likely imminent)"
            stats["bridge"] += 1
            anchor = cur
        elif pot and cur:
            # potential == current: no options remain, this really is the end.
            r["optionEnd"], r["competeEnd"] = "", cur.isoformat()
            r["status"] = "Recompete — final period end (competition point)"
            stats["final_period"] += 1
            anchor = cur
        elif cur:
            # potential_end_date is NULL. That means "not reported", not
            # "no options exist" — so do NOT assert a competition point.
            # Claiming finality on absent data is the same failure as a baked
            # date: confident-sounding, unsupported. Label the uncertainty.
            r["optionEnd"], r["competeEnd"] = "", ""
            r["status"] = "Recompete — period end (options not reported)"
            stats["unreported"] += 1
            anchor = cur
        else:
            r["status"] = "Recompete — period end (no dates returned)"
            continue

        new_days = (anchor - today).days
        if not (WINDOW_MIN_DAYS <= new_days <= WINDOW_MAX_DAYS):
            stats["rewindowed"] += 1
        r["solDate"] = anchor.isoformat()
        r["daysOut"] = new_days
        if r.get("vehicle") == "IDV":
            r["status"] = idv_status(r["status"])
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# index.html read / write
# ─────────────────────────────────────────────────────────────────────────────
def quote_js_keys(js):
    """JS object-literal -> strict JSON, string-aware. A regex would corrupt
    any value containing ", word:" — real contract descriptions do."""
    out, i, n = [], 0, len(js)
    in_str, quote = False, ""
    while i < n:
        c = js[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(js[i + 1]); i += 2; continue
            if c == quote:
                in_str = False
            i += 1
            continue
        if c in "\"'":
            in_str, quote = True, c
            out.append('"'); i += 1
            continue
        m = re.match(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*:", js[i:])
        if m and (not out or out[-1].strip()[-1:] in "{,[" or out[-1] in "{,"):
            out.append('"' + m.group(1) + '":'); i += m.end()
            continue
        out.append(c); i += 1
    return re.sub(r",(\s*[\]}])", r"\1", "".join(out))


def locate_agency_data(html):
    """Find the AGENCY_DATA block, skipping braces inside string literals.

    Naive brace counting breaks once record values contain '{' or '}' inside
    a quoted string (USASpending titles/descriptions do — verified 2026-08-20
    on the 220-record build: 8,698 '{' vs 4,362 '}' in the 4,336 build, and
    the naive counter fails on the curated 288 KB build too). Track quote
    state so only structural braces are counted.
    """
    m = re.search(r"const AGENCY_DATA\s*=\s*\{", html)
    if not m:
        raise SystemExit("AGENCY_DATA not found in index.html")
    start = html.index("{", m.start())
    depth = 0
    in_str = False
    esc = False
    for j in range(start, len(html)):
        c = html[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return m.start(), start, j + 1
    raise SystemExit("AGENCY_DATA braces unbalanced")


FIELD_ORDER = ["id", "title", "org", "status", "solDate", "daysOut", "awardQtr",
               "value", "valueRank", "naics", "naicsDesc", "setAside",
               "incumbent", "label", "positionNow", "previouslyFeatured",
               "clearance", "location", "pocName", "pocEmail",
               "optionEnd", "competeEnd", "bridgeInferred", "awardId",
               "vehicle", "source"]


def render_agency_data(data):
    def cell(rec, f):
        v = rec.get(f, False if f in ("positionNow", "previouslyFeatured",
                                      "bridgeInferred")
                    else (0 if f in ("valueRank", "daysOut") else ""))
        if isinstance(v, bool):
            return f"{f}:{'true' if v else 'false'}"
        if isinstance(v, (int, float)):
            return f"{f}:{v}"
        return f"{f}:{json.dumps(v if v is not None else '', ensure_ascii=False)}"

    lines = ["const AGENCY_DATA = {"]
    for agency, recs in data.items():
        lines.append(f'  "{agency}": [')
        lines.append(",\n".join(
            "    {" + ",".join(cell(r, f) for f in FIELD_ORDER) + "}"
            for r in recs))
        lines.append("  ],")
    lines.append("};")
    return "\n".join(lines)


def write_search_index(records, curated_ids):
    """Write EVERY record to forecast-index.json.

        [id, agency, date, naics, value, title, incumbent, onDashboardFlag,
         status, source]

    Older rows written before the status field exist with 8 elements; the
    renderer treats a missing r[8] as "no status" rather than breaking.

    Rows from this feed are replaced wholesale; rows from any other source
    (the Acquisition Gateway forecast archive) are preserved untouched.
    """
    try:
        with open(INDEX_JSON, encoding="utf-8") as f:
            existing = json.load(f)
    except (OSError, ValueError):
        existing = []

    def source_of(row):
        return row[9] if len(row) > 9 and row[9] else ""

    # Rows from this feed are replaced wholesale. Everything else is preserved,
    # and stamped retired ONLY when it is genuinely legacy Acquisition Gateway.
    preserved, stamped, left_alone = [], 0, 0
    for row in existing:
        if source_of(row) == SOURCE_USASPENDING or (
                not source_of(row) and FEED_MARKER in str(row[0])):
            continue                          # ours — about to be rewritten
        row = list(row) + [""] * (10 - len(row))
        if source_of(row) in LIVE_SOURCES:
            left_alone += 1                   # a live feed: NEVER stamp retired
        else:
            row[8] = RETIRED_LABEL
            row[9] = SOURCE_AG_RETIRED
            stamped += 1
        preserved.append(row)

    # Ninth field is the STATUS, so a lookup on a call reads out the same
    # qualification the dashboard shows. Archived records were never enriched,
    # so they say so explicitly rather than presenting a bare date that could
    # be mistaken for a competition point.
    def status_for(r):
        if r["id"] in curated_ids:
            return r.get("status") or ""
        # An over-cap Position Now row is trimmed from the DISPLAY, never
        # discarded: it stays searchable so a cited notice remains findable.
        # It gets its own caveat rather than the recompete one, which would
        # describe a qualification this row never went through.
        if r.get("label") == "Position Now":
            return ("Archive — current period of performance end, "
                    "not rendered on the page")
        # The archived caveat has to respect the same distinction the rendered
        # rows now do. Roughly 1,400 archived IDVs would otherwise carry
        # "current period end" wording, which is the exact conflation the
        # relabel exists to remove - moved out of sight into the search index
        # rather than fixed.
        if r.get("vehicle") == "IDV":
            return "Archive — last date to order, not verified"
        return "Archive — current period end, options not verified"

    rows = [[r["id"],
             r["id"].split("_")[0],
             r.get("competeEnd") or r.get("optionEnd") or r.get("solDate") or "",
             r.get("naics") or "",
             (r.get("value") or "")[:26],
             (r.get("title") or "")[:96],
             (r.get("incumbent") or "")[:40],
             1 if r["id"] in curated_ids else 0,
             status_for(r),
             SOURCE_USASPENDING]
            for r in records]

    with open(INDEX_JSON, "w", encoding="utf-8") as f:
        json.dump(preserved + rows, f, separators=(",", ":"))

    print(f"   layers: {len(rows)} USASPENDING (live), "
          f"{stamped} AG (retired, stamped), "
          f"{left_alone} other live row(s) left untouched")
    return len(preserved), len(rows)


# ─────────────────────────────────────────────────────────────────────────────
def main():
    today = local_today()
    runner_today = date.today()
    if today != runner_today:
        print(f"note: runner is {runner_today} (UTC), audience date {today} "
              f"(US Central). Using the audience date.")

    rc_lo = today + timedelta(days=WINDOW_MIN_DAYS)
    rc_hi = today + timedelta(days=WINDOW_MAX_DAYS)
    pn_lo = today + timedelta(days=POSITION_MIN_DAYS)
    pn_hi = today + timedelta(days=POSITION_MAX_DAYS)
    print(f"Recompete Watch window : {rc_lo} .. {rc_hi} "
          f"({WINDOW_MIN_DAYS}-{WINDOW_MAX_DAYS} days out)")
    print(f"Position Now window    : {pn_lo} .. {pn_hi} "
          f"({POSITION_MIN_DAYS}-{POSITION_MAX_DAYS} days out)  [NEW]")
    print(f"NAICS prefixes         : {', '.join(NAICS_PREFIXES)}")
    print(f"display caps           : recompete {DISPLAY_CAP} "
          f"(remainder stays searchable) / position {POSITION_CAP}")

    base_fields = ["Award ID", "Recipient Name", "Awarding Agency",
                   "Awarding Sub Agency", "Description", "NAICS",
                   "Award Amount", "generated_internal_id"]
    c_fields = base_fields + ["Start Date", "End Date"]
    i_fields = base_fields + ["Start Date", "Last Date to Order"]

    # Each band gets its own bounded walk. A single 60-545 fetch would page
    # from 545 down to 60, so the near band would sit at the very end of the
    # walk and be the first thing lost to MAX_PAGES.
    print(f"\npartitioned by agency: {len(AGENCY_MAP)} agencies, one bounded "
          f"descending walk each (API record cap {API_RECORD_CAP:,} per slice)")

    print("\n── Recompete Watch band ──")
    print("querying contracts (A,B,C,D)...")
    contracts, rc_c_rows = fetch_partitioned(
        today, WINDOW_MIN_DAYS, WINDOW_MAX_DAYS,
        CONTRACT_TYPES, c_fields, "End Date", "recompete/contracts")
    print("\nquerying IDVs...")
    idvs, rc_i_rows = fetch_partitioned(
        today, WINDOW_MIN_DAYS, WINDOW_MAX_DAYS,
        IDV_TYPES, i_fields, "Last Date to Order", "recompete/IDVs",
        subpartition=SUBPARTITION_BY_TYPE)
    print(f"\nin recompete window: {len(contracts)} contracts, {len(idvs)} IDVs")
    # Was the old unpartitioned 220 curated from a truncated scan? The tell is
    # the near edge of the window. A complete descending walk reaches 180 days;
    # a truncated one stops short and the earliest record sits well above it.
    rc_all = [parse_date(r.get("End Date")) for r in contracts] + \
             [parse_date(r.get("Last Date to Order")) for r in idvs]
    rc_all = [d for d in rc_all if d]
    if rc_all:
        nearest = min((d - today).days for d in rc_all)
        print(f"   nearest record in the recompete band: {nearest} days out "
              f"(floor is {WINDOW_MIN_DAYS})")
        if nearest > WINDOW_MIN_DAYS + 5:
            print(f"   NOTE: still {nearest - WINDOW_MIN_DAYS} days short of "
                  f"the floor — check the per-agency 'complete' column above.")

    print("\n── Position Now band — CONTRACTS ONLY ──")
    print(f"   descending walk · action lookback "
          f"{POSITION_ACTION_LOOKBACK_DAYS}d (recompete uses "
          f"{ACTION_LOOKBACK_DAYS}d)")
    print("   IDVs are deliberately OUT of this band: a Last Date to Order is")
    print("   an ordering window closing, not a period of performance ending.")
    print("   They stay in Recompete Watch and are labelled as what they are.")
    print("querying contracts (A,B,C,D)...")
    pn_contracts, pn_c_rows = fetch_partitioned(
        today, POSITION_MIN_DAYS, POSITION_MAX_DAYS,
        POSITION_AWARD_TYPES, c_fields, "End Date", "position/contracts",
        lookback_days=POSITION_ACTION_LOOKBACK_DAYS)
    pn_idvs, pn_i_rows = [], []
    print(f"\nBAND MEASUREMENT — raw in {POSITION_MIN_DAYS}-{POSITION_MAX_DAYS} "
          f"day band: {len(pn_contracts)} contracts (IDVs out of scope by design)")
    # Continuity check. The two bands are one population at two calendar
    # positions: a contract 8 months out becomes a contract 4 months out. A
    # cliff between them is a query artefact, not a thin market. Flag it here
    # rather than letting an empty-looking section pass as a finding.
    incomplete = [r[0] for r in rc_c_rows + rc_i_rows + pn_c_rows + pn_i_rows
                  if not r[4]]
    if incomplete:
        print(f"   INCOMPLETE partitions this run: "
              f"{', '.join(sorted(set(incomplete)))}")
    # Contracts against contracts. Comparing a contracts-only band to a mixed
    # one would build the scoping decision into the health check and hide a
    # real regression behind it.
    rc_raw, pn_raw = len(contracts), len(pn_contracts)
    rc_rate = rc_raw / (WINDOW_MAX_DAYS - WINDOW_MIN_DAYS)
    pn_rate = pn_raw / (POSITION_MAX_DAYS - POSITION_MIN_DAYS)
    print(f"   density (contracts only): recompete {rc_rate:.1f}/day · "
          f"position {pn_rate:.1f}/day")
    print(f"   recompete IDVs: {len(idvs)} (own leg, scoped to this band)")
    if rc_rate and pn_rate < rc_rate * 0.25:
        print(f"   WARNING - the near band is running at "
              f"{pn_rate / rc_rate:.0%} of the recompete band's density. "
              f"These are the same population at different calendar "
              f"positions; a gap this size points at the query, not the "
              f"market. Check the MAX_PAGES warning above and the action "
              f"lookback before accepting this count.")

    records, skipped = [], {}
    for i, rec in enumerate(contracts):
        r, why = to_record(rec, today, False, i)
        (records.append(r) if r else skipped.__setitem__(
            why, skipped.get(why, 0) + 1))
    for i, rec in enumerate(idvs):
        r, why = to_record(rec, today, True, 10000 + i)
        (records.append(r) if r else skipped.__setitem__(
            why, skipped.get(why, 0) + 1))

    # Index offsets keep slug() ids distinct across bands.
    position, pn_skipped = [], {}
    for i, rec in enumerate(pn_contracts):
        r, why = to_record(rec, today, False, 20000 + i, band="position")
        (position.append(r) if r else pn_skipped.__setitem__(
            why, pn_skipped.get(why, 0) + 1))
    for i, rec in enumerate(pn_idvs):
        r, why = to_record(rec, today, True, 30000 + i, band="position")
        (position.append(r) if r else pn_skipped.__setitem__(
            why, pn_skipped.get(why, 0) + 1))

    # Scope assertion. Cheap, and it fails loudly rather than letting an IDV
    # appear under period-of-performance wording months from now.
    strays = [r for r in position if r.get("vehicle") != "Contract"]
    if strays:
        raise SystemExit(
            f"ABORT: {len(strays)} non-contract record(s) reached the Position "
            f"Now band. That band is contracts-only by design. Leaving "
            f"index.html unchanged.")

    print(f"\nmapped to dashboard records: {len(records)} recompete, "
          f"{len(position)} position (contracts only)")
    for why, n in sorted(skipped.items(), key=lambda kv: -kv[1]):
        print(f"   recompete skipped {n}: {why}")
    for why, n in sorted(pn_skipped.items(), key=lambda kv: -kv[1]):
        print(f"   position  skipped {n}: {why}")
    if not records:
        raise SystemExit(
            "ABORT: USASpending returned no usable records. Leaving "
            "index.html unchanged rather than emptying Recompete Watch.")

    with_inc = sum(1 for r in records if r["incumbent"])
    idv_n = sum(1 for r in records if r["vehicle"] == "IDV")
    print(f"   with named incumbent      : {with_inc}")
    print(f"   IDV / Contract            : {idv_n} / {len(records)-idv_n}")

    # ---- curate: highest value first, then soonest expiry ----
    records.sort(key=lambda r: (-(r.get("valueRank") or 0),
                                r.get("daysOut") or 9999))
    curated = records[:DISPLAY_CAP]
    archived = records[DISPLAY_CAP:]
    curated_ids = {r["id"] for r in curated}
    print(f"\ncuration: {len(curated)} rendered on the page, "
          f"{len(archived)} archived to search "
          f"(display trimmed, nothing discarded)")

    # ---- curate Position Now: SOONEST first, then value ----
    # Deliberately the inverse of the recompete sort. The question this band
    # answers is "what closes first", not "what is biggest".
    position.sort(key=lambda r: (r.get("daysOut") or 9999,
                                 -(r.get("valueRank") or 0)))
    position_shown, position_over = stratified_pick(position, POSITION_CAP,
                                                    POSITION_MIN_DAYS,
                                                    POSITION_MAX_DAYS,
                                                    POSITION_STRATA)
    curated_ids |= {r["id"] for r in position_shown}
    print(f"Position Now: {len(position_shown)} rendered "
          f"(cap {POSITION_CAP})")
    if position_over:
        # No silent caps. Name what was trimmed and what it cost.
        last = position_shown[-1]["daysOut"] if position_shown else "-"
        print(f"   NOTE: {len(position_over)} record(s) over the cap are "
              f"archived to search, not rendered. Rendered rows end within "
              f"{last} days; the rest end later, up to "
              f"{position_over[-1]['daysOut']} days. Raise POSITION_CAP to "
              f"widen the rendered set.")
    if position_shown:
        span = (position_shown[0]["daysOut"], position_shown[-1]["daysOut"])
        print(f"   days-out span rendered   : {span[0]} .. {span[1]} "
              f"(band is {POSITION_MIN_DAYS}-{POSITION_MAX_DAYS}, "
              f"{POSITION_STRATA} strata)")
        if span[1] - span[0] < (POSITION_MAX_DAYS - POSITION_MIN_DAYS) / 3:
            print(f"   NOTE: rendered rows cover only {span[1]-span[0]} days of "
                  f"a {POSITION_MAX_DAYS-POSITION_MIN_DAYS}-day band. The "
                  f"section label is wider than what it shows.")
        by_agency = {}
        for r in position_shown:
            by_agency[r["id"].split("_")[0]] = \
                by_agency.get(r["id"].split("_")[0], 0) + 1
        print("   by agency                : " + ", ".join(
            f"{k} {v}" for k, v in sorted(by_agency.items(),
                                          key=lambda kv: -kv[1])))

    # ---- enrich the curated set so status is real, not a proxy for type ----
    if ENRICH_CURATED and curated:
        print(f"\nenriching {len(curated)} curated records with potential_end_date "
              f"(~{len(curated)*ENRICH_PAUSE:.0f}s)...")
        est = enrich_curated(curated, today)
        print(f"   detail fetched            : {est['enriched']}")
        print(f"   detail unavailable        : {est['no_detail']}")
        print(f"   no potential_end_date     : {est['no_potential']}")
        print(f"   anchor moved out of window: {est['rewindowed']}")
        have = est['options_remain'] + est['final_period'] + est['bridge']
        print(f"\n   WITH potential_end_date   : {have}")
        print(f"      options remain         : {est['options_remain']}")
        print(f"      final period           : {est['final_period']}")
        print(f"      bridge extension       : {est['bridge']}")
        print(f"   WITHOUT potential_end_date: {est['unreported'] + est['no_detail']}")
        print(f"      options not reported   : {est['unreported']}")
        print(f"      detail unavailable     : {est['no_detail']}")
        dist = {}
        for r in curated:
            dist[r["status"]] = dist.get(r["status"], 0) + 1
        print("   status distribution after enrichment:")
        for k, v in sorted(dist.items(), key=lambda kv: -kv[1]):
            print(f"      {v:>4}  {k}")
        bridges = sum(1 for r in curated if r["bridgeInferred"])
        print(f"   possible 52.217-8 bridges : {bridges} (inferred, not asserted)")
        curated.sort(key=lambda r: (-(r.get("valueRank") or 0),
                                    r.get("daysOut") or 9999))

    # ---- search index: every record, curated flag set ----
    # Every position record goes to the index, not just the rendered ones.
    preserved, written = write_search_index(records + position, curated_ids)
    print(f"\nforecast-index.json: {preserved} non-feed row(s) preserved, "
          f"{written} written, {preserved + written} total searchable")

    # ---- splice curated set into AGENCY_DATA ----
    html = open(INDEX, encoding="utf-8").read()
    head, start, end = locate_agency_data(html)
    existing = json.loads(quote_js_keys(html[start:end]))

    # Labels this feed OWNS and therefore clears before re-adding. Without
    # "Position Now" here the new rows would accumulate on every nightly run.
    #
    # "Monitor" and "Too Early" are included because they are retired rungs.
    # They existed only to bucket Acquisition Gateway's multi-year forecast
    # dates; on an observed period-end axis they would sit directly on top of
    # the Recompete window - the same records under a second name. The only
    # rows still carrying them are the frozen, untagged AG leftovers, which
    # this clears out.
    replaced_labels = {"Recompete Watch", "Position Now", "Monitor", "Too Early"}

    # Fail-safe: an empty near band must not silently blank the section. Leave
    # the existing Position Now rows in place, shout, and let the recompete
    # half of the run proceed.
    if not position_shown:
        replaced_labels -= {"Position Now", "Monitor", "Too Early"}
        print("\n   WARNING: the Position Now band returned NO records. "
              "Existing Position Now rows left untouched rather than writing "
              "an empty section. Investigate before trusting the page.")

    removed = 0
    merged = {}
    for agency, recs in existing.items():
        keep = [r for r in recs if r.get("label") not in replaced_labels]
        removed += len(recs) - len(keep)
        merged[agency] = keep

    for r in curated:
        merged.setdefault(r["id"].split("_")[0], []).append(r)
    for r in position_shown:
        merged.setdefault(r["id"].split("_")[0], []).append(r)

    for agency in merged:
        merged[agency].sort(key=lambda r: (-(r.get("valueRank") or 0),
                                           r.get("daysOut") or 9999))

    print(f"\nsplice: removed {removed} previous record(s) carrying "
          f"{sorted(replaced_labels)}")
    print(f"   Recompete Watch added    : {len(curated)}")
    print(f"   Position Now added       : {len(position_shown)}")

    html = html[:head] + render_agency_data(merged) + html[end:]
    # DATA_AS_OF tracks the content just written: every run stamps the label
    # with the run's date so the "As of" label can never lag the data.
    html = re.sub(r"DATA_AS_OF_ISO\s*=\s*'[^']*'",
                  f"DATA_AS_OF_ISO = '{today.isoformat()}'",
                  html, count=1)
    open(INDEX, "w", encoding="utf-8").write(html)

    size_kb = os.path.getsize(INDEX) / 1024
    json_kb = os.path.getsize(INDEX_JSON) / 1024
    print(f"index.html updated — {sum(len(v) for v in merged.values())} "
          f"records across {len(merged)} agencies")
    print(f"   index.html          : {size_kb:,.0f} KB")
    print(f"   forecast-index.json : {json_kb:,.0f} KB (lazy-loaded)")
    if size_kb > 400:
        print("   WARNING: index.html above 400 KB — lower DISPLAY_CAP.")


if __name__ == "__main__":
    main()
