#!/usr/bin/env python3
"""Run the DICOM SEG metadata checks over a per-segment table.

Input is the CSV written by seg_attributes.py (or a BigQuery export of the view
in scripts/sql/01_seg_attributes.sql) — the two are the same contract.

Standard library only, so this runs wherever Python does.

Usage:
  python seg_checks.py seg_attributes.csv --outdir findings/
  python seg_checks.py seg_attributes.csv --outdir findings/ \\
      --codes findings/codes.csv --review review.csv \\
      --iod findings/issue9_iod_validation.csv \\
      --encoding findings/encoding_per_object.csv

Computed checks (issues 2, 4, 5, 6, 7, 10, 13, 14) need nothing but the data
and will follow a new delivery. Issue 8 needs the fully specified names from
lookup_codes.py. Issue 9 is the IOD validator's verdict, read from
dciodvfy_check.py via --iod, and issues 11 and 12 come from seg_encoding.py
via --encoding; all three need the files, so they are unavailable on the
BigQuery and DICOMweb paths. Issues 1 and 3 are curated: they need --review,
and a review built against an earlier batch is STALE until re-derived. See
SKILL.md, "Core rule".
"""

import argparse
import csv
import sys
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cielab  # noqa: E402  - same directory, stdlib only

# Tag -> severity. CODE_AMBIGUOUS_ELSEWHERE is deliberately absent: it is a
# property of a code across the whole population, not evidence about the series
# carrying it, so it must not raise worstSeverity. See references/reporting.md.
SEVERITY = {
    "LATERALITY_INVERTED": "High",
    "ANATOMY_CONFLICT": "High",
    "CODE_SELF_INCONSISTENT": "High",
    "CODE_MEANING_MINORITY": "High",
    "TRACKINGUID_CROSS_PATIENT": "High",
    "LOSSY_COMPRESSED": "High",
    "ENTIRE_CODE_FLAVOUR": "Medium",
    "LATERALITY_UNCODED": "Medium",
    "MALFORMED_SEGMENT": "Medium",
    "RETIRED_CODING_SCHEME": "Medium",
    "IOD_ERROR": "Medium",
    "TRACKINGUID_AMBIGUOUS": "Medium",
    "EMPTY_SEGMENT": "Medium",
    "COLOR_DUPLICATE": "Medium",
    "COLOR_NOT_PERMITTED": "Medium",
    "COLOR_MALFORMED": "Medium",
    "ALGORITHM_NAME_MISSING": "Medium",
    "COSMETIC_VARIANT": "Low",
    "IOD_WARNING": "Low",
    "CODE_MEANING_SPELLING": "Low",
    "TYPE_REPEATS_CATEGORY": "Low",
    "NO_SEGMENTS_OVERLAP": "Low",
    "EMPTY_FRAMES_RETAINED": "Low",
    "UNCOMPRESSED": "Low",
    "COLOR_CONFUSABLE": "Low",
    "COLOR_INCONSISTENT": "Low",
    "COLOR_ABSENT": "Low",
    "ALGORITHM_UNIDENTIFIED": "Low",
}
TAG_ORDER = list(SEVERITY) + ["CODE_AMBIGUOUS_ELSEWHERE"]
RANK = {"High": 0, "Medium": 1, "Low": 2, "None": 3}

# Issue 10. Coding scheme designators DICOM has retired, and what replaces
# them. The replacement is NOT a rename: an SRT code and its SCT equivalent
# have different CodeValues (T-62000 -> 10200004), so swapping the designator
# alone produces a code that does not exist. dciodvfy reports these too, as a
# Warning; this check is here because it runs on the BigQuery and DICOMweb
# paths, where dciodvfy cannot.
RETIRED_SCHEMES = {
    "SRT": "SCT",
    "SNM3": "SCT",
    "SNM": "SCT",
    "99SDM": "SCT",
}

