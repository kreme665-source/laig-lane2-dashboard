#!/usr/bin/env python3
"""
refresh_usaspending.py — rebuild Recompete Watch from USASpending (FPDS).

Replaces the static DOE OSBP snapshot. Runs server-side in GitHub Actions.
No API key, no quota — USASpending is a fully open endpoint.

WHY CONTRACT END DATES, NOT FORECAST DATES
    Agency forecast dates are projections and they slip: in the Aug 2026
    Acquisition Gateway export, 1,400 of 1,504 rows were already past their own
    published solicitation date. A period-of-performance end date is different
    in kind — it is a term the Government already committed to and reported to
    FPDS. It does not slip. Anchoring the recompete pipeline on observed end
    dates is therefore more reliable than anchoring it on forecasts.

WHAT THIS SCRIPT WILL NOT DO
    It does not predict whether an option will be exercised. The Government may
    exercise or decline at its discretion, the RFO text at 17.204-1 relaxed the
    prior "most advantageous method" test, and the 5-year services ceiling is a
    default that agency supplements and approved acquisition strategies can
    exceed. Any computed "likelihood" would be a guess wearing a data costume.
    So: observed dates only, clearly labelled as to which kind.

TWO DATES PER RECORD
    optionEnd    Period of Performance Current End Date.
                 An option DECISION point. Government discretion. Weak signal.
    competeEnd   Ordering-period / ultimate end (Last Date to Order for IDVs,
                 End Date for contracts). A MANDATORY competition point.
    Positioning is anchored on competeEnd where one exists, because that is the
    date a competition must occur by. Both are written to the record so the
    dashboard can show them separately.

52.217-8 BRIDGE INFERENCE
    FPDS does not report clause numbers, so a 217-8 "Option to Extend Services"
    cannot be read directly. It can be *inferred*: when performance is running
    PAST the award's planned end by a period within the clause's six-month cap,
    that is the shape of a bridge extension — and a bridge usually means the
    follow-on solicitation is late and imminent. Flagged as inferred, never as
    fact. Set INFER_BRIDGES = False to disable.

API SHAPE — read before changing the query
    period_of_performance_current_end_date is NOT a valid `date_type`. The award
    search time_period filter accepts only action_date, date_signed,
    last_modified_date and new_awards_only. The end-date window is therefore
    applied CLIENT-SIDE, after sorting the result set by end date descending.
    Do not "optimise" this into a server-side date filter; it does not exist.
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

API_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"

# Dates resolve in the audience's timezone, not the UTC runner's. Same reason
# as refresh_sam.py: a 00:15 UTC job would otherwise stamp tomorrow's date.
LOCAL_TZ = ZoneInfo("America/Chicago")


def local_today():
    return datetime.now(LOCAL_TZ).date()


# ── Positioning window ───────────────────────────────────────────────────────
# A services recompete solicitation typically drops 6-18 months before the
# incumbent's contract ends. Outside that window a record is not actionable.
WINDOW_MIN_DAYS = 180
WINDOW_MAX_DAYS = 545

NAICS_PREFIXES = ["5413", "5415", "5416"]   # engineering, IT, professional svcs
MIN_AWARD_AMOUNT = 250_000                  # skip micro-purchases
ACTION_LOOKBACK_DAYS = 730                  # only awards with recent activity

CONTRACT_TYPES = ["A", "B", "C", "D"]
IDV_TYPES = ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C",
             "IDV_C", "IDV_D", "IDV_E"]

PAGE_LIMIT = 100
MAX_PAGES = 100          # 10,000 records — the documented practical ceiling
REQUEST_PAUSE = 0.25
INFER_BRIDGES = True
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

# Awarding agency name -> dashboard agency code. Anything unmapped is skipped:
# adding an agency to the dashboard requires Phil's explicit approval per SOP.
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
        detail = e.read().decode("utf-8", "replace")[:500]
        raise SystemExit(f"USASpending HTTP {e.code}: {detail}")
    except Exception as e:
        raise SystemExit(f"USASpending request failed: {e}")


def fetch_window(today, award_types, fields, sort_field, label):
    """Page sorted by end date descending, collecting the positioning window.

    Sorted descending means we walk from the furthest-future end dates down.
    Once we drop below the window we can stop — everything after is nearer-term
    and already outside scope.
    """
    lo = today + timedelta(days=WINDOW_MIN_DAYS)
    hi = today + timedelta(days=WINDOW_MAX_DAYS)
    kept, page, scanned = [], 1, 0

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
                continue
            if end > hi:
                continue                    # still above the window
            if end < lo:
                below += 1
                continue                    # past it
            kept.append(rec)

        print(f"  {label} page {page}: {len(results)} scanned, "
              f"{len(kept)} in window so far")

        # Sorted descending: once a whole page sits below the window, stop.
        if below == len(results):
            break
        if not (payload.get("page_metadata") or {}).get("hasNext"):
            break
        page += 1
        time.sleep(REQUEST_PAUSE)

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
    return f"{agency}_USAS_{s}_{i}"


def to_record(rec, today, is_idv, i):
    """Map an FPDS award onto the dashboard's record schema."""
    agency_name = (rec.get("Awarding Agency") or "").strip()
    sub_agency = (rec.get("Awarding Sub Agency") or "").strip()
    title = (rec.get("Description") or "").strip() or (rec.get("Award ID") or "").strip()
    incumbent = (rec.get("Recipient Name") or "").strip()

    # Conflict screen runs FIRST, before the agency map. A DHS component bought
    # through a mapped agency (Coast Guard work on a GSA vehicle) would
    # otherwise slip through as an ordinary GSA record.
    kw = excluded(agency_name, sub_agency, title, incumbent)
    if kw:
        return None, f"excluded agency keyword '{kw}'"

    code = AGENCY_MAP.get(agency_name)
    if not code:
        return None, "agency not on dashboard"

    option_end = parse_date(rec.get("Period of Performance Current End Date"))
    compete_end = parse_date(rec.get("Last Date to Order") if is_idv
                             else rec.get("End Date"))

    # Anchor on the mandatory competition point where one exists.
    anchor = compete_end or option_end
    if not anchor:
        return None, "no usable end date"

    # Bridge inference: performance running past the planned end, within the
    # six-month cap 52.217-8 imposes. Inferred from dates only, never asserted.
    bridge = False
    if (INFER_BRIDGES and option_end and compete_end
            and option_end > compete_end
            and (option_end - compete_end).days <= BRIDGE_MAX_DAYS):
        bridge = True
        anchor = option_end

    if bridge:
        status = "Recompete — possible bridge extension"
    elif compete_end:
        status = "Recompete — competition point"
    else:
        status = "Recompete — option decision point"

    return {
        "id": slug(code, title, i),
        "title": title[:140],
        "org": sub_agency or agency_name,
        "status": status,
        "solDate": anchor.isoformat(),
        "daysOut": (anchor - today).days,
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
        # Extra fields — inert until the renderer uses them. Two dates, kept
        # separate on purpose so the dashboard never conflates a discretionary
        # option decision with a mandatory competition.
        "optionEnd": option_end.isoformat() if option_end else "",
        "competeEnd": compete_end.isoformat() if compete_end else "",
        "bridgeInferred": bridge,
        "awardId": (rec.get("Award ID") or "").strip(),
        "vehicle": "IDV" if is_idv else "Contract",
        "source": "USASpending",
    }, None


