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


# ── Positioning window ───────────────────────────────────────────────────────
WINDOW_MIN_DAYS = 180
WINDOW_MAX_DAYS = 545

# ── Display curation ─────────────────────────────────────────────────────────
DISPLAY_CAP = 220

# ── Enrichment ───────────────────────────────────────────────────────────────
ENRICH_CURATED = True
ENRICH_PAUSE = 0.25

NAICS_PREFIXES = ["5413", "5415", "5416"]
MIN_AWARD_AMOUNT = 250_000
ACTION_LOOKBACK_DAYS = 730

CONTRACT_TYPES = ["A", "B", "C", "D"]
IDV_TYPES = ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C",
             "IDV_C", "IDV_D", "IDV_E"]

PAGE_LIMIT = 100
MAX_PAGES = 100
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


def fetch_window(today, award_types, fields, sort_field, label):
    lo = today + timedelta(days=WINDOW_MIN_DAYS)
    hi = today + timedelta(days=WINDOW_MAX_DAYS)
    kept, page, scanned, undated = [], 1, 0, 0

    while page <= MAX_PAGES:
        body = {
            "subawards": False,
            "limit": PAGE_LIMIT,
            "page": page,
            "sort": sort_field,
            "order": "desc",
            "filters": {
                "award_type_codes": award_types,
                "naics_codes": {"require": NAICS_PREFIXES},
                "award_amounts": [{"lower_bound": MIN_AWARD_AMOUNT}],
                "time_period": [{
                    "start_date": (today - timedelta(days=ACTION_LOOKBACK_DAYS)).isoformat(),
                    "end_date": (today + timedelta(days=1)).isoformat(),
                    "date_type": "action_date",
                }],
            },
            "fields": fields,
        }
        payload = post(body)
        results = payload.get("results") or []
        scanned += len(results)
        if not results:
            break

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

        print(f"  {label} page {page}: {len(results)} scanned, "
              f"{len(kept)} in window so far")

        if below == len(results):
            break
        if not (payload.get("page_metadata") or {}).get("hasNext"):
            break
        page += 1
        time.sleep(REQUEST_PAUSE)

    if undated:
        print(f"  {label}: {undated} record(s) had no '{sort_field}' and were skipped")
    return kept, scanned, page


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


def to_record(rec, today, is_idv, i):
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

    return {
        "id": slug(code, title, i),
        "title": title[:140],
        "org": sub_agency or agency_name,
        "status": "Recompete — pending date resolution",
        "solDate": first_end.isoformat(),
        "daysOut": (first_end - today).days,
        "awardQtr": "",
        "value": money(rec.get("Award Amount")),
        "valueRank": value_rank(rec.get("Award Amount")),
        "naics": str(rec.get("NAICS") or "").strip()[:6],
        "naicsDesc": "",
        "setAside": "",
        "incumbent": incumbent,
        "label": "Recompete Watch",
        "positionNow": False,
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
         status]

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

    preserved = [e for e in existing if not (e and FEED_MARKER in str(e[0]))]

    # Ninth field is the STATUS, so a lookup on a call reads out the same
    # qualification the dashboard shows. Archived records were never enriched,
    # so they say so explicitly rather than presenting a bare date that could
    # be mistaken for a competition point.
    def status_for(r):
        if r["id"] in curated_ids:
            return r.get("status") or ""
        return "Archive — current period end, options not verified"

    rows = [[r["id"],
             r["id"].split("_")[0],
             r.get("competeEnd") or r.get("optionEnd") or r.get("solDate") or "",
             r.get("naics") or "",
             (r.get("value") or "")[:26],
             (r.get("title") or "")[:96],
             (r.get("incumbent") or "")[:40],
             1 if r["id"] in curated_ids else 0,
             status_for(r)]
            for r in records]

    with open(INDEX_JSON, "w", encoding="utf-8") as f:
        json.dump(preserved + rows, f, separators=(",", ":"))

    return len(preserved), len(rows)


# ─────────────────────────────────────────────────────────────────────────────
def main():
    today = local_today()
    runner_today = date.today()
    if today != runner_today:
        print(f"note: runner is {runner_today} (UTC), audience date {today} "
              f"(US Central). Using the audience date.")

    lo = today + timedelta(days=WINDOW_MIN_DAYS)
    hi = today + timedelta(days=WINDOW_MAX_DAYS)
    print(f"positioning window: {lo} .. {hi} "
          f"({WINDOW_MIN_DAYS}-{WINDOW_MAX_DAYS} days out)")
    print(f"NAICS prefixes    : {', '.join(NAICS_PREFIXES)}")
    print(f"display cap       : {DISPLAY_CAP} (remainder stays searchable)")

    base_fields = ["Award ID", "Recipient Name", "Awarding Agency",
                   "Awarding Sub Agency", "Description", "NAICS",
                   "Award Amount", "generated_internal_id"]

    print("\nquerying contracts (A,B,C,D)...")
    contracts, c_scanned, c_pages = fetch_window(
        today, CONTRACT_TYPES, base_fields + ["Start Date", "End Date"],
        "End Date", "contracts")

    print("\nquerying IDVs...")
    idvs, i_scanned, i_pages = fetch_window(
        today, IDV_TYPES, base_fields + ["Start Date", "Last Date to Order"],
        "Last Date to Order", "IDVs")

    print(f"\nscanned {c_scanned} contracts over {c_pages} page(s), "
          f"{i_scanned} IDVs over {i_pages} page(s)")
    print(f"in positioning window: {len(contracts)} contracts, {len(idvs)} IDVs")

    records, skipped = [], {}
    for i, rec in enumerate(contracts):
        r, why = to_record(rec, today, False, i)
        (records.append(r) if r else skipped.__setitem__(
            why, skipped.get(why, 0) + 1))
    for i, rec in enumerate(idvs):
        r, why = to_record(rec, today, True, 10000 + i)
        (records.append(r) if r else skipped.__setitem__(
            why, skipped.get(why, 0) + 1))

    print(f"\nmapped to dashboard records: {len(records)}")
    for why, n in sorted(skipped.items(), key=lambda kv: -kv[1]):
        print(f"   skipped {n}: {why}")
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
    preserved, written = write_search_index(records, curated_ids)
    print(f"\nforecast-index.json: {preserved} non-feed row(s) preserved, "
          f"{written} written, {preserved + written} total searchable")

    # ---- splice curated set into AGENCY_DATA ----
    html = open(INDEX, encoding="utf-8").read()
    head, start, end = locate_agency_data(html)
    existing = json.loads(quote_js_keys(html[start:end]))

    removed = 0
    merged = {}
    for agency, recs in existing.items():
        keep = [r for r in recs if r.get("label") != "Recompete Watch"]
        removed += len(recs) - len(keep)
        merged[agency] = keep

    for r in curated:
        merged.setdefault(r["id"].split("_")[0], []).append(r)

    for agency in merged:
        merged[agency].sort(key=lambda r: (-(r.get("valueRank") or 0),
                                           r.get("daysOut") or 9999))

    print(f"\nRecompete Watch: removed {removed} previous record(s), "
          f"added {len(curated)} from USASpending")

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