# Issue 14. SegmentAlgorithmName (0062,0009) is Type 1C - "Required if Segment
# Algorithm Type (0062,0008) is not MANUAL" (PS3.3 C.8.20.2) - so these two
# values are what make it required.
NON_MANUAL_ALGORITHM = {"AUTOMATIC", "SEMIAUTOMATIC"}

# Names that satisfy the letter of 1C while identifying nothing. Matched on the
# whole value, case-folded, after stripping punctuation: a name is a name, and
# "unknown" is not one. Toolkit names are here because a converter's name says
# what wrote the object, not what segmented it.
UNINFORMATIVE_ALGORITHM_NAMES = {
    "", "-", "n/a", "na", "none", "null", "nil", "unknown", "unspecified",
    "not specified", "not applicable", "tbd", "todo", "test", "default",
    "algorithm", "segmentation", "segment", "auto", "automatic", "manual",
    "ai", "model", "dcmqi", "pydicom", "highdicom", "itk", "simpleitk",
    "slicer", "3d slicer", "plastimatch", "dcmtk",
}

# The code sequences the per-segment table carries a designator for.
CODE_SEQUENCES = [
    "AnatomicRegion",
    "AnatomicRegionModifier",
    "SegmentedPropertyCategory",
    "SegmentedPropertyType",
    "SegmentedPropertyTypeModifier",
]

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
# Issue 10 - retired coding scheme designator. Computed.
# ---------------------------------------------------------------------------


def retired_scheme(rows):
    """Segments carrying a code under a designator DICOM has retired.

    Reported per segment, listing every sequence affected, because a producer
    that uses SRT in one sequence normally uses it in all of them and the fix
    is one change to the producer, not one per sequence.
    """
    findings = []
    for row in rows:
        affected = []
        for prefix in CODE_SEQUENCES:
            scheme = row.get(f"{prefix}CodingSchemeDesignator", "")
            if scheme in RETIRED_SCHEMES and row.get(f"{prefix}CodeValue", ""):
                affected.append(
                    f"{prefix}CodeSequence={scheme}:"
                    f"{row[f'{prefix}CodeValue']}"
                )
        if not affected:
            continue
        schemes = sorted({a.split("=")[1].split(":")[0] for a in affected})
        finding = key(row)
        finding.update({
            "SOPInstanceUID": row["SOPInstanceUID"],
            "retiredSchemes": ", ".join(schemes),
            "replacementScheme": ", ".join(
                sorted({RETIRED_SCHEMES[s] for s in schemes})),
            "affectedSequences": "; ".join(affected),
            "SeriesDescription": row["SeriesDescription"],
        })
        findings.append(finding)
    return findings


# ---------------------------------------------------------------------------
# Issue 13 - RecommendedDisplayCIELabValue. Computed, in Lab: no colour is
# converted to RGB to decide anything, only to draw the preview.
# ---------------------------------------------------------------------------


def structure_key(row):
    """What a viewer's user would call "a different thing" on screen.

    The coded identity first, because that is what the object asserts; the
    label only where there is no code at all. Two segments of the SAME
    structure sharing a colour is not a finding - it is the point of a colour
    convention - so this key is what separates the finding from the norm.
    """
    coded = (
        row.get("SegmentedPropertyTypeCodingSchemeDesignator", ""),
        row.get("SegmentedPropertyTypeCodeValue", ""),
        row.get("AnatomicRegionCodingSchemeDesignator", ""),
        row.get("AnatomicRegionCodeValue", ""),
    )
    if any(coded):
        return coded
    return ("", row.get("SegmentLabel", ""), "", "")


def structure_label(row):
    parts = [
        row.get("SegmentedPropertyTypeCodeMeaning", ""),
        row.get("AnatomicRegionCodeMeaning", ""),
    ]
    named = " / ".join(part for part in parts if part)
    return named or row.get("SegmentLabel", "") or "(uncoded)"


