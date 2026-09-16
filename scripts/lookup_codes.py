#!/usr/bin/env python3
"""Resolve every anatomic code in a batch to its fully specified name.

This is step 4 of the workflow: what turns "these codes are suspicious" into
"this code denotes a vein and the segment calls it bowel". It also detects the
"Entire X" flavour problem (issue 8) mechanically, and flags codes that do not
exist or are retired.

Two servers, both public and unauthenticated, and they are not interchangeable:

  tx.fhir.org  (default)  All of SNOMED CT, including retired concepts, with the
                          fully specified name and the active/inactive flag.
                          Authoritative here. One request at a time, by policy.
  EBI OLS4                SNOMED CT International as the inferred OWL, which
                          holds ACTIVE CONCEPTS ONLY, and whose label is not
                          reliably the FSN. Fast - it takes concurrent requests.

Because OLS4 has no retired concepts, a code it cannot find is *unresolved*, not
missing: usually retired, which is a different finding. `--server ols4` therefore
never writes the `active` column, and reports a miss as UNRESOLVED.

Usage:
  python lookup_codes.py seg_attributes.csv -o findings/codes.csv
  python lookup_codes.py seg_attributes.csv --server hybrid    # fast; see below
  python lookup_codes.py seg_attributes.csv --column SegmentedPropertyTypeCodeValue

`--server hybrid` screens the whole batch against OLS4 in parallel, then sends
everything OLS4 could not resolve to tx.fhir.org. That residue is precisely the
retired-and-nonexistent set - where the findings are - so the authoritative
answers are the ones you get, in a fraction of the wall-clock.

Output is one row per distinct (CodingSchemeDesignator, CodeValue), sorted by
how many segments depend on it. Work down from the top - a handful of codes
usually accounts for most of the damage. The `source` column says which server
answered each row; read `fsn` in that light.

Only SCT codes are looked up. Private designators (99XXXX) and DCM codes cannot
be resolved on either server and are reported as skipped - judge them against
whatever defines them. Standard library only.
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
from concurrent.futures import ThreadPoolExecutor

FHIR_TX = "https://tx.fhir.org/r4/CodeSystem/$lookup"
SNOMED_SYSTEM = "http://snomed.info/sct"
SNOMED_FSN = "900000000000003001"

OLS4_TERMS = "https://www.ebi.ac.uk/ols4/api/ontologies/snomed/terms"
OLS4_ONTOLOGY = "https://www.ebi.ac.uk/ols4/api/ontologies/snomed"
OLS4_IRI = "http://snomed.info/id/"

SERVERS = ("fhir", "ols4", "hybrid")
FHIR_NAME = "tx.fhir.org"
OLS4_NAME = "ols4"

# Consecutive tx.fhir.org failures before the run finishes on OLS4 instead. A
# public server with no SLA should not take the whole terminology review down
# with it; the rows it did not answer say `source=ols4` and carry no `active`.
FHIR_FAILURES_BEFORE_FALLBACK = 3

COLUMNS = [
    "CodingSchemeDesignator",
    "CodeValue",
    "fsn",
    "found",
    "active",
    "source",
    "terms",
    "isEntireFlavour",
    "codeSequences",
    "meaningsInBatch",
    "distinctMeanings",
    "segments",
    "series",
]

# The code sequences resolved by default. The anatomic region is Type 3 in the
# Segment Description Macro and the other two are Type 1, so on many
# deliveries the organ is the TYPE code and the region is absent; resolving
# the region alone would then resolve nothing. A code used in more than one
# sequence is looked up once and its `codeSequences` column says where.
DEFAULT_COLUMNS = [
    "AnatomicRegionCodeValue",
    "SegmentedPropertyTypeCodeValue",
    "SegmentedPropertyCategoryCodeValue",
]


def lookup_fhir(code, system=SNOMED_SYSTEM, timeout=15):
    """Return (found, fsn, active) for one code, from tx.fhir.org.

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
            if use == SNOMED_FSN:
                display = parts.get("value", {}).get("valueString", display)
        if parameter.get("name") == "property":
            parts = {p.get("name"): p for p in parameter.get("part", [])}
            if parts.get("code", {}).get("valueCode") == "inactive":
                if parts.get("value", {}).get("valueBoolean", False):
                    active = False
    return True, display, active