# ─────────────────────────────────────────────────────────────────────────────
# index.html read / write
# ─────────────────────────────────────────────────────────────────────────────
def quote_js_keys(js):
    """JS object-literal -> strict JSON, string-aware.

    A regex like ([{,]\\s*)(\\w+)\\s*: corrupts any value containing
    ", word:" — which real contract descriptions do contain. Character walk.
    """
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
    m = re.search(r"const AGENCY_DATA\s*=\s*\{", html)
    if not m:
        raise SystemExit("AGENCY_DATA not found in index.html")
    start = html.index("{", m.start())
    depth = 0
    for j in range(start, len(html)):
        if html[j] == "{":
            depth += 1
        elif html[j] == "}":
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

    base_fields = ["Award ID", "Recipient Name", "Awarding Agency",
                   "Awarding Sub Agency", "Description", "NAICS",
                   "Award Amount", "Period of Performance Current End Date"]

    print("\nquerying contracts (A,B,C,D)...")
    contracts, c_scanned, c_pages = fetch_window(
        today, CONTRACT_TYPES, base_fields + ["End Date", "Start Date"],
        "Period of Performance Current End Date", "contracts")

    print("\nquerying IDVs...")
    idvs, i_scanned, i_pages = fetch_window(
        today, IDV_TYPES, base_fields + ["Last Date to Order"],
        "Period of Performance Current End Date", "IDVs")

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

    bridges = sum(1 for r in records if r["bridgeInferred"])
    with_inc = sum(1 for r in records if r["incumbent"])
    both = sum(1 for r in records if r["optionEnd"] and r["competeEnd"])
    print(f"   with named incumbent      : {with_inc}")
    print(f"   with both dates present   : {both}")
    print(f"   possible 52.217-8 bridges : {bridges} (inferred, not asserted)")

    # ---- splice into AGENCY_DATA, replacing existing Recompete Watch ----
    html = open(INDEX, encoding="utf-8").read()
    head, start, end = locate_agency_data(html)
    existing = json.loads(quote_js_keys(html[start:end]))

    removed = 0
    merged = {}
    for agency, recs in existing.items():
        keep = [r for r in recs if r.get("label") != "Recompete Watch"]
        removed += len(recs) - len(keep)
        merged[agency] = keep

    added = 0
    for r in records:
        agency = r["id"].split("_")[0]
        merged.setdefault(agency, []).append(r)
        added += 1

    for agency in merged:
        merged[agency].sort(key=lambda r: (-(r.get("valueRank") or 0),
                                           r.get("daysOut") or 9999))

    print(f"\nRecompete Watch: removed {removed} previous record(s) "
          f"(DOE snapshot), added {added} from USASpending")

    html = html[:head] + render_agency_data(merged) + html[end:]
    open(INDEX, "w", encoding="utf-8").write(html)
    print(f"index.html updated — {sum(len(v) for v in merged.values())} "
          f"total records across {len(merged)} agencies")


if __name__ == "__main__":
    main()