def recommended_color(rows, threshold=10.0):
    """Which colours the batch assigns, and where two structures share one.

    Returns (findings, palette). `findings` is one row per segment with a
    problem, `palette` one row per distinct colour - the palette is the
    deliverable that answers "what colours are assigned at all", and it is
    worth publishing even when nothing is wrong.

    Four things are decided here:

      ABSENT           (0062,000D) is Type 3, so this is conformant. Reported
                       because a consumer then has to invent a colour, and two
                       consumers will invent different ones.
      MALFORMED        present but not three unsigned shorts. A VM violation.
      NOT_PERMITTED    PS3.3 C.8.20.2: the attribute "shall not be present if
                       Segmentation Type is LABELMAP and Photometric
                       Interpretation is PALETTE COLOR" - the palette already
                       carries the colour, and two sources of truth is one too
                       many.
      DUPLICATE /      two segments of DIFFERENT structures, in ONE object,
      CONFUSABLE       given the same colour or one within `threshold` dE*ab
                       of it. This is the one a reader sees: the viewer draws
                       both overlays identically and no amount of correct
                       metadata tells them apart on screen.
      INCONSISTENT     one structure drawn in several colours across the
                       batch. Not wrong anywhere in particular; it makes two
                       series unreadable side by side.
    """
    findings = []
    # Keyed on the row's index, not the row: the row is a dict, so it is
    # neither hashable nor safe to identify by value - two segments can be
    # identical in every column this check reads.
    parsed = {}
    malformed = {}
    for index, row in enumerate(rows):
        try:
            parsed[index] = cielab.parse(row.get("RecommendedDisplayCIELabValue", ""))
        except ValueError as exc:
            parsed[index] = None
            malformed[index] = str(exc)

    def finding(index, issue, **extra):
        row = rows[index]
        entry = key(row)
        entry.update({
            "SOPInstanceUID": row["SOPInstanceUID"],
            "colorIssue": issue,
            "RecommendedDisplayCIELabValue": row.get(
                "RecommendedDisplayCIELabValue", ""),
            "hex": "",
            "structure": structure_label(row),
            "SegmentationType": row.get("SegmentationType", ""),
            "PhotometricInterpretation": row.get("PhotometricInterpretation", ""),
            "collidesWithSegments": "",
            "collidesWithStructures": "",
            "deltaE": "",
            "SeriesDescription": row.get("SeriesDescription", ""),
        })
        triplet = parsed.get(index)
        if triplet:
            entry["hex"] = cielab.to_hex(triplet)
        entry.update(extra)
        return entry

    for index, message in malformed.items():
        findings.append(finding(index, "MALFORMED", collidesWithStructures=message))

    for index, row in enumerate(rows):
        if parsed[index] is None:
            if index not in malformed:
                findings.append(finding(index, "ABSENT"))
        elif (
            row.get("SegmentationType", "") == "LABELMAP"
            and row.get("PhotometricInterpretation", "") == "PALETTE COLOR"
        ):
            findings.append(finding(index, "NOT_PERMITTED"))

    # Within one object: who cannot be told apart from whom.
    objects = defaultdict(list)
    for index, row in enumerate(rows):
        if parsed[index] is not None:
            objects[row["SOPInstanceUID"]].append(index)

    shared_colours = set()
    for group in objects.values():
        collisions = defaultdict(list)
        for position, first in enumerate(group):
            for second in group[position + 1:]:
                if structure_key(rows[first]) == structure_key(rows[second]):
                    continue
                distance = cielab.delta_e(parsed[first], parsed[second])
                if parsed[first] == parsed[second]:
                    verdict = "DUPLICATE_IN_OBJECT"
                elif distance < threshold:
                    verdict = "CONFUSABLE_IN_OBJECT"
                else:
                    continue
                shared_colours.add(parsed[first])
                shared_colours.add(parsed[second])
                collisions[first].append((verdict, second, distance))
                collisions[second].append((verdict, first, distance))
        for index in group:
            entries = collisions.get(index)
            if not entries:
                continue
            # One row per segment, not one per pair: the segment is what gets
            # recoloured, and a three-way collision is one job, not three.
            verdict = (
                "DUPLICATE_IN_OBJECT"
                if any(v == "DUPLICATE_IN_OBJECT" for v, _, _ in entries)
                else "CONFUSABLE_IN_OBJECT"
            )
            findings.append(finding(
                index, verdict,
                collidesWithSegments=";".join(
                    str(rows[other]["SegmentNumber"]) for _, other, _ in entries),
                collidesWithStructures="; ".join(
                    sorted({structure_label(rows[other])
                            for _, other, _ in entries})),
                deltaE=round(min(distance for _, _, distance in entries), 2),
            ))

    # Across the batch: one structure, several colours.
    per_structure = defaultdict(set)
    for index, row in enumerate(rows):
        if parsed[index] is not None:
            per_structure[structure_key(row)].add(parsed[index])
    inconsistent = {k for k, colours in per_structure.items() if len(colours) > 1}
    for index, row in enumerate(rows):
        if parsed[index] is not None and structure_key(row) in inconsistent:
            findings.append(finding(
                index, "INCONSISTENT_ACROSS_BATCH",
                collidesWithStructures="; ".join(sorted(
                    cielab.to_hex(t) for t in per_structure[structure_key(row)])),
            ))

    # The palette: one row per distinct colour, most used first.
    per_colour = defaultdict(lambda: {"rows": [], "structures": {}})
    for index, row in enumerate(rows):
        triplet = parsed[index]
        if triplet is None:
            continue
        entry = per_colour[triplet]
        entry["rows"].append(row)
        entry["structures"][structure_key(row)] = structure_label(row)

    palette = []
    for triplet, entry in per_colour.items():
        lightness, a_star, b_star = cielab.to_lab(triplet)
        group = entry["rows"]
        palette.append({
            "RecommendedDisplayCIELabValue": "/".join(str(v) for v in triplet),
            "hex": cielab.to_hex(triplet),
            "Lstar": round(lightness, 1),
            "astar": round(a_star, 1),
            "bstar": round(b_star, 1),
            "segments": len(group),
            "series": len({r["SeriesInstanceUID"] for r in group}),
            "objects": len({r["SOPInstanceUID"] for r in group}),
            "distinctStructures": len(entry["structures"]),
            "structures": "; ".join(sorted(entry["structures"].values())[:6]),
            "sharedWithinObject": str(triplet in shared_colours),
            "exampleSeriesInstanceUID": group[0]["SeriesInstanceUID"],
            "viewer_url": group[0]["viewer_url"],
        })
    palette.sort(key=lambda r: (-r["segments"], r["hex"]))
    return findings, palette


