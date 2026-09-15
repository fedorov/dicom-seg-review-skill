#!/usr/bin/env python3
"""Check a batch's codes against the code set DICOM itself uses.

The reference is dcmterms - every coded entry extracted from PS3.16's context
groups, published as Parquet at

  https://github.com/fedorov/dcmterms/tree/main/docs/data

which this script downloads and caches. Public, versioned, and it records the
DICOM edition it came from, so a finding can be reproduced later. Nothing here
needs BigQuery.

What it answers, and what it does not:

  dcmterms says what DICOM EXPECTS - which codes its context groups draw on,
  and with what meanings. It is NOT all of SNOMED, and the share of a batch it
  reaches can be well under half - `coverage` prints that number. Codes it does
  not carry are not thereby wrong; they are unreviewed, and the most serious
  error in a batch can be among them. Resolve those against a terminology
  server with lookup_codes.py.

Usage:
  python dcmterm.py fetch                            # download / refresh the cache
  python dcmterm.py coverage seg_attributes.csv -o findings/coverage.csv
  python dcmterm.py lookup 10200004 110634007        # what DICOM records, and in which CIDs
  python dcmterm.py search 'cervical lymph node'     # find a standard code by meaning
  python dcmterm.py suggest findings/codes.csv -o findings/entire.csv

`coverage` is the computed check behind sql/03 and the deliverable described in
references/terminology.md. `suggest` fills in the "Entire X" replacement table
that sql/07 needs, from the FSNs lookup_codes.py resolved.

Needs one of pyarrow, duckdb or pandas to read Parquet; nothing else beyond the
standard library.
"""

import argparse
import csv
import json
import os
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

DATA_BASE = "https://raw.githubusercontent.com/fedorov/dcmterms/main/docs/data"
PARQUET = ("codes_unique.parquet", "coded_entries.parquet")
PROVENANCE = ("extraction_metadata.json", "version.json")

COVERAGE_COLUMNS = [
    "CodingSchemeDesignator",
    "CodeValue",
    "meaningsInBatch",
    "distinctMeanings",
    "segments",
    "series",
    "inDcmterm",
    "isPrivateScheme",
    "dcmMeanings",
    "meaningAgrees",
    "numCids",
]

# Columns named to match the entireFlavourCodes struct in sql/07.
SUGGEST_COLUMNS = [
    "code",
    "entireFsn",
    "inDcmterm",
    "suggestedReplacement",
    "replacementMeaning",
    "replacementNumCids",
    "otherCandidates",
    "segments",
]


def cache_dir(override=None):
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "dcmterms"


def fetch(cache, refresh=False, quiet=False):
    """Download the published tables into `cache`, skipping what is already there."""
    cache.mkdir(parents=True, exist_ok=True)
    for name in PARQUET + PROVENANCE:
        target = cache / name
        if target.exists() and not refresh:
            continue
        url = f"{DATA_BASE}/{name}"
        if not quiet:
            print(f"downloading {url}", file=sys.stderr)
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
        target.write_bytes(data)
    return cache


def provenance(cache):
    """DICOM edition and extraction date, for the report to cite."""
    try:
        meta = json.loads((cache / "extraction_metadata.json").read_text())
    except (OSError, ValueError):
        return {}
    try:
        version = json.loads((cache / "version.json").read_text())
    except (OSError, ValueError):
        version = {}
    return {
        "dicom_edition": meta.get("dicom_edition", ""),
        "extraction_date": meta.get("extraction_date", ""),
        "unique_codes": meta.get("unique_codes", ""),
        "commit": version.get("short_sha", ""),
    }


def read_parquet(path):
    """Read a Parquet file with whichever reader is installed.

    Deliberately not a hard dependency on any one of them: the rest of this
    skill is standard library only, and which of these a given environment has
    is luck.
    """
    try:
        import pyarrow.parquet as parquet
    except ImportError:
        pass
    else:
        return parquet.read_table(path).to_pylist()

    try:
        import duckdb
    except ImportError:
        pass
    else:
        cursor = duckdb.connect().execute(
            "SELECT * FROM read_parquet(?)", [str(path)])
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    try:
        import pandas
        # pandas is a front end here, not an engine - it still needs pyarrow or
        # fastparquet underneath, so this only helps a fastparquet-only install.
        return pandas.read_parquet(path).to_dict("records")
    except ImportError:
        sys.exit(f"cannot read {path.name}: no Parquet reader available.\n"
                 "  pip install pyarrow      (or duckdb)")


