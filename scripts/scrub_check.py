#!/usr/bin/env python3
"""
scrub_check.py — refuse to publish index.html if it leaks internal information.

The dashboard is a lead magnet. Prospects read it. Nothing about how the
outbound operation works should ever be visible on it: not the project
codename, not teammate names, not internal tooling.

Two independent guards:

  1. INTERNAL TERMS — project codenames and teammate first names.
  2. EXCLUDED AGENCIES — DHS and all its components, which must not appear
     anywhere prospect-facing while Phil is engaged with that agency.

Both FAIL CLOSED. A hit stops the build and nothing is committed, because a
leak that reaches a prospect cannot be recalled.

FALSE POSITIVES
    Teammate first names are common English names, and a federal point of
    contact could legitimately be named Sarah or Luke. That is why hits print
    surrounding context: if a match turns out to be a real contracting officer,
    add the exact string to ALLOWLIST below rather than weakening the pattern.

Usage:  python scripts/scrub_check.py [path/to/index.html]
"""

import re
import sys

# Codenames and internal project references. Matched case-insensitively as
# phrases — these should never appear on a prospect-facing page.
INTERNAL_PHRASES = [
    "Money Maker",
    "MoneyMaker",
    "Lane 2",
    "Lane2",
    "lead magnet",
    "credibility magnet",
    "Perplexity Computer",
    "dashboard_state.json",
    "dashboard_build.py",
]

# Teammate first names. Word-boundary matched to avoid catching substrings.
INTERNAL_NAMES = [
    "Sarah",
    "Luke",
    "Trent",
]

# Agencies Phil is engaged with — must not surface. Keep in sync with
# EXCLUDED_AGENCY_KEYWORDS in refresh_sam.py.
EXCLUDED_AGENCIES = [
    "DHS", "Homeland Security",
    "Coast Guard", "USCG", "SFLC",
    "Transportation Security", "TSA",
    "Secret Service", "USSS",
    "Customs and Border", "CBP",
    "Immigration and Customs", "USCIS",
    "Federal Emergency Management", "FEMA",
    "Cybersecurity and Infrastructure", "CISA",
    "FLETC",
]

# Exact strings confirmed benign. Add here rather than loosening a pattern.
# Example: "Sarah Chen" if a real contracting officer by that name appears.
ALLOWLIST = [
]


def context(text, start, end, width=60):
    a = max(0, start - width)
    b = min(len(text), end + width)
    snippet = text[a:b].replace("\n", " ")
    return re.sub(r"\s+", " ", snippet).strip()


def allowed(text, start, end):
    for ok in ALLOWLIST:
        if ok and ok.lower() in text[max(0, start - 40):end + 40].lower():
            return True
    return False


def scan(text, patterns, label, word_boundary):
    hits = []
    for p in patterns:
        pattern = (r"\b" + re.escape(p) + r"\b") if word_boundary else re.escape(p)
        for m in re.finditer(pattern, text, re.IGNORECASE):
            if allowed(text, m.start(), m.end()):
                continue
            hits.append((p, m.start(), context(text, m.start(), m.end())))
    if hits:
        print(f"\n{label}: {len(hits)} hit(s)")
        for p, pos, ctx in hits:
            print(f"    '{p}' at offset {pos}")
            print(f"        ...{ctx}...")
    return hits


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "index.html"
    text = open(path, encoding="utf-8").read()

    print(f"scrub check: {path} ({len(text):,} chars)")

    hits = []
    hits += scan(text, INTERNAL_PHRASES, "INTERNAL PHRASES", word_boundary=False)
    hits += scan(text, INTERNAL_NAMES, "INTERNAL NAMES", word_boundary=True)
    hits += scan(text, EXCLUDED_AGENCIES, "EXCLUDED AGENCIES", word_boundary=True)

    if hits:
        print(f"\nFAILED — {len(hits)} disallowed reference(s) found. "
              f"index.html not published.")
        print("If a hit is a legitimate federal contact or office, add the "
              "exact string to ALLOWLIST in this file.")
        sys.exit(1)

    print("\nPASSED — no internal references, no excluded agencies.")


if __name__ == "__main__":
    main()