def write_palette_markdown(outdir, palette, swatch_dir="swatches"):
    """The palette as a paste-ready Markdown table, with a visible swatch.

    A colour finding that a reader cannot SEE is half a finding, and no
    Markdown renderer agrees on how to show one. GitHub strips `style`, so an
    inline-styled cell renders as bare text; it refuses `data:` image sources,
    so an embedded PNG renders as a broken image; its `#rrggbb` chip syntax
    works in issues and pull requests but not in a committed .md file.

    What survives all of them is a small SVG file referenced relatively, so
    that is what this writes - one file per distinct colour, beside the report.
    The hex is printed in the same row regardless, because a swatch that fails
    to load must still leave the reader with the value.

    Returns (markdown path, swatch directory). Keep them together when the
    report moves: the <img> paths are relative to the Markdown file.
    """
    outdir = Path(outdir)
    swatches = outdir / swatch_dir
    swatches.mkdir(parents=True, exist_ok=True)

    lines = [
        "| Colour | Hex | L\\*a\\*b\\* | Segments | Series | Structures |",
        "|---|---|---|---|---|---|",
    ]
    for entry in palette:
        name = entry["hex"].lstrip("#")
        triplet = cielab.parse(entry["RecommendedDisplayCIELabValue"])
        (swatches / f"{name}.svg").write_text(cielab.swatch_svg(triplet))
        shared = " **shared**" if entry["sharedWithinObject"] == "True" else ""
        lines.append(
            f'| <img src="{swatch_dir}/{name}.svg" width="16" height="16" '
            f'alt="{entry["hex"]}"> | `{entry["hex"]}`{shared} | '
            f'{entry["Lstar"]} / {entry["astar"]} / {entry["bstar"]} | '
            f'{entry["segments"]} | {entry["series"]} | {entry["structures"]} |'
        )
    lines.append("")
    lines.append(
        "Swatches are sRGB renderings of the stored CIELab, assuming the D50 "
        "white point of the ICC PCS (PS3.3 C.10.7.1.1). Writers differ at the "
        "margins, so read a swatch as the producer's intent, not as evidence - "
        "every finding above is decided in CIELab itself."
    )
    path = outdir / "issue13_color_palette.md"
    path.write_text("\n".join(lines) + "\n")
    return path, swatches