def normalise(meaning):
    """Case and whitespace only.

    Hyphenation and wording differences are left alone on purpose - "Paraaortic"
    against "para-aortic" is the SPELLING verdict, a finding rather than noise
    to normalise away.
    """
    return " ".join((meaning or "").casefold().split())


def dcm_codes(cache):
    """(scheme, value) -> {meanings, normalised, numCids} from codes_unique.

    codes_unique is deduplicated on the MEANING as well as the code, so one code
    appears once per spelling DICOM uses for it - 21974007 is there as both
    "Tongue" and "tongue". Collapse to one entry per code, keeping every meaning.
    """
    table = defaultdict(lambda: {"meanings": [], "normalised": set(), "numCids": 0})
    for row in read_parquet(cache / "codes_unique.parquet"):
        scheme = (row.get("coding_scheme_designator") or "").strip()
        value = (row.get("code_value") or "").strip()
        meaning = (row.get("code_meaning") or "").strip()
        if not value:
            continue
        entry = table[(scheme, value)]
        if meaning and meaning not in entry["meanings"]:
            entry["meanings"].append(meaning)
        entry["normalised"].add(normalise(meaning))
        entry["numCids"] += int(row.get("num_cids") or 0)
    return table


def dcm_cids(cache):
    """(scheme, value) -> [(cid_number, cid_name, code_meaning)] from coded_entries."""
    table = defaultdict(list)
    for row in read_parquet(cache / "coded_entries.parquet"):
        scheme = (row.get("coding_scheme_designator") or "").strip()
        value = (row.get("code_value") or "").strip()
        if not value:
            continue
        table[(scheme, value)].append((
            int(row.get("cid_number") or 0),
            (row.get("cid_name") or "").strip(),
            (row.get("code_meaning") or "").strip(),
        ))
    return table


def batch_codes(table, value_column, scheme_column, meaning_column):
    """Distinct codes in the batch, with what depends on each.

    Same shape as lookup_codes.collect - Background segments excluded, since
    they are an artefact of labelmap encoding rather than something segmented.
    """
    codes = defaultdict(lambda: {"meanings": set(), "segments": 0, "series": set()})
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


def is_private(scheme):
    """99-prefixed designators are private by DICOM PS3.3 section 8.2.

    Their absence from dcmterms is expected, not a finding - they are
    placeholders for structures with no standard code, and must be judged
    against whatever defines them.
    """
    return scheme.startswith("99")


def coverage(codes, reference):
    """One row per distinct batch code: is it a code DICOM uses, and with this meaning?"""
    rows = []
    for (scheme, value), info in codes.items():
        meanings = sorted(m for m in info["meanings"] if m)
        entry = reference.get((scheme, value))
        row = {
            "CodingSchemeDesignator": scheme,
            "CodeValue": value,
            "meaningsInBatch": " | ".join(meanings),
            "distinctMeanings": len(meanings),
            "segments": info["segments"],
            "series": len(info["series"]),
            "inDcmterm": str(entry is not None),
            "isPrivateScheme": str(is_private(scheme)),
            "dcmMeanings": "",
            "meaningAgrees": "",
            "numCids": "",
        }
        if entry is not None:
            agreeing = sum(1 for m in meanings if normalise(m) in entry["normalised"])
            row["dcmMeanings"] = " | ".join(entry["meanings"])
            row["numCids"] = entry["numCids"]
            row["meaningAgrees"] = (
                "True" if agreeing == len(meanings) and meanings
                else "False" if agreeing == 0
                else "PARTIAL")
        rows.append(row)
    return sorted(rows, key=lambda r: (-r["segments"], r["CodeValue"]))


def strip_semantic_tag(fsn):
    """"Entire colon (body structure)" -> "Entire colon"."""
    fsn = (fsn or "").strip()
    if fsn.endswith(")") and "(" in fsn:
        return fsn[:fsn.rindex("(")].strip()
    return fsn


def search(reference, text, scheme=None):
    """Codes whose DICOM meaning contains `text`, exact matches first."""
    wanted = normalise(text)
    hits = []
    for (code_scheme, value), entry in reference.items():
        if scheme and code_scheme != scheme:
            continue
        for meaning in entry["meanings"]:
            normalised = normalise(meaning)
            if wanted in normalised:
                hits.append((normalised != wanted, -entry["numCids"],
                             code_scheme, value, meaning, entry["numCids"]))
                break
    return [(s, v, m, n) for _, _, s, v, m, n in sorted(hits)]


