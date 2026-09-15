#!/usr/bin/env python3
"""Run the DICOM SEG metadata checks over a per-segment table.

Input is the CSV written by seg_attributes.py (or a BigQuery export of the view
in scripts/sql/01_seg_attributes.sql) — the two are the same contract.

Standard library only, so this runs wherever Python does.

Usage:
  python seg_checks.py seg_attributes.csv --outdir findings/
  python seg_checks.py seg_attributes.csv --outdir findings/ \\
      --codes findings/codes.csv --review review.csv

Computed checks (issues 2, 4, 5, 6, 7) need nothing but the data and will follow
a new delivery. Issue 8 needs the fully specified names from lookup_codes.py.
Issues 1 and 3 are curated: they need --review, and a review built against an
earlier batch is STALE until re-derived. See SKILL.md, "Core rule".
"""

import argparse
import csv
import sys
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

# Tag -> severity. CODE_AMBIGUOUS_ELSEWHERE is deliberately absent: it is a
# property of a code across the whole population, not evidence about the series
# carrying it, so it must not raise worstSeverity. See references/reporting.md.
SEVERITY = {
    "LATERALITY_INVERTED": "High",
    "ANATOMY_CONFLICT": "High",
    "CODE_SELF_INCONSISTENT": "High",
    "CODE_MEANING_MINORITY": "High",
    "TRACKINGUID_CROSS_PATIENT": "High",
    "ENTIRE_CODE_FLAVOUR": "Medium",
    "LATERALITY_UNCODED": "Medium",
    "MALFORMED_SEGMENT": "Medium",
    "TRACKINGUID_AMBIGUOUS": "Medium",
    "COSMETIC_VARIANT": "Low",
    "CODE_MEANING_SPELLING": "Low",
    "TYPE_REPEATS_CATEGORY": "Low",
    "NO_SEGMENTS_OVERLAP": "Low",
}
TAG_ORDER = list(SEVERITY) + ["CODE_AMBIGUOUS_ELSEWHERE"]
RANK = {"High": 0, "Medium": 1, "Low": 2, "None": 3}

# Type 1 in the Segment Description Macro, DICOM PS3.3 Table C.8.20-4.
TYPE1 = [
    ("SegmentNumber", "SegmentNumber"),
    ("SegmentLabel", "SegmentLabel"),
    ("SegmentedPropertyCategoryCodeValue", "SegmentedPropertyCategoryCodeSequence"),
    ("SegmentedPropertyTypeCodeValue", "SegmentedPropertyTypeCodeSequence"),
    ("SegmentAlgorithmType", "SegmentAlgorithmType"),
]