# ---------------------------------------------------------------------------
# Issue 14 - an automatic segmentation that does not say what made it.
# Computed.
# ---------------------------------------------------------------------------


def _uninformative(name):
    stripped = "".join(
        character for character in name.lower() if character.isalnum() or character == " "
    ).strip()
    return stripped in UNINFORMATIVE_ALGORITHM_NAMES


def algorithm_identification(rows):
    """Segments whose SegmentAlgorithmType is not MANUAL but which do not
    identify the algorithm that produced them.

    One row per affected segment, listing every reason, because they are one
    fix at the producer rather than one per segment.

      NAME_MISSING      SegmentAlgorithmName (0062,0009) absent. Type 1C -
                        "Required if Segment Algorithm Type (0062,0008) is not
                        MANUAL" - so this is a conformance violation, and the
                        only one of the four that is.
      NAME_UNINFORMATIVE  a name that identifies nothing: "unknown", "AI", or
                        the name of the toolkit that WROTE the object rather
                        than the model that segmented it.
      NO_IDENTIFICATION  no Segmentation Algorithm Identification Sequence
                        (0062,0007). Type 3, so conformant - and the only
                        standard home for the version, the source and a code
                        for the specific algorithm. Without it the delivery
                        cannot be attributed to a model at all.
      NO_VERSION        the sequence is present but Algorithm Version
                        (0066,0031), Type 1 within it, is empty.
    """
    findings = []
    for row in rows:
        algorithm_type = (row.get("SegmentAlgorithmType", "") or "").strip().upper()
        if algorithm_type not in NON_MANUAL_ALGORITHM:
            continue
        name = (row.get("SegmentAlgorithmName", "") or "").strip()
        issues = []
        if not name:
            issues.append("NAME_MISSING")
        elif _uninformative(name):
            issues.append("NAME_UNINFORMATIVE")
        if row.get("hasAlgorithmIdentification", "") != "True":
            issues.append("NO_IDENTIFICATION")
        elif not (row.get("AlgorithmVersion", "") or "").strip():
            issues.append("NO_VERSION")
        if not issues:
            continue
        entry = key(row)
        entry.update({
            "SOPInstanceUID": row["SOPInstanceUID"],
            "SegmentAlgorithmType": algorithm_type,
            "algorithmIssues": "; ".join(issues),
            "SegmentAlgorithmName": name,
            "AlgorithmName": row.get("AlgorithmName", ""),
            "AlgorithmVersion": row.get("AlgorithmVersion", ""),
            "AlgorithmSource": row.get("AlgorithmSource", ""),
            "AlgorithmNameCodeMeaning": row.get("AlgorithmNameCodeMeaning", ""),
            # The object-level identification, which is NOT a substitute: it
            # names the software that wrote the file, which on a converted
            # segmentation is the converter.
            "ManufacturerModelName": row.get("ManufacturerModelName", ""),
            "SoftwareVersion": row.get("SoftwareVersion", ""),
            "SeriesDescription": row.get("SeriesDescription", ""),
        })
        findings.append(entry)
    return findings