def lookup_ols4(code, timeout=30):
    """Return (found, label, terms) for one code, from EBI OLS4.

    `terms` is every term OLS4 carries for the concept - its label plus the
    preferred and alternative labels. The FSN stem is always among them, but
    nothing marks WHICH one it is, and the semantic tag ("(body structure)") is
    usually stripped: OLS4's `label` was the FSN without its tag for 198 of 257
    codes measured, the full FSN for 22, and a synonym for 37 ("Cardiac MRI" for
    "Magnetic resonance imaging of heart (procedure)"). So `label` is a term, not
    the FSN, and checks that turn on the FSN belong on tx.fhir.org.

    A 404 means OLS4 does not hold the concept. Since its SNOMED release carries
    active concepts only, that is usually a RETIRED code rather than a
    nonexistent one - the caller must not report it as missing.
    """
    url = f"{OLS4_TERMS}?" + urllib.parse.urlencode({"iri": OLS4_IRI + code})
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False, "", ()
        raise

    terms = (data.get("_embedded") or {}).get("terms") or []
    if not terms:
        return False, "", ()
    term = terms[0]
    label = (term.get("label") or "").strip()
    annotation = term.get("annotation") or {}
    every = [label]
    for key in ("preferred label", "alternative label"):
        every.extend(annotation.get(key) or [])
    every.extend(term.get("synonyms") or [])
    seen, ordered = set(), []
    for value in every:
        value = (value or "").strip()
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return True, label, tuple(ordered)


def ols4_provenance(timeout=30):
    """The SNOMED version IRI OLS4 currently serves, for the report.

    "Not in SNOMED" and "retired" are claims about one release on one day; OLS4
    reloads nightly, so the version has to be quoted rather than assumed.
    """
    try:
        with urllib.request.urlopen(OLS4_ONTOLOGY, timeout=timeout) as response:
            data = json.loads(response.read())
    except Exception:
        return ""
    config = data.get("config") or {}
    version = config.get("versionIri") or config.get("version") or ""
    loaded = (data.get("loaded") or "")[:10]
    return f"{version} (OLS4 load {loaded})" if loaded else version


def is_entire_flavour(terms):
    """Issue 8's mechanical half: does any term name the whole organ only?

    SNOMED's "Entire X" concept denotes the complete organ exclusively, where
    DICOM's context groups use the "X structure" flavour - see
    references/terminology.md. On tx.fhir.org `terms` is the one FSN, so this is
    the FSN test. On OLS4 it is the whole term set, which cannot miss an "Entire
    X" hiding behind a synonymous label but can flag a concept whose FSN is
    something else. Either way these are candidates, confirmed by
    `dcmterm.py suggest`.
    """
    return any(term.startswith("Entire ") for term in terms)


def resolve_ols4(values, workers=8, progress=None):
    """Resolve `values` against OLS4 concurrently. Returns code -> result dict."""
    results = {}

    def one(code):
        try:
            found, label, terms = lookup_ols4(code)
        except Exception as exc:
            return code, {"found": "ERROR", "fsn": "", "active": "",
                          "source": OLS4_NAME, "terms": (), "status": "err",
                          "note": f"lookup failed: {exc}"}
        if not found:
            return code, {"found": "UNRESOLVED", "fsn": "", "active": "",
                          "source": OLS4_NAME, "terms": (), "status": "unres",
                          "note": "not in OLS4's active-only release - retired, "
                                  "or absent; confirm on tx.fhir.org"}
        return code, {"found": "True", "fsn": label, "active": "",
                      "source": OLS4_NAME, "terms": terms, "status": "ok",
                      "note": ""}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for done, (code, result) in enumerate(pool.map(one, values), start=1):
            results[code] = result
            if progress:
                progress(done, len(values))
    return results


def resolve_fhir(values, delay=0.3, workers=8, progress=None):
    """Resolve `values` against tx.fhir.org, one at a time.

    On `FHIR_FAILURES_BEFORE_FALLBACK` consecutive failures the rest of the run
    goes to OLS4, so a server outage costs the `active` column rather than the
    whole review. Returns (results, answered_before_fallback), the second being
    how many codes tx.fhir.org did answer before the switch, or None if it never
    happened.
    """
    results, consecutive = {}, 0
    for done, code in enumerate(values, start=1):
        try:
            found, fsn, active = lookup_fhir(code)
        except Exception as exc:
            consecutive += 1
            results[code] = {"found": "ERROR", "fsn": "", "active": "",
                             "source": FHIR_NAME, "terms": (), "status": "err",
                             "note": f"lookup failed: {exc}"}
            if consecutive >= FHIR_FAILURES_BEFORE_FALLBACK:
                remaining = [v for v in values if v not in results]
                failed = [v for v, r in results.items() if r["status"] == "err"]
                answered = len(results) - len(failed)
                results.update(resolve_ols4(failed + remaining, workers, progress))
                return results, answered
            time.sleep(1)
            continue
        consecutive = 0
        results[code] = {
            "found": str(found),
            "fsn": fsn,
            # A code that does not exist has no activity status; writing True
            # there would read as "exists and is current".
            "active": str(active) if found else "",
            "source": FHIR_NAME,
            "terms": (fsn,) if fsn else (),
            "status": "ok" if found and active else ("retired" if found else "missing"),
            "note": "" if found and active else
                    ("RETIRED: " if found else "MISSING: ") + (fsn or "not in SNOMED CT"),
        }
        if progress:
            progress(done, len(values))
        time.sleep(delay)
    return results, None