def suggest(code_rows, reference):
    """Replacements for the "Entire X" codes lookup_codes.py flagged.

    The argument for issue 8 is DICOM's own code set: the "Entire" flavour is
    absent from it while the "X structure" flavour is present. This reports both
    halves of that, and the candidate replacement is a CANDIDATE - SNOMED's
    naming is not regular enough for the match to stand unreviewed.
    """
    rows = []
    for code in code_rows:
        if str(code.get("isEntireFlavour", "")).lower() != "true":
            continue
        value = (code.get("CodeValue") or "").strip()
        scheme = (code.get("CodingSchemeDesignator") or "SCT").strip()
        fsn = strip_semantic_tag(code.get("fsn"))
        stem = fsn[len("Entire "):].strip() if fsn.lower().startswith("entire ") else fsn
        candidates = [c for c in search(reference, stem, scheme=scheme)
                      if c[1] != value]
        exact = [c for c in candidates if normalise(c[2]) == normalise(stem)]
        best = (exact or candidates or [(None, "", "", "")])[0]
        others = [f"{c[1]} {c[2]}" for c in candidates if c[1] != best[1]][:5]
        rows.append({
            "code": value,
            "entireFsn": fsn,
            "inDcmterm": str((scheme, value) in reference),
            "suggestedReplacement": best[1],
            "replacementMeaning": best[2],
            "replacementNumCids": best[3],
            "otherCandidates": " | ".join(others),
            "segments": code.get("segments", ""),
        })
    return sorted(rows, key=lambda r: (r["suggestedReplacement"] != "", r["code"]))