def load(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def write(outdir, name, rows, columns):
    path = Path(outdir) / name
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def key(row):
    """Report columns every finding carries, so a row can be acted on alone."""
    return {
        "PatientID": row["PatientID"],
        "StudyInstanceUID": row["StudyInstanceUID"],
        "SeriesInstanceUID": row["SeriesInstanceUID"],
        "SegmentNumber": row["SegmentNumber"],
        "SegmentLabel": row["SegmentLabel"],
        "viewer_url": row["viewer_url"],
    }


# ---------------------------------------------------------------------------
# Issue 2 - one code, conflicting meanings. Computed. Run first: it needs no
# curation and hands you the shortlist that issues 1 and 3 then judge.
# ---------------------------------------------------------------------------


def _same_anatomy_written_twice(a, b):
    """Two CodeMeanings that look like the same anatomy spelled differently.

    A CANDIDATE only - whether two meanings are the same anatomy is a judgement,
    and the caller labels it as such. Two rules, chosen to separate cosmetic
    variants from genuine granularity differences:

      * the token sets differ by at most one word, which catches
        "Left adrenal gland" / "Left adrenal" and "Cardiophrenic angle lymph
        node" / "Cardiophrenic lymph node", while leaving "Lung" / "Nodule of
        lung" and "Sternum" / "body of sternum" alone - those are
        NARROWER_OR_BROADER, a different and more serious finding
      * near-identical strings, which catches typos such as
        "Right axilary lymph node" / "Right axillary lymph node"
    """
    left, right = a.lower().strip(), b.lower().strip()
    if left == right:
        return True
    tokens_left = set(left.replace(",", " ").split())
    tokens_right = set(right.replace(",", " ").split())
    if tokens_left and tokens_right:
        if len(tokens_left ^ tokens_right) <= 1:
            return True
    return SequenceMatcher(None, left, right).ratio() >= 0.85


def ambiguous_codes(rows):
    meanings = defaultdict(Counter)
    for row in rows:
        code = row["AnatomicRegionCodeValue"]
        if code:
            meanings[code][row["AnatomicRegionCodeMeaning"]] += 1

    ambiguous = {c: m for c, m in meanings.items() if len(m) > 1}
    # Dominant = most used, ties broken alphabetically, as in the SQL.
    dominant = {
        code: sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        for code, counter in ambiguous.items()
    }

    # A series that uses ONE code with two meanings by itself is the strongest
    # evidence available, and does not depend on the rest of the population.
    per_series = defaultdict(lambda: defaultdict(set))
    for row in rows:
        if row["AnatomicRegionCodeValue"]:
            per_series[row["SeriesInstanceUID"]][row["AnatomicRegionCodeValue"]].add(
                row["AnatomicRegionCodeMeaning"]
            )
    self_inconsistent = {
        series
        for series, codes in per_series.items()
        if any(len(m) > 1 for m in codes.values())
    }

    cosmetic = set()
    for code, counter in ambiguous.items():
        variants = list(counter)
        if all(
            _same_anatomy_written_twice(a, b)
            for i, a in enumerate(variants)
            for b in variants[i + 1 :]
        ):
            cosmetic.add(code)

    findings = []
    for row in rows:
        code = row["AnatomicRegionCodeValue"]
        if code not in ambiguous:
            continue
        if row["SeriesInstanceUID"] in self_inconsistent:
            scope = "SELF_INCONSISTENT"
        elif row["AnatomicRegionCodeMeaning"] != dominant[code]:
            scope = "MINORITY_MEANING"
        else:
            scope = "DOMINANT_MEANING"
        finding = key(row)
        finding.update(
            {
                "AnatomicRegionCodeValue": code,
                "AnatomicRegionCodeMeaning": row["AnatomicRegionCodeMeaning"],
                "dominantMeaning": dominant[code],
                "distinctMeanings": len(ambiguous[code]),
                "scope": scope,
                "cosmeticCandidate": code in cosmetic,
            }
        )
        findings.append(finding)

    findings.sort(
        key=lambda f: (
            {"SELF_INCONSISTENT": 0, "MINORITY_MEANING": 1}.get(f["scope"], 2),
            -f["distinctMeanings"],
            f["AnatomicRegionCodeValue"],
            f["PatientID"],
        )
    )
    return findings, ambiguous, cosmetic


# ---------------------------------------------------------------------------
# Issue 4 - Type 1 conformance. Computed.
# ---------------------------------------------------------------------------


def malformed(rows):
    findings = []
    for row in rows:
        missing = [label for column, label in TYPE1 if not row[column]]
        # A code value without its scheme designator does not identify a concept.
        for prefix in (
            "SegmentedPropertyCategory",
            "SegmentedPropertyType",
            "AnatomicRegion",
        ):
            if row[f"{prefix}CodeValue"] and not row[
                f"{prefix}CodingSchemeDesignator"
            ]:
                missing.append(f"{prefix}CodeSequence.CodingSchemeDesignator")
        if not missing:
            continue
        finding = key(row)
        finding.update(
            {
                "SOPInstanceUID": row["SOPInstanceUID"],
                "missingType1Attributes": ", ".join(missing),
                "SeriesDescription": row["SeriesDescription"],
            }
        )
        findings.append(finding)
    return findings


# ---------------------------------------------------------------------------
# Issue 5 - type repeats category. Issue 6 - SegmentsOverlap absent (object
# level, so one row per SOP instance). Both computed.
# ---------------------------------------------------------------------------


def type_repeats_category(rows):
    findings = []
    for row in rows:
        type_code = row["SegmentedPropertyTypeCodeValue"]
        if (
            type_code
            and type_code == row["SegmentedPropertyCategoryCodeValue"]
            and row["SegmentedPropertyTypeCodingSchemeDesignator"]
            == row["SegmentedPropertyCategoryCodingSchemeDesignator"]
        ):
            finding = key(row)
            finding.update(
                {
                    "repeatedCodeValue": type_code,
                    "repeatedCodeMeaning": row["SegmentedPropertyCategoryCodeMeaning"],
                    "AnatomicRegionCodeMeaning": row["AnatomicRegionCodeMeaning"],
                }
            )
            findings.append(finding)
    return findings


def segments_overlap_absent(rows):
    objects = defaultdict(list)
    for row in rows:
        objects[row["SOPInstanceUID"]].append(row)
    findings = []
    for sop, group in objects.items():
        if group[0]["SegmentsOverlap"]:
            continue
        finding = {
            "PatientID": group[0]["PatientID"],
            "StudyInstanceUID": group[0]["StudyInstanceUID"],
            "SeriesInstanceUID": group[0]["SeriesInstanceUID"],
            "SOPInstanceUID": sop,
            # Only above 1 does the missing attribute withhold anything.
            "segmentsInObject": len(group),
            "SeriesDescription": group[0]["SeriesDescription"],
            "viewer_url": group[0]["viewer_url"],
        }
        findings.append(finding)
    findings.sort(key=lambda f: (-f["segmentsInObject"], f["PatientID"]))
    return findings


# ---------------------------------------------------------------------------
# Issue 7 - TrackingUID sharing. Computed.
# ---------------------------------------------------------------------------


def tracking_uid(rows):
    groups = defaultdict(list)
    for row in rows:
        if row["TrackingUID"]:
            groups[row["TrackingUID"]].append(row)

    findings = []
    for uid, group in groups.items():
        if len(group) < 2:
            continue
        patients = {r["PatientID"] for r in group}
        studies = {r["StudyInstanceUID"] for r in group}
        series = {r["SeriesInstanceUID"] for r in group}
        seed = any("SEED POINT" in (r["SeriesDescription"] or "").upper() for r in group)

        if len(patients) > 1:
            # Never seen in the batches this came from, and a serious problem if
            # it appears: one identifier spanning two people.
            pattern = "CROSS_PATIENT"
        elif len(studies) > 1:
            pattern = "LONGITUDINAL"      # the intended use
        elif len(series) == 1:
            pattern = "WITHIN_SERIES"     # repeated inside one series
        elif seed:
            pattern = "SEED_AND_LESION"
        else:
            pattern = "WITHIN_STUDY"      # unclear: annotated twice, or reused

        findings.append(
            {
                "PatientID": sorted(patients)[0],
                "StudyInstanceUID": sorted(studies)[0],
                "seriesInstanceUIDs": "; ".join(sorted(series)),
                "TrackingUID": uid,
                "sharingPattern": pattern,
                "segments": len(group),
                "patients": len(patients),
                "studies": len(studies),
                "series": len(series),
                "segmentLabels": "; ".join(sorted({r["SegmentLabel"] for r in group})),
                "viewer_url": group[0]["viewer_url"],
            }
        )
    order = {
        "CROSS_PATIENT": 0,
        "WITHIN_SERIES": 1,
        "WITHIN_STUDY": 2,
        "SEED_AND_LESION": 3,
        "LONGITUDINAL": 4,
    }
    findings.sort(key=lambda f: (order[f["sharingPattern"]], f["PatientID"]))
    return findings


# ---------------------------------------------------------------------------
# Issue 8 - "Entire X" flavour, from the FSNs lookup_codes.py resolved.
# Issues 1 and 3 - curated verdicts.
# ---------------------------------------------------------------------------


def entire_flavour(rows, codes_csv):
    entire = {}
    for code in load(codes_csv):
        if str(code.get("isEntireFlavour", "")).lower() == "true":
            entire[code["CodeValue"]] = code.get("fsn", "")
    findings = []
    for row in rows:
        code = row["AnatomicRegionCodeValue"]
        if code in entire:
            finding = key(row)
            finding.update(
                {
                    "AnatomicRegionCodeValue": code,
                    "entireFsn": entire[code],
                    "meaningRecorded": row["AnatomicRegionCodeMeaning"],
                }
            )
            findings.append(finding)
    findings.sort(key=lambda f: (f["AnatomicRegionCodeValue"], f["PatientID"]))
    return findings, entire


def curated(rows, review_csv):
    """Join the curated verdicts on (CodeValue, CodingScheme, CodeMeaning).

    The review is keyed on the PAIRING, not on the code: the same code can be
    correct under one meaning and wrong under another, which is exactly what
    issue 2 surfaces.
    """
    review = {}
    for entry in load(review_csv):
        review[
            (
                entry["CodeValue"],
                entry.get("CodingSchemeDesignator", ""),
                entry["meaningRecorded"],
            )
        ] = entry

    issue1, issue3 = [], []
    for row in rows:
        entry = review.get(
            (
                row["AnatomicRegionCodeValue"],
                row["AnatomicRegionCodingSchemeDesignator"],
                row["AnatomicRegionCodeMeaning"],
            )
        )
        if not entry:
            continue
        finding = key(row)
        finding.update(
            {
                "AnatomicRegionCodeValue": row["AnatomicRegionCodeValue"],
                "codeActuallyMeans": entry.get("codeActuallyMeans", ""),
                "meaningRecorded": row["AnatomicRegionCodeMeaning"],
                "verdict": entry["verdict"],
                "reviewSource": entry.get("reviewSource", ""),
            }
        )
        if entry["verdict"] in ("INVERTED", "UNCODED"):
            issue3.append(finding)
        else:
            issue1.append(finding)

    rank1 = {"WRONG_ANATOMY": 0, "NARROWER_OR_BROADER": 1, "SPELLING": 2}
    issue1.sort(key=lambda f: (rank1.get(f["verdict"], 3), f["AnatomicRegionCodeValue"]))
    issue3.sort(key=lambda f: (f["verdict"] != "INVERTED", f["AnatomicRegionCodeValue"]))
    return issue1, issue3


# ---------------------------------------------------------------------------
# Per-series triage
# ---------------------------------------------------------------------------


def triage(rows, ambiguous, cosmetic, entire, verdicts, tracking):
    ambiguous_tracking = {
        f["TrackingUID"] for f in tracking if f["sharingPattern"] == "WITHIN_STUDY"
    }
    cross_patient = {
        f["TrackingUID"] for f in tracking if f["sharingPattern"] == "CROSS_PATIENT"
    }
    dominant = {
        code: sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        for code, counter in ambiguous.items()
    }
    per_series = defaultdict(lambda: defaultdict(set))
    for row in rows:
        if row["AnatomicRegionCodeValue"]:
            per_series[row["SeriesInstanceUID"]][row["AnatomicRegionCodeValue"]].add(
                row["AnatomicRegionCodeMeaning"]
            )
    self_inconsistent = {
        series
        for series, codes in per_series.items()
        if any(len(m) > 1 for m in codes.values())
    }

    series_rows = defaultdict(list)
    for row in rows:
        series_rows[row["SeriesInstanceUID"]].append(row)

    out = []
    for series, group in series_rows.items():
        counts = Counter()
        offending = set()
        for row in group:
            code = row["AnatomicRegionCodeValue"]
            verdict = verdicts.get(
                (
                    code,
                    row["AnatomicRegionCodingSchemeDesignator"],
                    row["AnatomicRegionCodeMeaning"],
                ),
                "",
            )
            if verdict == "INVERTED":
                counts["LATERALITY_INVERTED"] += 1
            elif verdict == "UNCODED":
                counts["LATERALITY_UNCODED"] += 1
            elif verdict in ("WRONG_ANATOMY", "NARROWER_OR_BROADER"):
                counts["ANATOMY_CONFLICT"] += 1
            elif verdict == "SPELLING":
                counts["CODE_MEANING_SPELLING"] += 1
            if verdict in ("INVERTED", "WRONG_ANATOMY", "NARROWER_OR_BROADER"):
                offending.add(f'{code} "{row["AnatomicRegionCodeMeaning"]}"')

            if code in ambiguous:
                counts["CODE_AMBIGUOUS_ELSEWHERE"] += 1
                if series in self_inconsistent:
                    counts["CODE_SELF_INCONSISTENT"] += 1
                elif row["AnatomicRegionCodeMeaning"] != dominant[code]:
                    counts["CODE_MEANING_MINORITY"] += 1
                if code in cosmetic:
                    counts["COSMETIC_VARIANT"] += 1
            if code in entire:
                counts["ENTIRE_CODE_FLAVOUR"] += 1
            if any(not row[column] for column, _ in TYPE1):
                counts["MALFORMED_SEGMENT"] += 1
            if row["TrackingUID"] in ambiguous_tracking:
                counts["TRACKINGUID_AMBIGUOUS"] += 1
            if row["TrackingUID"] in cross_patient:
                counts["TRACKINGUID_CROSS_PATIENT"] += 1
            type_code = row["SegmentedPropertyTypeCodeValue"]
            if type_code and type_code == row["SegmentedPropertyCategoryCodeValue"]:
                counts["TYPE_REPEATS_CATEGORY"] += 1
            if not row["SegmentsOverlap"]:
                counts["NO_SEGMENTS_OVERLAP"] += 1

        tags = [t for t in TAG_ORDER if counts[t]]
        severities = [SEVERITY[t] for t in tags if t in SEVERITY]
        worst = min(severities, key=lambda s: RANK[s]) if severities else "None"

        out.append(
            {
                "PatientID": group[0]["PatientID"],
                "StudyInstanceUID": group[0]["StudyInstanceUID"],
                "SeriesInstanceUID": series,
                "issues": "; ".join(tags),
                "worstSeverity": worst,
                "segmentCount": len(group),
                # The work queue: segments needing a per-segment human decision.
                "segmentsNeedingRecode": counts["ANATOMY_CONFLICT"]
                + counts["LATERALITY_INVERTED"],
                "offendingCodes": "; ".join(sorted(offending)),
                "SeriesDescription": group[0]["SeriesDescription"],
                "viewer_url": group[0]["viewer_url"],
            }
        )

    out.sort(
        key=lambda r: (
            RANK[r["worstSeverity"]],
            -r["segmentsNeedingRecode"],
            r["PatientID"],
            r["SeriesInstanceUID"],
        )
    )
    return out


# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Run the DICOM SEG metadata checks over a per-segment table.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("table", help="CSV from seg_attributes.py")
    parser.add_argument("--outdir", default="findings")
    parser.add_argument(
        "--codes", help="codes.csv from lookup_codes.py; enables issue 8"
    )
    parser.add_argument(
        "--review", help="curated verdict CSV; enables issues 1 and 3"
    )
    parser.add_argument(
        "--include-background",
        action="store_true",
        help="keep labelmap Background segments (SegmentNumber 0). They are an "
        "artefact of the encoding, not findings, and will trip the "
        "conformance check.",
    )
    args = parser.parse_args()

    rows = load(args.table)
    if not rows:
        sys.exit(f"{args.table} is empty")

    total = len(rows)
    if not args.include_background:
        rows = [r for r in rows if r["isBackgroundSegment"] != "True"]
    multi = sum(1 for r in rows if r["multiValuedCodeSequence"] == "True")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"{total} segment rows; {len(rows)} after excluding Background segments")
    if multi:
        print(
            f"\n  !! {multi} segments carry more than one code per sequence. Every "
            "count below\n     understates - handle those by hand.\n"
        )

    # ---- computed
    amb, ambiguous, cosmetic = ambiguous_codes(rows)
    write(outdir, "issue2_ambiguous_code.csv", amb,
          ["PatientID", "StudyInstanceUID", "SeriesInstanceUID", "SegmentNumber",
           "SegmentLabel", "AnatomicRegionCodeValue", "AnatomicRegionCodeMeaning",
           "dominantMeaning", "distinctMeanings", "scope", "cosmeticCandidate",
           "viewer_url"])
    scopes = Counter(f["scope"] for f in amb)
    print(f"Issue 2  ambiguous codes  {len(ambiguous):>5} codes, {len(amb):>6} segments")
    for scope in ("SELF_INCONSISTENT", "MINORITY_MEANING", "DOMINANT_MEANING"):
        note = "  <- not evidence about the segment" if scope.startswith("DOM") else ""
        print(f"           {scope:<20} {scopes[scope]:>6}{note}")

    mal = malformed(rows)
    write(outdir, "issue4_malformed_segment.csv", mal,
          ["PatientID", "StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID",
           "SegmentNumber", "SegmentLabel", "missingType1Attributes",
           "SeriesDescription", "viewer_url"])
    print(f"Issue 4  malformed        {len(mal):>6} segments")

    rep = type_repeats_category(rows)
    write(outdir, "issue5_type_repeats_category.csv", rep,
          ["PatientID", "StudyInstanceUID", "SeriesInstanceUID", "SegmentNumber",
           "SegmentLabel", "repeatedCodeValue", "repeatedCodeMeaning",
           "AnatomicRegionCodeMeaning", "viewer_url"])
    print(f"Issue 5  type = category  {len(rep):>6} segments")

    overlap = segments_overlap_absent(rows)
    multiseg = sum(1 for f in overlap if f["segmentsInObject"] > 1)
    write(outdir, "issue6_segments_overlap_absent.csv", overlap,
          ["PatientID", "StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID",
           "segmentsInObject", "SeriesDescription", "viewer_url"])
    print(f"Issue 6  no SegmentsOverlap {len(overlap):>4} objects "
          f"({multiseg} multi-segment; Type 3, so conformant)")

    track = tracking_uid(rows)
    write(outdir, "issue7_tracking_uid.csv", track,
          ["PatientID", "StudyInstanceUID", "seriesInstanceUIDs", "TrackingUID",
           "sharingPattern", "segments", "patients", "studies", "series",
           "segmentLabels", "viewer_url"])
    patterns = Counter(f["sharingPattern"] for f in track)
    print(f"Issue 7  shared TrackingUID {len(track):>4} UIDs")
    for pattern, count in patterns.most_common():
        print(f"           {pattern:<20} {count:>6}")
    # Reassuring negatives bound the problem; report them, not only the breaks.
    for pattern, note in (
        ("CROSS_PATIENT", "no TrackingUID is shared across patients"),
        ("WITHIN_SERIES", "no TrackingUID is repeated within a single series"),
    ):
        if not patterns[pattern]:
            print(f"           ok: {note}")

    # ---- needs the FSN lookup
    entire = {}
    if args.codes:
        ent, entire = entire_flavour(rows, args.codes)
        write(outdir, "issue8_entire_code_flavour.csv", ent,
              ["PatientID", "StudyInstanceUID", "SeriesInstanceUID", "SegmentNumber",
               "SegmentLabel", "AnatomicRegionCodeValue", "entireFsn",
               "meaningRecorded", "viewer_url"])
        print(f'Issue 8  "Entire X" codes {len(entire):>5} codes, {len(ent):>6} segments')
    else:
        print("Issue 8  skipped - run lookup_codes.py and pass --codes")

    # ---- curated
    verdicts = {}
    if args.review:
        one, three = curated(rows, args.review)
        columns = ["PatientID", "StudyInstanceUID", "SeriesInstanceUID",
                   "SegmentNumber", "SegmentLabel", "AnatomicRegionCodeValue",
                   "codeActuallyMeans", "meaningRecorded", "verdict",
                   "reviewSource", "viewer_url"]
        write(outdir, "issue1_anatomy_conflict.csv", one, columns)
        write(outdir, "issue3_laterality.csv", three, columns)
        for entry in load(args.review):
            verdicts[
                (entry["CodeValue"], entry.get("CodingSchemeDesignator", ""),
                 entry["meaningRecorded"])
            ] = entry["verdict"]
        v1 = Counter(f["verdict"] for f in one)
        v3 = Counter(f["verdict"] for f in three)
        print(f"Issue 1  anatomy conflict {len(one):>6} segments  "
              + ", ".join(f"{k}={v}" for k, v in v1.most_common()))
        print(f"Issue 3  laterality       {len(three):>6} segments  "
              + ", ".join(f"{k}={v}" for k, v in v3.most_common()))
    else:
        print("Issues 1, 3  skipped - curated, need --review "
              "(see references/terminology.md)")

    # ---- roll-up
    series = triage(rows, ambiguous, cosmetic, entire, verdicts, track)
    path = write(outdir, "series_triage.csv", series,
                 ["PatientID", "StudyInstanceUID", "SeriesInstanceUID", "issues",
                  "worstSeverity", "segmentCount", "segmentsNeedingRecode",
                  "offendingCodes", "SeriesDescription", "viewer_url"])
    worst = Counter(r["worstSeverity"] for r in series)
    print(f"\n{len(series)} series -> {path}")
    print("  " + ", ".join(f"{worst[s]} {s}" for s in ("High", "Medium", "Low", "None")))
    recode = sum(r["segmentsNeedingRecode"] for r in series)
    if recode:
        print(f"  work queue: {recode} segments across "
              f"{sum(1 for r in series if r['segmentsNeedingRecode'])} series")
    if not args.review:
        print("\n  Counts above exclude issues 1 and 3, which are curated. A review "
              "built\n  against an earlier batch is STALE until re-derived - say so "
              "in the report.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
