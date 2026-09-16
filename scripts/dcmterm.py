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
PARQUET = ("codes_unique.parquet", "coded_entries.parquet", "context_groups.parquet")
PROVENANCE = ("extraction_metadata.json", "version.json")

COVERAGE_COLUMNS = [
    "CodingSchemeDesignator",
    "CodeValue",
    "codeSequences",
    "meaningsInBatch",
    "distinctMeanings",
    "segments",
    "series",
    "inDcmterm",
    "isPrivateScheme",
    "dcmMeanings",
    "meaningAgrees",
    "numCids",
    "cids",
]

# The code sequences `coverage` reads by default. The anatomic region is Type 3
# and the other two Type 1, so on many deliveries the organ is the TYPE code
# and the region is absent; a coverage figure computed on the region alone
# would then describe an empty column.
DEFAULT_COLUMNS = [
    "AnatomicRegionCodeValue",
    "SegmentedPropertyTypeCodeValue",
    "SegmentedPropertyCategoryCodeValue",
]

# Issue 15. PS3.3 Table C.8.20-4 gives the Segmented Property Category Code
# Sequence (0062,0003) BCID 7150 and the Segmented Property Type Code Sequence
# (0062,000F) BCID 7151. Both are BASELINE, so a code outside them is
# permitted - which is why membership alone is Low. CID 7150 additionally
# names, per category, the context group its types come from (its
# "Segmentation Property Type Context Group" column; dcmterms carries it as
# context_group_cid), and a type outside the CID its own category names
# contradicts the category - Medium.
SEGMENTATION_PROPERTY_CATEGORY_CID = 7150
SEGMENTATION_PROPERTY_TYPE_CID = 7151
RETIRED_SCHEMES = {"SRT", "SNM3", "SNM", "99SDM"}