def read_rows(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def write(path, rows, columns):
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {len(rows)} rows to {path}", file=sys.stderr)


def report_provenance(cache):
    facts = provenance(cache)
    if facts:
        print(f"dcmterms: DICOM edition {facts['dicom_edition']}, extracted "
              f"{facts['extraction_date']}, {facts['unique_codes']} unique codes "
              f"(commit {facts['commit']})", file=sys.stderr)
        print("  Cite this in the report - it is what makes the finding "
              "reproducible.\n", file=sys.stderr)
    return facts


def command_fetch(args, cache):
    fetch(cache, refresh=args.refresh)
    print(f"cache: {cache}", file=sys.stderr)
    report_provenance(cache)
    return 0


def command_coverage(args, cache):
    fetch(cache, quiet=True)
    report_provenance(cache)
    prefix = args.column.replace("CodeValue", "")
    codes = batch_codes(args.table, args.column,
                        f"{prefix}CodingSchemeDesignator",
                        f"{prefix}CodeMeaning")
    if not codes:
        sys.exit(f"no values in {args.column}")

    rows = coverage(codes, dcm_codes(cache))
    write(args.output, rows, COVERAGE_COLUMNS)

    covered = [r for r in rows if r["inDcmterm"] == "True"]
    private = [r for r in rows if r["isPrivateScheme"] == "True"]
    gap = [r for r in rows
           if r["inDcmterm"] == "False" and r["isPrivateScheme"] == "False"]
    disagree = [r for r in covered if r["meaningAgrees"] in ("False", "PARTIAL")]
    total_segments = sum(r["segments"] for r in rows)
    gap_segments = sum(r["segments"] for r in gap)

    print(f"\n  {len(covered)} of {len(rows)} codes are codes DICOM uses "
          f"({sum(r['segments'] for r in covered)} of {total_segments} segments)",
          file=sys.stderr)
    if private:
        print(f"  {len(private)} private-scheme codes over "
              f"{sum(r['segments'] for r in private)} segments - expected misses, "
              "judge against whatever defines them", file=sys.stderr)
    if gap:
        print(f"\n  COVERAGE GAP: {len(gap)} codes over {gap_segments} segments are "
              "NOT in DICOM's code set.", file=sys.stderr)
        print("  These are UNREVIEWED, not clean. Publish this list with the "
              "report and resolve", file=sys.stderr)
        print("  every one of them with lookup_codes.py:", file=sys.stderr)
        for row in gap[:15]:
            print(f"    {row['segments']:>5}  {row['CodeValue']:<14} "
                  f"{row['meaningsInBatch'][:50]}", file=sys.stderr)
        if len(gap) > 15:
            print(f"    ... and {len(gap) - 15} more in {args.output}",
                  file=sys.stderr)
    if disagree:
        print(f"\n  {len(disagree)} codes ARE in DICOM's code set but the batch "
              "records a meaning", file=sys.stderr)
        print("  DICOM does not use for them - issue 1 candidates, reviewSource "
              "dcmterm:", file=sys.stderr)
        for row in disagree[:15]:
            print(f"    {row['segments']:>5}  {row['CodeValue']:<14} "
                  f"batch: {row['meaningsInBatch'][:34]:<34} "
                  f"DICOM: {row['dcmMeanings'][:34]}", file=sys.stderr)
        if len(disagree) > 15:
            print(f"    ... and {len(disagree) - 15} more", file=sys.stderr)
    return 0


def command_lookup(args, cache):
    fetch(cache, quiet=True)
    reference, cids = dcm_codes(cache), dcm_cids(cache)
    for value in args.codes:
        matches = [(s, v) for (s, v) in reference if v == value]
        if not matches:
            print(f"{value}: not in dcmterms - DICOM's context groups do not use "
                  "this code.\n  That is not a verdict: resolve it against "
                  "tx.fhir.org with lookup_codes.py.", file=sys.stdout)
            continue
        for scheme, _ in matches:
            entry = reference[(scheme, value)]
            print(f"{scheme} {value}: {' | '.join(entry['meanings'])}")
            for number, name, meaning in sorted(set(cids[(scheme, value)])):
                print(f"    CID {number:<6} {name}  ({meaning})")
    return 0


def command_search(args, cache):
    fetch(cache, quiet=True)
    hits = search(dcm_codes(cache), " ".join(args.text), scheme=args.scheme)
    if not hits:
        print("no DICOM code carries that meaning; try a shorter phrase",
              file=sys.stderr)
        return 1
    for scheme, value, meaning, num_cids in hits[:args.limit]:
        print(f"{scheme:<4} {value:<14} {num_cids:>3} CIDs  {meaning}", flush=True)
    if len(hits) > args.limit:
        print(f"... {len(hits) - args.limit} more; raise --limit", file=sys.stderr)
    return 0


def command_suggest(args, cache):
    fetch(cache, quiet=True)
    report_provenance(cache)
    rows = suggest(read_rows(args.codes), dcm_codes(cache))
    if not rows:
        print("no rows with isEntireFlavour=True - run lookup_codes.py first, or "
              "issue 8 does not apply to this batch", file=sys.stderr)
        return 0
    for row in rows:
        print(f"{row['code']:<14} {row['entireFsn'][:34]:<34} -> "
              f"{row['suggestedReplacement'] or '(no candidate - needs a decision)':<14} "
              f"{row['replacementMeaning']}", file=sys.stderr)
    write(args.output, rows, SUGGEST_COLUMNS)
    print("  Candidates, not verdicts: confirm each against the FSN before "
          "putting it in a report.\n  The columns match the entireFlavourCodes "
          "struct in scripts/sql/07.", file=sys.stderr)
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Check a batch's codes against the code set DICOM itself uses.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument("--cache", help="where to keep the downloaded tables "
                        "(default: $XDG_CACHE_HOME/dcmterms or ~/.cache/dcmterms)")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch_parser = sub.add_parser("fetch", help="download or refresh the cache")
    fetch_parser.add_argument("--refresh", action="store_true",
                              help="re-download even if cached; do this when a new "
                                   "DICOM edition is out")
    fetch_parser.set_defaults(run=command_fetch)

    coverage_parser = sub.add_parser(
        "coverage", help="which of the batch's codes DICOM uses, and with what meaning")
    coverage_parser.add_argument("table", help="CSV from seg_attributes.py")
    coverage_parser.add_argument("-o", "--output", default="coverage.csv")
    coverage_parser.add_argument(
        "--column", default="AnatomicRegionCodeValue",
        help="which code column to check (default: %(default)s). Use "
             "SegmentedPropertyTypeCodeValue for the type coding.")
    coverage_parser.set_defaults(run=command_coverage)

    lookup_parser = sub.add_parser(
        "lookup", help="what DICOM records for a code, and in which context groups")
    lookup_parser.add_argument("codes", nargs="+")
    lookup_parser.set_defaults(run=command_lookup)

    search_parser = sub.add_parser(
        "search", help="find a code DICOM uses by its meaning")
    search_parser.add_argument("text", nargs="+")
    search_parser.add_argument("--scheme", default="SCT",
                               help="coding scheme (default: %(default)s); "
                                    "pass '' for any")
    search_parser.add_argument("--limit", type=int, default=25)
    search_parser.set_defaults(run=command_search)

    suggest_parser = sub.add_parser(
        "suggest", help='structure-flavour replacements for the "Entire X" codes')
    suggest_parser.add_argument("codes", help="codes.csv from lookup_codes.py")
    suggest_parser.add_argument("-o", "--output", default="entire_flavour.csv")
    suggest_parser.set_defaults(run=command_suggest)

    args = parser.parse_args()
    return args.run(args, cache_dir(args.cache))


if __name__ == "__main__":
    sys.exit(main())
