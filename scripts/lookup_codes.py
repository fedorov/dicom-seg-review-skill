#!/usr/bin/env python3
"""Resolve every anatomic code in a batch to its fully specified name.

This is step 4 of the workflow: what turns "these codes are suspicious" into
"this code denotes a vein and the segment calls it bowel". It also detects the
"Entire X" flavour problem (issue 8) mechanically, and flags codes that do not
exist or are retired.

Looks codes up on the public FHIR terminology server at tx.fhir.org (no auth).

Usage:
  python lookup_codes.py seg_attributes.csv -o findings/codes.csv
  python lookup_codes.py seg_attributes.csv --column SegmentedPropertyTypeCodeValue

Output is one row per distinct (CodingSchemeDesignator, CodeValue), sorted by
how many segments depend on it. Work down from the top - a handful of codes
usually accounts for most of the damage.

Only SCT codes are looked up. Private designators (99XXXX) and DCM codes cannot
be resolved here and are reported as skipped - judge them against whatever
defines them. Standard library only.
"""

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

FHIR_TX = "https://tx.fhir.org/r4/CodeSystem/$lookup"
SNOMED_SYSTEM = "http://snomed.info/sct"

COLUMNS = [
    "CodingSchemeDesignator",
    "CodeValue",
    "fsn",
    "found",
    "active",
    "isEntireFlavour",
    "meaningsInBatch",
    "distinctMeanings",
    "segments",
    "series",
]


def lookup(code, system=SNOMED_SYSTEM, timeout=15):
    """Return (found, display, active) for one code.

    A 404, or a body saying the code is not known, means the code does not exist
    in the code system - a finding in itself, not an error to swallow.
    """
    url = f"{FHIR_TX}?" + urllib.parse.urlencode({"system": system, "code": code})
    request = urllib.request.Request(url, headers={"Accept": "application/fhir+json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False, "", True
        body = exc.read().decode("utf-8", errors="replace")
        if "not found" in body.lower() or "not known" in body.lower():
            return False, "", True
        raise

    display, active = "", True
    for parameter in data.get("parameter", []):
        if parameter.get("name") == "display":
            display = parameter.get("valueString", "")
        # The fully specified name is more precise than the preferred display
        # term, and is what distinguishes "Entire colon" from "Colon structure".
        if parameter.get("name") == "designation":
            parts = {p.get("name"): p for p in parameter.get("part", [])}
            use = parts.get("use", {}).get("valueCoding", {}).get("code", "")
            if use == "900000000000003001":  # SNOMED FSN
                display = parts.get("value", {}).get("valueString", display)
        if parameter.get("name") == "property":
            parts = {p.get("name"): p for p in parameter.get("part", [])}
            if parts.get("code", {}).get("valueCode") == "inactive":
                if parts.get("value", {}).get("valueBoolean", False):
                    active = False
    return True, display, active


def collect(table, value_column, scheme_column, meaning_column):
    codes = defaultdict(
        lambda: {"meanings": set(), "segments": 0, "series": set()}
    )
    with open(table, newline="") as handle:
        for row in csv.DictReader(handle):
            value = (row.get(value_column) or "").strip()
            if not value or row.get("isBackgroundSegment") == "True":
                continue
            entry = codes[((row.get(scheme_column) or "").strip(), value)]
            entry["meanings"].add((row.get(meaning_column) or "").strip())
            entry["segments"] += 1
            entry["series"].add(row.get("SeriesInstanceUID", ""))
    return codes


def main():
    parser = argparse.ArgumentParser(
        description="Resolve anatomic codes to fully specified names.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("table", help="CSV from seg_attributes.py")
    parser.add_argument("-o", "--output", default="codes.csv")
    parser.add_argument(
        "--column",
        default="AnatomicRegionCodeValue",
        help="which code column to resolve (default: %(default)s). Use "
        "SegmentedPropertyTypeCodeValue to review the type coding instead.",
    )
    parser.add_argument("--delay", type=float, default=0.3,
                        help="seconds between requests; do not hammer a public server")
    args = parser.parse_args()

    prefix = args.column.replace("CodeValue", "")
    codes = collect(
        args.table,
        args.column,
        f"{prefix}CodingSchemeDesignator",
        f"{prefix}CodeMeaning",
    )
    if not codes:
        sys.exit(f"no values in {args.column}")

    ordered = sorted(codes.items(), key=lambda kv: (-kv[1]["segments"], kv[0][1]))
    print(f"{len(ordered)} distinct codes in {args.column}\n", file=sys.stderr)
    print(f"{'code':<14} {'st':>4}  {'segs':>5}  fully specified name", file=sys.stderr)
    print("-" * 78, file=sys.stderr)

    rows, problems, entire = [], [], []
    for (scheme, value), info in ordered:
        meanings = sorted(m for m in info["meanings"] if m)
        row = {
            "CodingSchemeDesignator": scheme,
            "CodeValue": value,
            "fsn": "",
            "found": "",
            "active": "",
            "isEntireFlavour": "False",
            "meaningsInBatch": " | ".join(meanings),
            "distinctMeanings": len(meanings),
            "segments": info["segments"],
            "series": len(info["series"]),
        }

        if scheme != "SCT":
            row["found"] = "SKIPPED"
            print(f"{value:<14} {'skip':>4}  {info['segments']:>5}  "
                  f"scheme {scheme or '(none)'} - not resolvable here", file=sys.stderr)
            rows.append(row)
            continue

        try:
            found, fsn, active = lookup(value)
        except Exception as exc:
            row["found"] = "ERROR"
            problems.append((value, f"lookup failed: {exc}"))
            print(f"{value:<14} {'err':>4}  {info['segments']:>5}  {exc}",
                  file=sys.stderr)
            rows.append(row)
            time.sleep(1)
            continue

        row["found"] = str(found)
        row["active"] = str(active)
        row["fsn"] = fsn
        # Mechanical detection of issue 8: SNOMED's "Entire X" concept denotes
        # the whole organ exclusively, where DICOM's context groups use the
        # "X structure" flavour. See references/terminology.md.
        if fsn.startswith("Entire "):
            row["isEntireFlavour"] = "True"
            entire.append((value, fsn, info["segments"]))

        status = "ok" if found and active else ("RETIRED" if found else "MISSING")
        if status != "ok":
            problems.append((value, f"{status}: {fsn or 'not in SNOMED CT'}"))
        print(f"{value:<14} {status:>4}  {info['segments']:>5}  {fsn}", file=sys.stderr)
        rows.append(row)
        time.sleep(args.delay)

    with open(args.output, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} codes to {args.output}", file=sys.stderr)
    if entire:
        segments = sum(s for _, _, s in entire)
        print(f"\n  {len(entire)} \"Entire X\" codes over {segments} segments "
              "(issue 8). Split these into", file=sys.stderr)
        print("  those with a drop-in \"X structure\" equivalent and those needing "
              "a decision:", file=sys.stderr)
        for value, fsn, count in sorted(entire, key=lambda e: -e[2]):
            print(f"    {count:>5}  {value:<12} {fsn}", file=sys.stderr)
    if problems:
        print(f"\n  {len(problems)} codes need attention:", file=sys.stderr)
        for value, message in problems:
            print(f"    {value}: {message}", file=sys.stderr)

    print(
        "\n  Next: compare `fsn` against `meaningsInBatch` for every row and record a\n"
        "  verdict per (CodeValue, CodeMeaning) pair in a review CSV - see\n"
        "  references/terminology.md. Then re-run seg_checks.py with --review.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