PROPERTY_COLUMNS = [
    "categoryScheme",
    "categoryCode",
    "categoryMeaning",
    "typeScheme",
    "typeCode",
    "typeMeaning",
    "segments",
    "series",
    "categoryInCid7150",
    "typeInCid7151",
    "categoryTypeCid",
    "categoryTypeCidName",
    "typeInCategoryCid",
    "propertyIssue",
    "note",
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


def dcm_context_groups(cache):
    """cid -> {"name", "includes": [cid, ...]} from context_groups.parquet.

    `includes` is the CID's own "Include CID" lines, one level deep;
    cid_members follows them transitively. CID 7151 is nothing BUT includes -
    nine of them, each including more - so without this table the type
    context group has no members at all.
    """
    groups = {}
    for row in read_parquet(cache / "context_groups.parquet"):
        number = int(row.get("cid_number") or 0)
        if not number:
            continue
        includes = [int(c) for c in str(row.get("includes") or "").split(",")
                    if c.strip().isdigit()]
        groups[number] = {"name": (row.get("cid_name") or "").strip(),
                          "includes": includes}
    return groups


def codes_by_cid(cache):
    """(direct, links) from coded_entries.

    direct: cid -> {(scheme, code)} of the entries listed IN that CID, before
    includes. links: (cid, scheme, code) -> cid, the context group a coded
    entry points at - CID 7150's "Segmentation Property Type Context Group"
    column, which names the type CID each category draws on.
    """
    direct = defaultdict(set)
    links = {}
    for row in read_parquet(cache / "coded_entries.parquet"):
        cid = int(row.get("cid_number") or 0)
        scheme = (row.get("coding_scheme_designator") or "").strip()
        value = (row.get("code_value") or "").strip()
        if not cid or not value:
            continue
        direct[cid].add((scheme, value))
        linked = row.get("context_group_cid")
        if linked:
            links[(cid, scheme, value)] = int(linked)
    return direct, links


def cid_members(cid, groups, direct, _seen=None):
    """Every (scheme, code) in a context group, following Include CID transitively."""
    seen = _seen if _seen is not None else set()
    if cid in seen:
        return set()
    seen.add(cid)
    members = set(direct.get(cid, ()))
    for included in groups.get(cid, {}).get("includes", []):
        members |= cid_members(included, groups, direct, seen)
    return members


def batch_codes(table, value_column, scheme_column=None, meaning_column=None):
    """Distinct codes in the batch, with what depends on each.

    `value_column` is one CodeValue column or a list of them; the scheme and
    meaning columns follow from the prefix unless given. A code used in two
    sequences is one entry whose `sequences` names both. Same shape as
    lookup_codes.collect - Background segments excluded, since they are an
    artefact of labelmap encoding rather than something segmented.
    """
    columns = [value_column] if isinstance(value_column, str) else list(value_column)
    codes = defaultdict(lambda: {"meanings": set(), "segments": 0, "series": set(),
                                 "sequences": set()})
    with open(table, newline="") as handle:
        for row in csv.DictReader(handle):
            if (row.get("isBackgroundSegment") or "").strip().lower() == "true":
                continue
            for column in columns:
                prefix = column.replace("CodeValue", "")
                scheme_col = scheme_column if len(columns) == 1 and scheme_column \
                    else f"{prefix}CodingSchemeDesignator"
                meaning_col = meaning_column if len(columns) == 1 and meaning_column \
                    else f"{prefix}CodeMeaning"
                value = (row.get(column) or "").strip()
                if not value:
                    continue
                entry = codes[((row.get(scheme_col) or "").strip(), value)]
                entry["meanings"].add((row.get(meaning_col) or "").strip())
                entry["segments"] += 1
                entry["series"].add(row.get("SeriesInstanceUID", ""))
                entry["sequences"].add(prefix)
    return codes


def property_pairs(table):
    """Distinct (category, type) pairings in the batch, with what depends on each."""
    pairs = defaultdict(lambda: {"segments": 0, "series": set()})
    columns = (
        "SegmentedPropertyCategoryCodingSchemeDesignator",
        "SegmentedPropertyCategoryCodeValue",
        "SegmentedPropertyCategoryCodeMeaning",
        "SegmentedPropertyTypeCodingSchemeDesignator",
        "SegmentedPropertyTypeCodeValue",
        "SegmentedPropertyTypeCodeMeaning",
    )
    with open(table, newline="") as handle:
        for row in csv.DictReader(handle):
            if (row.get("isBackgroundSegment") or "").strip().lower() == "true":
                continue
            pair = tuple((row.get(c) or "").strip() for c in columns)
            if not pair[1] and not pair[4]:
                continue
            pairs[pair]["segments"] += 1
            pairs[pair]["series"].add(row.get("SeriesInstanceUID", ""))
    return pairs


def property_check(pairs, groups, direct, links):
    """Issue 15: each (category, type) pairing against its context groups.

    Three findings, per pairing:
      CATEGORY_NOT_IN_CID     the category is not in CID 7150. Baseline, so
                              permitted; Low.
      TYPE_NOT_IN_CID         the type is not in CID 7151 (transitively).
                              Baseline, so permitted; Low.
      TYPE_OUTSIDE_CATEGORY   the type IS a segmentation property type, but
                              not one of the CID that its own category names
                              in CID 7150 - "Anatomical Structure" over a
                              lesion type, say. The two Type 1 attributes
                              contradict each other; Medium.
    Private (99...) and retired (SRT...) schemes are not judged here: the
    first must be judged against whatever defines them, the second is issue 10.
    """
    categories = cid_members(SEGMENTATION_PROPERTY_CATEGORY_CID, groups, direct)
    types = cid_members(SEGMENTATION_PROPERTY_TYPE_CID, groups, direct)
    per_category = {}
    rows = []
    for (cs, cc, cm, ts, tc, tm), info in pairs.items():
        issues, notes = [], []

        def judgeable(scheme):
            if is_private(scheme):
                notes.append(f"{scheme}: private scheme - judge against its definition")
                return False
            if scheme in RETIRED_SCHEMES:
                notes.append(f"{scheme}: retired scheme - issue 10; re-code first")
                return False
            return True

        category_in = (cs, cc) in categories if cc else None
        type_in = (ts, tc) in types if tc else None
        category_cid = links.get((SEGMENTATION_PROPERTY_CATEGORY_CID, cs, cc))
        type_in_category = None
        if cc and judgeable(cs) and not category_in:
            issues.append("CATEGORY_NOT_IN_CID")
        if tc and judgeable(ts):
            if not type_in:
                issues.append("TYPE_NOT_IN_CID")
            elif category_in and category_cid:
                if category_cid not in groups:
                    notes.append(f"CID {category_cid} is not in dcmterms; category/type "
                                 "consistency not judged")
                else:
                    if category_cid not in per_category:
                        per_category[category_cid] = cid_members(category_cid, groups, direct)
                    type_in_category = (ts, tc) in per_category[category_cid]
                    if not type_in_category:
                        issues.append("TYPE_OUTSIDE_CATEGORY")
        rows.append({
            "categoryScheme": cs, "categoryCode": cc, "categoryMeaning": cm,
            "typeScheme": ts, "typeCode": tc, "typeMeaning": tm,
            "segments": info["segments"], "series": len(info["series"]),
            "categoryInCid7150": "" if category_in is None else str(category_in),
            "typeInCid7151": "" if type_in is None else str(type_in),
            "categoryTypeCid": category_cid or "",
            "categoryTypeCidName": groups.get(category_cid, {}).get("name", "")
            if category_cid else "",
            "typeInCategoryCid": "" if type_in_category is None else str(type_in_category),
            "propertyIssue": "; ".join(issues),
            "note": "; ".join(dict.fromkeys(notes)),
        })
    severity = {"TYPE_OUTSIDE_CATEGORY": 0, "TYPE_NOT_IN_CID": 1, "CATEGORY_NOT_IN_CID": 1}
    return sorted(rows, key=lambda r: (
        min((severity[i] for i in r["propertyIssue"].split("; ") if i), default=9),
        -r["segments"], r["categoryCode"], r["typeCode"]))


def is_private(scheme):
    """99-prefixed designators are private by DICOM PS3.3 section 8.2.

    Their absence from dcmterms is expected, not a finding - they are
    placeholders for structures with no standard code, and must be judged
    against whatever defines them.
    """
    return scheme.startswith("99")


def coverage(codes, reference, cids=None):
    """One row per distinct batch code: is it a code DICOM uses, and with this meaning?

    `cids` is dcm_cids' table, if the caller has it; it fills the `cids`
    column with the context groups the code appears in.
    """
    rows = []
    for (scheme, value), info in codes.items():
        meanings = sorted(m for m in info["meanings"] if m)
        entry = reference.get((scheme, value))
        row = {
            "CodingSchemeDesignator": scheme,
            "CodeValue": value,
            "codeSequences": "; ".join(sorted(info.get("sequences", ()))),
            "meaningsInBatch": " | ".join(meanings),
            "distinctMeanings": len(meanings),
            "segments": info["segments"],
            "series": len(info["series"]),
            "inDcmterm": str(entry is not None),
            "isPrivateScheme": str(is_private(scheme)),
            "dcmMeanings": "",
            "meaningAgrees": "",
            "numCids": "",
            "cids": "",
        }
        if entry is not None:
            agreeing = sum(1 for m in meanings if normalise(m) in entry["normalised"])
            row["dcmMeanings"] = " | ".join(entry["meanings"])
            row["numCids"] = entry["numCids"]
            row["meaningAgrees"] = (
                "True" if agreeing == len(meanings) and meanings
                else "False" if agreeing == 0
                else "PARTIAL")
            if cids:
                row["cids"] = "; ".join(
                    str(n) for n in sorted({c[0] for c in cids.get((scheme, value), ())}))
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
    columns = args.column or DEFAULT_COLUMNS
    codes = batch_codes(args.table, columns)
    if not codes:
        sys.exit(f"no values in {', '.join(columns)}")
    print(f"  {len(codes)} distinct codes across {', '.join(columns)}", file=sys.stderr)

    rows = coverage(codes, dcm_codes(cache), dcm_cids(cache))
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


def command_property(args, cache):
    fetch(cache, quiet=True)
    report_provenance(cache)
    groups = dcm_context_groups(cache)
    direct, links = codes_by_cid(cache)
    pairs = property_pairs(args.table)
    if not pairs:
        sys.exit("no SegmentedPropertyCategory or SegmentedPropertyType codes in the table")

    rows = property_check(pairs, groups, direct, links)
    write(args.output, rows, PROPERTY_COLUMNS)

    segments = sum(r["segments"] for r in rows)
    kinds = defaultdict(int)
    for row in rows:
        for issue in row["propertyIssue"].split("; "):
            if issue:
                kinds[issue] += row["segments"]
    categories = {(r["categoryScheme"], r["categoryCode"]) for r in rows if r["categoryCode"]}
    types = {(r["typeScheme"], r["typeCode"]) for r in rows if r["typeCode"]}
    print(f"\n  {len(pairs)} distinct (category, type) pairings over {segments} "
          f"segments: {len(categories)} categories, {len(types)} types",
          file=sys.stderr)
    if not kinds:
        print("  ok: every category is in CID 7150, every type in CID 7151, and "
              "every type\n  in the context group its category names", file=sys.stderr)
    for issue, label in (
        ("TYPE_OUTSIDE_CATEGORY",
         "type is a property type, but not of this CATEGORY (Medium)"),
        ("TYPE_NOT_IN_CID", "type not in CID 7151 - baseline, so permitted (Low)"),
        ("CATEGORY_NOT_IN_CID", "category not in CID 7150 - baseline (Low)"),
    ):
        if kinds[issue]:
            print(f"  {kinds[issue]:>6} segments  {issue:<22} {label}", file=sys.stderr)
    shown = 0
    for row in rows:
        if row["propertyIssue"] and shown < 12:
            print(f"    {row['segments']:>5}  {row['categoryCode']:<11} "
                  f"{row['categoryMeaning'][:24]:<24} / {row['typeCode']:<11} "
                  f"{row['typeMeaning'][:24]:<24} {row['propertyIssue']}",
                  file=sys.stderr)
            shown += 1
    print("\n  PS3.3 Table C.8.20-4: BCID 7150 for (0062,0003), BCID 7151 for "
          "(0062,000F). Baseline, so membership\n  is not conformance - but a type "
          "outside the CID its own category names contradicts the category.\n  Fold "
          "into the triage list with: seg_checks.py --property " + str(args.output),
          file=sys.stderr)
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
        "--column", action="append", metavar="CODEVALUE_COLUMN",
        help="which CodeValue column to check; repeatable. Default: the anatomic "
             "region, the segmented property type AND the segmented property "
             "category, since the anatomy is often the type code with no region "
             "at all. Name one column to narrow it.")
    coverage_parser.set_defaults(run=command_coverage)

    property_parser = sub.add_parser(
        "property",
        help="issue 15: category and type codes against CID 7150 / 7151, and "
             "each type against the context group its category names")
    property_parser.add_argument("table", help="CSV from seg_attributes.py")
    property_parser.add_argument("-o", "--output",
                                 default="issue15_property_context_group.csv")
    property_parser.set_defaults(run=command_property)

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