# ---------------------------------------------------------------------------
# Issue 9 - IOD conformance, from dciodvfy. Read, not computed here: the
# validator needs the files, which the BigQuery and DICOMweb paths do not have.
# ---------------------------------------------------------------------------


def iod_tags(path):
    """issue9_iod_validation.csv -> SeriesInstanceUID -> {tag: message count}.

    A series inherits IOD_ERROR if any of its objects drew a dciodvfy Error,
    IOD_WARNING if any drew a Warning. Retired coding schemes are deliberately
    NOT taken from here: issue 10 computes them from the per-segment table, so
    the tag means the same thing on all three access paths.
    """
    tags = defaultdict(Counter)
    for row in load(path):
        if row["messageClass"] == "DEPRECATED_CODING_SCHEME":
            continue
        tag = "IOD_ERROR" if row["dciodvfySeverity"] == "Error" else "IOD_WARNING"
        tags[row["SeriesInstanceUID"]][tag] += 1
    return tags


def encoding_tags(path):
    """encoding_per_object.csv -> SeriesInstanceUID -> {tag: count of objects}.

    Issues 11 and 12, read the same way issue 9 is: seg_encoding.py needs the
    objects, so on the BigQuery and DICOMweb paths these tags are absent and
    the triage list is SILENT about them rather than clearing them.

    LOSSY_COMPRESSED is taken from the transfer syntax alone, never from
    LossyImageCompression (0028,2110). PS3.3 C.8.20.2.2 requires that Attribute
    to be "01" when any of the SOURCE images was lossy compressed, so on a
    segmentation of a lossy-compressed CT it is "01" while the segmentation
    itself is intact. Reading it as "this object was lossy compressed" would
    manufacture a High finding out of a correctly encoded object.
    """
    tags = defaultdict(Counter)
    for row in load(path):
        series = row["SeriesInstanceUID"]
        if row.get("isLossy", "") == "True":
            tags[series]["LOSSY_COMPRESSED"] += 1
        elif row.get("isCompressed", "") == "False":
            tags[series]["UNCOMPRESSED"] += 1
        if int(row.get("emptyFrames") or 0):
            tags[series]["EMPTY_FRAMES_RETAINED"] += 1
        if int(row.get("emptySegmentCount") or 0):
            tags[series]["EMPTY_SEGMENT"] += 1
    return tags


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
            # Normally absent, and a serious problem if it appears: one
            # identifier spanning two people.
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