def collect(table, columns):
    """Distinct (scheme, code) across the CodeValue `columns`, with what
    depends on each.

    `columns` is one column name or a list; the scheme and meaning columns
    follow from the prefix. A code used in two sequences is one entry whose
    `sequences` names both, and its `segments` count is one per sequence that
    carries it. Background segments are excluded - they are an artefact of
    labelmap encoding, not something segmented.
    """
    if isinstance(columns, str):
        columns = [columns]
    codes = defaultdict(
        lambda: {"meanings": set(), "segments": 0, "series": set(), "sequences": set()}
    )
    with open(table, newline="") as handle:
        for row in csv.DictReader(handle):
            if (row.get("isBackgroundSegment") or "").strip().lower() == "true":
                continue
            for column in columns:
                prefix = column.replace("CodeValue", "")
                value = (row.get(column) or "").strip()
                if not value:
                    continue
                scheme = (row.get(f"{prefix}CodingSchemeDesignator") or "").strip()
                entry = codes[(scheme, value)]
                entry["meanings"].add((row.get(f"{prefix}CodeMeaning") or "").strip())
                entry["segments"] += 1
                entry["series"].add(row.get("SeriesInstanceUID", ""))
                entry["sequences"].add(prefix)
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
        action="append",
        metavar="CODEVALUE_COLUMN",
        help="which CodeValue column to resolve; repeatable. Default: the "
        "anatomic region, the segmented property type AND the segmented "
        "property category - the anatomy is often the TYPE code, with no "
        "region at all. Name one column to narrow it.",
    )
    parser.add_argument(
        "--server", choices=SERVERS, default="fhir",
        help="fhir (default): tx.fhir.org alone - the FSN and the active flag, "
        "one request at a time. ols4: EBI OLS4 alone - fast and concurrent, but "
        "its release has no retired concepts and its label is not reliably the "
        "FSN, so no `active` is written and a miss is UNRESOLVED. hybrid: OLS4 "
        "for the batch, then tx.fhir.org for everything OLS4 could not resolve.")
    parser.add_argument("--delay", type=float, default=0.3,
                        help="seconds between tx.fhir.org requests; do not hammer "
                             "a public server")
    parser.add_argument("--workers", type=int, default=8,
                        help="concurrent OLS4 requests (default 8)")
    args = parser.parse_args()

    columns = args.column or DEFAULT_COLUMNS
    codes = collect(args.table, columns)
    if not codes:
        sys.exit(f"no values in {', '.join(columns)}")

    ordered = sorted(codes.items(), key=lambda kv: (-kv[1]["segments"], kv[0][1]))
    sct = [value for (scheme, value), _ in ordered if scheme == "SCT"]
    print(f"{len(ordered)} distinct codes across {', '.join(columns)}; "
          f"{len(sct)} of them SCT", file=sys.stderr)

    def progress(label):
        def report(done, total):
            if done == total or done % 20 == 0:
                print(f"\r  {label}: {done}/{total}", end="", file=sys.stderr,
                      flush=True)
            if done == total:
                print(file=sys.stderr)
        return report

    resolved, fallback_after, provenance = {}, None, ""
    if args.server in ("ols4", "hybrid") and sct:
        provenance = ols4_provenance()
        print(f"  OLS4: SNOMED {provenance or 'version unknown'}", file=sys.stderr)
        resolved = resolve_ols4(sct, args.workers, progress("ols4"))
    if args.server == "fhir":
        residue = sct
    elif args.server == "hybrid":
        residue = [v for v in sct if resolved[v]["found"] != "True"]
        if residue:
            print(f"  {len(residue)} unresolved by OLS4 -> tx.fhir.org "
                  f"(~{len(residue) * args.delay:.0f}s)", file=sys.stderr)
    else:
        residue = []
    if residue:
        answers, fallback_after = resolve_fhir(residue, args.delay, args.workers,
                                               progress("tx.fhir.org"))
        resolved.update(answers)
        if fallback_after is not None and not provenance:
            provenance = ols4_provenance()

    print(f"\n{'code':<14} {'st':>7}  {'segs':>5}  fully specified name / term",
          file=sys.stderr)
    print("-" * 78, file=sys.stderr)

    rows, problems, entire, by_source = [], [], [], defaultdict(int)
    for (scheme, value), info in ordered:
        meanings = sorted(m for m in info["meanings"] if m)
        row = {
            "CodingSchemeDesignator": scheme,
            "CodeValue": value,
            "fsn": "",
            "found": "",
            "active": "",
            "source": "",
            "terms": "",
            "isEntireFlavour": "False",
            "codeSequences": "; ".join(sorted(info["sequences"])),
            "meaningsInBatch": " | ".join(meanings),
            "distinctMeanings": len(meanings),
            "segments": info["segments"],
            "series": len(info["series"]),
        }

        if scheme != "SCT":
            row["found"] = "SKIPPED"
            print(f"{value:<14} {'skip':>7}  {info['segments']:>5}  "
                  f"scheme {scheme or '(none)'} - not resolvable here", file=sys.stderr)
            rows.append(row)
            continue

        result = resolved.get(value)
        if result is None:  # only when a mode resolved nothing for it
            row["found"] = "ERROR"
            print(f"{value:<14} {'err':>7}  {info['segments']:>5}  not resolved",
                  file=sys.stderr)
            rows.append(row)
            continue

        row["found"] = result["found"]
        row["active"] = result["active"]
        row["fsn"] = result["fsn"]
        row["source"] = result["source"]
        row["terms"] = " | ".join(result["terms"]) if result["source"] == OLS4_NAME else ""
        by_source[result["source"]] += 1
        if is_entire_flavour(result["terms"]):
            row["isEntireFlavour"] = "True"
            entire.append((value, result["fsn"], info["segments"]))
        if result["note"]:
            problems.append((value, result["note"]))

        status = {"ok": "ok", "retired": "RETIRED", "missing": "MISSING",
                  "unres": "UNRES", "err": "err"}[result["status"]]
        print(f"{value:<14} {status:>7}  {info['segments']:>5}  {result['fsn']}",
              file=sys.stderr)
        rows.append(row)

    with open(args.output, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} codes to {args.output}", file=sys.stderr)
    if by_source:
        print("  answered by: " + ", ".join(f"{name} {count}"
                                            for name, count in sorted(by_source.items())),
              file=sys.stderr)
    if fallback_after is not None:
        print(f"\n  ** tx.fhir.org failed {FHIR_FAILURES_BEFORE_FALLBACK} times in a "
              f"row having answered {fallback_after}; the rest went to OLS4. Those "
              "rows have\n     no `active` flag and their UNRESOLVED codes are "
              "unjudged - re-run them\n     against tx.fhir.org before the report "
              "calls anything retired or missing.", file=sys.stderr)
    if provenance:
        print(f"  OLS4 served SNOMED {provenance} - cite this for every ols4 row.",
              file=sys.stderr)
    if entire:
        segments = sum(s for _, _, s in entire)
        print(f"\n  {len(entire)} \"Entire X\" codes over {segments} segments "
              "(issue 8). Split these into", file=sys.stderr)
        print("  those with a drop-in \"X structure\" equivalent and those needing "
              "a decision:", file=sys.stderr)
        for value, fsn, count in sorted(entire, key=lambda e: -e[2]):
            print(f"    {count:>5}  {value:<12} {fsn}", file=sys.stderr)
    if problems:
        unresolved = [p for p in problems if "not in OLS4" in p[1]]
        print(f"\n  {len(problems)} codes need attention:", file=sys.stderr)
        for value, message in problems:
            print(f"    {value}: {message}", file=sys.stderr)
        if unresolved:
            print(f"\n  {len(unresolved)} of those are UNRESOLVED, not missing. OLS4's "
                  "release carries active\n  concepts only, so it cannot tell a retired "
                  "code from a nonexistent one:\n"
                  "  re-run them with --server fhir before reporting either.",
                  file=sys.stderr)

    print(
        "\n  Next: compare `fsn` against `meaningsInBatch` for every row and record a\n"
        "  verdict per (CodeValue, CodeMeaning) pair in a review CSV - see\n"
        "  references/terminology.md. Then re-run seg_checks.py with --review.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