def triage(rows, ambiguous, cosmetic, entire, verdicts, tracking,
           iod=None, colors=None, algorithms=None, encoding=None):
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

    # Issues 13 and 14 are decided per segment by their own checks, so the
    # roll-up reads their verdicts rather than recomputing them - there is one
    # definition of each tag, in one place.
    colour_tags = {
        "DUPLICATE_IN_OBJECT": "COLOR_DUPLICATE",
        "CONFUSABLE_IN_OBJECT": "COLOR_CONFUSABLE",
        "INCONSISTENT_ACROSS_BATCH": "COLOR_INCONSISTENT",
        "NOT_PERMITTED": "COLOR_NOT_PERMITTED",
        "MALFORMED": "COLOR_MALFORMED",
        "ABSENT": "COLOR_ABSENT",
    }
    per_segment = defaultdict(Counter)
    for entry in colors or []:
        tag = colour_tags.get(entry["colorIssue"])
        if tag:
            per_segment[(entry["SOPInstanceUID"], entry["SegmentNumber"])][tag] += 1
    for entry in algorithms or []:
        issues = entry["algorithmIssues"].split("; ")
        tag = (
            "ALGORITHM_NAME_MISSING"
            if "NAME_MISSING" in issues
            else "ALGORITHM_UNIDENTIFIED"
        )
        per_segment[(entry["SOPInstanceUID"], entry["SegmentNumber"])][tag] += 1

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
            if any(row.get(f"{prefix}CodingSchemeDesignator", "") in RETIRED_SCHEMES
                   and row.get(f"{prefix}CodeValue", "")
                   for prefix in CODE_SEQUENCES):
                counts["RETIRED_CODING_SCHEME"] += 1
            if row["TrackingUID"] in ambiguous_tracking:
                counts["TRACKINGUID_AMBIGUOUS"] += 1
            if row["TrackingUID"] in cross_patient:
                counts["TRACKINGUID_CROSS_PATIENT"] += 1
            type_code = row["SegmentedPropertyTypeCodeValue"]
            if type_code and type_code == row["SegmentedPropertyCategoryCodeValue"]:
                counts["TYPE_REPEATS_CATEGORY"] += 1
            if not row["SegmentsOverlap"]:
                counts["NO_SEGMENTS_OVERLAP"] += 1
            for tag, count in per_segment.get(
                (row["SOPInstanceUID"], row["SegmentNumber"]), {}
            ).items():
                counts[tag] += count

        # Issues 9, 11 and 12 are per OBJECT, not per segment, so they join at
        # the series.
        for tag, count in (iod or {}).get(series, {}).items():
            counts[tag] += count
        for tag, count in (encoding or {}).get(series, {}).items():
            counts[tag] += count

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
        "--iod",
        help="issue9_iod_validation.csv from dciodvfy_check.py; folds the IOD "
        "validator's verdict into the triage list. Local files only - the "
        "validator needs the objects, not a metadata table.",
    )
    parser.add_argument(
        "--encoding",
        help="encoding_per_object.csv from seg_encoding.py; folds issues 11 "
        "and 12 (empty frames, compression) into the triage list. Local files "
        "only - both need the objects, not a metadata table.",
    )
    parser.add_argument(
        "--color-delta-e",
        type=float,
        default=10.0,
        metavar="DE",
        help="issue 13: how close two colours in ONE object must be before a "
        "reader cannot tell the segments apart, in dE*ab (default 10). 2.3 is "
        "the just-noticeable difference for two large flat patches; segment "
        "overlays are small, scattered and drawn over grey, so the useful "
        "threshold is well above that.",
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

    ret = retired_scheme(rows)
    write(outdir, "issue10_retired_coding_scheme.csv", ret,
          ["PatientID", "StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID",
           "SegmentNumber", "SegmentLabel", "retiredSchemes", "replacementScheme",
           "affectedSequences", "SeriesDescription", "viewer_url"])
    if ret:
        schemes = Counter(f["retiredSchemes"] for f in ret)
        print(f"Issue 10 retired scheme   {len(ret):>6} segments  "
              + ", ".join(f"{k}={v}" for k, v in schemes.most_common()))
        print("           the CodeValues change too - a designator swap alone "
              "invents codes")
    else:
        print("Issue 10 retired scheme        0 segments  ok: no SRT/SNM3 codes")

    colors, palette = recommended_color(rows, args.color_delta_e)
    write(outdir, "issue13_recommended_color.csv", colors,
          ["PatientID", "StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID",
           "SegmentNumber", "SegmentLabel", "colorIssue",
           "RecommendedDisplayCIELabValue", "hex", "structure",
           "collidesWithSegments", "collidesWithStructures", "deltaE",
           "SegmentationType", "PhotometricInterpretation", "SeriesDescription",
           "viewer_url"])
    write(outdir, "issue13_color_palette.csv", palette,
          ["RecommendedDisplayCIELabValue", "hex", "Lstar", "astar", "bstar",
           "segments", "series", "objects", "distinctStructures", "structures",
           "sharedWithinObject", "exampleSeriesInstanceUID", "viewer_url"])
    colour_counts = Counter(f["colorIssue"] for f in colors)
    coloured = sum(1 for r in rows if r.get("RecommendedDisplayCIELabValue"))
    print(f"Issue 13 display colour   {len(palette):>6} distinct colours over "
          f"{coloured} of {len(rows)} segments")
    for name, label in (
        ("DUPLICATE_IN_OBJECT", "two structures, one colour, one object"),
        ("CONFUSABLE_IN_OBJECT", f"within {args.color_delta_e:g} dE*ab in one object"),
        ("INCONSISTENT_ACROSS_BATCH", "one structure, several colours"),
        ("NOT_PERMITTED", "LABELMAP + PALETTE COLOR: shall not be present"),
        ("MALFORMED", "not three unsigned shorts"),
        ("ABSENT", "no colour at all (Type 3, so conformant)"),
    ):
        if colour_counts[name]:
            print(f"           {name:<26} {colour_counts[name]:>6}  {label}")
    if palette:
        markdown, swatches = write_palette_markdown(outdir, palette)
        print(f"           palette -> {markdown} ({len(palette)} swatches in "
              f"{swatches}/) - paste the table into the report")

    algorithms = algorithm_identification(rows)
    write(outdir, "issue14_algorithm_identification.csv", algorithms,
          ["PatientID", "StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID",
           "SegmentNumber", "SegmentLabel", "SegmentAlgorithmType",
           "algorithmIssues", "SegmentAlgorithmName", "AlgorithmName",
           "AlgorithmVersion", "AlgorithmSource", "AlgorithmNameCodeMeaning",
           "ManufacturerModelName", "SoftwareVersion", "SeriesDescription",
           "viewer_url"])
    automatic = [
        r for r in rows
        if (r.get("SegmentAlgorithmType", "") or "").strip().upper()
        in NON_MANUAL_ALGORITHM
    ]
    algorithm_counts = Counter(
        issue for f in algorithms for issue in f["algorithmIssues"].split("; ")
    )
    if automatic:
        print(f"Issue 14 algorithm id     {len(algorithms):>6} of {len(automatic)} "
              "AUTOMATIC/SEMIAUTOMATIC segments do not identify what made them")
        for name, label in (
            ("NAME_MISSING", "SegmentAlgorithmName absent - Type 1C violation"),
            ("NAME_UNINFORMATIVE", "a name that identifies nothing"),
            ("NO_IDENTIFICATION", "no (0062,0007), so no version and no model code"),
            ("NO_VERSION", "(0062,0007) present, AlgorithmVersion empty"),
        ):
            if algorithm_counts[name]:
                print(f"           {name:<26} {algorithm_counts[name]:>6}  {label}")
    else:
        print("Issue 14 algorithm id          - no AUTOMATIC or SEMIAUTOMATIC "
              "segments; nothing is required")

    # ---- read from the IOD validator, if it was run
    iod = None
    if args.iod:
        iod = iod_tags(args.iod)
        messages = load(args.iod)
        errors = sum(1 for m in messages if m["dciodvfySeverity"] == "Error")
        print(f"Issue 9  IOD validation   {errors:>6} errors, "
              f"{len(messages) - errors} warnings over {len(iod)} series "
              f"({args.iod})")
    else:
        print("Issue 9  skipped - run dciodvfy_check.py and pass --iod "
              "(local files only)")

    # ---- read from the encoding scan, if it was run
    encoding = None
    if args.encoding:
        encoding = encoding_tags(args.encoding)
        objects = load(args.encoding)
        empty = [o for o in objects if int(o.get("emptyFrames") or 0)]
        lossy = [o for o in objects if o.get("isLossy") == "True"]
        uncompressed = [o for o in objects if o.get("isCompressed") == "False"]
        print(f"Issue 11 empty frames     {len(empty):>6} of {len(objects)} objects "
              "keep at least one all-zero frame")
        print(f"Issue 12 compression      {len(uncompressed):>6} of {len(objects)} "
              f"objects are uncompressed, {len(lossy)} LOSSY compressed")
    else:
        print("Issues 11, 12 skipped - run seg_encoding.py and pass --encoding "
              "(local files only)")

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
    series = triage(rows, ambiguous, cosmetic, entire, verdicts, track, iod,
                    colors, algorithms, encoding)
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
