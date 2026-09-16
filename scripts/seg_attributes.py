#!/usr/bin/env python3
"""Flatten DICOM SEG objects into one row per segment.

Produces the per-segment table every check in this skill runs on — the same
columns the BigQuery view in scripts/sql/01_seg_attributes.sql produces, so a
review is written once and runs against whichever access path is available.

Two sources:

  --files <dir>        walk a directory of Part 10 files (needs pydicom)
  --dicomweb <url>     a DICOMweb endpoint (needs dicomweb-client)

Both converge on one extraction function over a pydicom Dataset, so the two
paths cannot drift apart.

Usage:
  python seg_attributes.py --files /path/to/delivery -o seg_attributes.csv
  python seg_attributes.py --dicomweb projects/p/locations/l/datasets/d/dicomStores/s \\
      --gcp -o seg_attributes.csv

Carries attributes only, no judgements — see references/issue-catalogue.md for
what is then checked, and SKILL.md for why the two are kept apart.
"""

import argparse
import csv
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Segmentation Storage, and Labelmap Segmentation Storage. Filtering on
# SOPClassUID rather than Modality == "SEG": Modality is free enough that
# producers get it wrong, and the SOP class is what determines the encoding.
SEG_SOP_CLASSES = {
    "1.2.840.10008.5.1.4.1.1.66.4": "Segmentation Storage",
    "1.2.840.10008.5.1.4.1.1.66.7": "Labelmap Segmentation Storage",
}

# The output contract. Keep in step with scripts/sql/01_seg_attributes.sql.
COLUMNS = [
    "Collection",
    "PatientID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "SOPInstanceUID",
    "SegmentNumber",
    "SeriesDescription",
    "SeriesNumber",
    "SeriesDate",
    "FrameOfReferenceUID",
    "SegmentLabel",
    "SegmentDescription",
    "TrackingID",
    "TrackingUID",
    "AnatomicRegionCodeValue",
    "AnatomicRegionCodingSchemeDesignator",
    "AnatomicRegionCodeMeaning",
    "AnatomicRegionModifierCodeValue",
    "AnatomicRegionModifierCodingSchemeDesignator",
    "AnatomicRegionModifierCodeMeaning",
    "SegmentedPropertyCategoryCodeValue",
    "SegmentedPropertyCategoryCodingSchemeDesignator",
    "SegmentedPropertyCategoryCodeMeaning",
    "SegmentedPropertyTypeCodeValue",
    "SegmentedPropertyTypeCodingSchemeDesignator",
    "SegmentedPropertyTypeCodeMeaning",
    "SegmentedPropertyTypeModifierCodeValue",
    "SegmentedPropertyTypeModifierCodingSchemeDesignator",
    "SegmentedPropertyTypeModifierCodeMeaning",
    "SegmentAlgorithmType",
    "SegmentAlgorithmName",
    "hasAlgorithmIdentification",
    "AlgorithmName",
    "AlgorithmVersion",
    "AlgorithmSource",
    "AlgorithmFamilyCodeValue",
    "AlgorithmFamilyCodingSchemeDesignator",
    "AlgorithmFamilyCodeMeaning",
    "AlgorithmNameCodeValue",
    "AlgorithmNameCodingSchemeDesignator",
    "AlgorithmNameCodeMeaning",
    "RecommendedDisplayCIELabValue",
    "SegmentationType",
    "SegmentsOverlap",
    "PhotometricInterpretation",
    "TransferSyntaxUID",
    "Manufacturer",
    "ManufacturerModelName",
    "SoftwareVersions",
    "segmentsInInstance",
    "numberOfFrames",
    "referencedSeriesInstanceUID",
    "referencedModality",
    "referencedBodyPartExamined",
    "referencedFrameOfReferenceUID",
    "referencedSeriesFound",
    "referencedInstanceCount",
    "referencedSeriesCount",
    "isBackgroundSegment",
    "multiValuedCodeSequence",
    "viewer_url",
]

# Columns whose emptiness across the whole batch is itself a finding: it means
# no instance in the delivery populates the attribute. The BigQuery path learns
# this from the export schema; here it has to be measured.
COVERAGE_COLUMNS = [
    "TrackingID",
    "TrackingUID",
    "AnatomicRegionCodeValue",
    "AnatomicRegionModifierCodeValue",
    "SegmentedPropertyTypeModifierCodeValue",
    "SegmentAlgorithmName",
    "AlgorithmName",
    "AlgorithmVersion",
    "RecommendedDisplayCIELabValue",
    "SegmentsOverlap",
    "SegmentDescription",
]


def _require_pydicom():
    try:
        import pydicom
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            "reading DICOM needs pydicom:\n  pip install 'pydicom>=3.0'\n"
            f"({exc})"
        ) from exc
    return pydicom


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _first(seq):
    """First item of a DICOM sequence, or None. Mirrors SQL SAFE_OFFSET(0)."""
    if seq is None:
        return None
    try:
        return seq[0]
    except (IndexError, TypeError):
        return None


def _code(item, keyword):
    """(CodeValue, CodingSchemeDesignator, CodeMeaning) of a nested code sequence.

    `item` is the dataset holding the sequence; `keyword` names it. Returns
    empty strings rather than None so an absent sequence and an absent code
    value are written the same way — both are Type 1 violations where the
    attribute is required, and issue 4 treats them alike.
    """
    entry = _first(getattr(item, keyword, None)) if item is not None else None
    if entry is None:
        return "", "", ""
    return (
        str(getattr(entry, "CodeValue", "") or ""),
        str(getattr(entry, "CodingSchemeDesignator", "") or ""),
        str(getattr(entry, "CodeMeaning", "") or ""),
    )


def _multi_valued(segment):
    """TRUE when any code sequence of this segment holds more than one item.

    The guard on the SAFE_OFFSET(0) assumption every check makes. If this is
    ever TRUE the single code reported per sequence is only the first, and the
    checks understate — handle those segments by hand.
    """
    region = _first(getattr(segment, "AnatomicRegionSequence", None))
    seg_type = _first(getattr(segment, "SegmentedPropertyTypeCodeSequence", None))
    sequences = [
        getattr(segment, "AnatomicRegionSequence", None),
        getattr(segment, "SegmentedPropertyCategoryCodeSequence", None),
        getattr(segment, "SegmentedPropertyTypeCodeSequence", None),
        getattr(region, "AnatomicRegionModifierSequence", None) if region else None,
        getattr(seg_type, "SegmentedPropertyTypeModifierCodeSequence", None)
        if seg_type
        else None,
    ]
    return any(s is not None and len(s) > 1 for s in sequences)


def _scalar(ds, keyword, default=""):
    """String value of an attribute, taking the first of a multi-valued one."""
    value = getattr(ds, keyword, None)
    if value is None:
        return default
    if isinstance(value, (list, tuple)) or type(value).__name__ == "MultiValue":
        value = value[0] if len(value) else None
    return "" if value is None else str(value)


def _algorithm_identification(segment):
    """Segmentation Algorithm Identification Sequence (0062,0007), flattened.

    Type 3 on the segment, and the ONLY standard place a model's version lives:
    Segment Algorithm Name (0062,0009) is a bare string with nowhere to put the
    version, the source, or a code for the specific algorithm. Inside this
    sequence, Algorithm Name (0066,0036) and Algorithm Version (0066,0031) are
    both Type 1 — so a sequence that is present but missing either is itself a
    finding, which is why the flag is carried separately from the values.
    PS3.3 Table 10-19. See issue 14.
    """
    item = _first(getattr(segment, "SegmentationAlgorithmIdentificationSequence", None))
    if item is None:
        return {"hasAlgorithmIdentification": "False"}
    family = _code(item, "AlgorithmFamilyCodeSequence")
    name_code = _code(item, "AlgorithmNameCodeSequence")
    return {
        "hasAlgorithmIdentification": "True",
        "AlgorithmName": _scalar(item, "AlgorithmName"),
        "AlgorithmVersion": _scalar(item, "AlgorithmVersion"),
        "AlgorithmSource": _scalar(item, "AlgorithmSource"),
        "AlgorithmFamilyCodeValue": family[0],
        "AlgorithmFamilyCodingSchemeDesignator": family[1],
        "AlgorithmFamilyCodeMeaning": family[2],
        "AlgorithmNameCodeValue": name_code[0],
        "AlgorithmNameCodingSchemeDesignator": name_code[1],
        "AlgorithmNameCodeMeaning": name_code[2],
    }


def _cielab(segment):
    """RecommendedDisplayCIELabValue (0062,000D) as "l/a/b", or "".

    Three unsigned shorts, kept exactly as stored. Nothing is converted here:
    the scaling is defined in PS3.3 C.10.7.1.1 but rendering it as RGB needs a
    white point the Standard only implies, so the conversion belongs with the
    check that uses it (scripts/cielab.py) and not in the extract, where it
    would look like data. A value with anything other than three components is
    passed through as-is for issue 13 to report.
    """
    value = getattr(segment, "RecommendedDisplayCIELabValue", None)
    if value is None:
        return ""
    if isinstance(value, (list, tuple)) or type(value).__name__ == "MultiValue":
        return "/".join(str(v) for v in value)
    return str(value)


def extract_instance(ds, viewer_url_pattern=None, collection="", referenced=None):
    """One row per segment of one SEG instance.

    An instance with no SegmentSequence still yields a single row, with every
    segment column empty, so that the conformance check reports it rather than
    the object vanishing from the review.
    """
    # `referenced` is None when the referenced series were not looked up and a
    # dict - possibly empty - when they were. The difference is what lets
    # referencedSeriesFound say "False" only for a lookup that found nothing,
    # which is issue 17's dangling reference, and stay empty otherwise.
    resolved = referenced is not None
    referenced = referenced or {}
    ref_series = _first(getattr(ds, "ReferencedSeriesSequence", None))
    ref_series_uid = str(getattr(ref_series, "SeriesInstanceUID", "") or "")
    ref_info = referenced.get(ref_series_uid, {})

    instance = {
        "Collection": collection,
        "PatientID": _scalar(ds, "PatientID"),
        "StudyInstanceUID": _scalar(ds, "StudyInstanceUID"),
        "SeriesInstanceUID": _scalar(ds, "SeriesInstanceUID"),
        "SOPInstanceUID": _scalar(ds, "SOPInstanceUID"),
        "SeriesDescription": _scalar(ds, "SeriesDescription"),
        "SeriesNumber": _scalar(ds, "SeriesNumber"),
        "SeriesDate": _scalar(ds, "SeriesDate"),
        "FrameOfReferenceUID": _scalar(ds, "FrameOfReferenceUID"),
        "SegmentationType": _scalar(ds, "SegmentationType"),
        "SegmentsOverlap": _scalar(ds, "SegmentsOverlap"),
        # Needed by issue 13: (0062,000D) shall NOT be present when a LABELMAP
        # object is PALETTE COLOR, since the palette already carries the colour.
        "PhotometricInterpretation": _scalar(ds, "PhotometricInterpretation"),
        # Issue 12. File meta, so it exists on the local-files path only - a
        # DICOMweb metadata response and a BigQuery export both drop it.
        "TransferSyntaxUID": str(
            getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", "") or ""
        ),
        "Manufacturer": _scalar(ds, "Manufacturer"),
        "ManufacturerModelName": _scalar(ds, "ManufacturerModelName"),
        "SoftwareVersions": _scalar(ds, "SoftwareVersions"),
        "numberOfFrames": _scalar(ds, "NumberOfFrames"),
        "referencedSeriesInstanceUID": ref_series_uid,
        # Only some producers copy these into ReferencedSeriesSequence, so fall
        # back to whatever --resolve-referenced found.
        "referencedModality": str(getattr(ref_series, "Modality", "") or "")
        or ref_info.get("Modality", ""),
        "referencedBodyPartExamined": str(
            getattr(ref_series, "BodyPartExamined", "") or ""
        )
        or ref_info.get("BodyPartExamined", ""),
        # Issue 17. PS3.3 A.51.1: a segmentation "shall have the same Frame of
        # Reference" as referenced images that define one. Empty means the
        # referenced series was not looked up - never "matches".
        "referencedFrameOfReferenceUID": ref_info.get("FrameOfReferenceUID", ""),
        "referencedSeriesFound": (
            str(ref_series_uid in referenced) if resolved and ref_series_uid else ""
        ),
        # Same two counts the BigQuery view carries: how many source instances
        # the first referenced series lists (compare with numberOfFrames), and
        # how many series are referenced (above 1, the columns above describe
        # only the first).
        "referencedInstanceCount": (
            str(len(getattr(ref_series, "ReferencedInstanceSequence", None) or []))
            if ref_series is not None else ""
        ),
        "referencedSeriesCount": str(
            len(getattr(ds, "ReferencedSeriesSequence", None) or [])),
    }

    if viewer_url_pattern:
        instance["viewer_url"] = viewer_url_pattern.format(
            StudyInstanceUID=instance["StudyInstanceUID"],
            SeriesInstanceUID=instance["SeriesInstanceUID"],
            SOPInstanceUID=instance["SOPInstanceUID"],
        )
    else:
        instance["viewer_url"] = ""

    segments = getattr(ds, "SegmentSequence", None) or []
    instance["segmentsInInstance"] = str(len(segments))

    if not segments:
        row = {c: "" for c in COLUMNS}
        row.update(instance)
        row["isBackgroundSegment"] = "False"
        row["multiValuedCodeSequence"] = "False"
        row["hasAlgorithmIdentification"] = "False"
        return [row]

    rows = []
    for segment in segments:
        region = _first(getattr(segment, "AnatomicRegionSequence", None))
        seg_type = _first(getattr(segment, "SegmentedPropertyTypeCodeSequence", None))

        region_code = _code(segment, "AnatomicRegionSequence")
        region_mod = _code(region, "AnatomicRegionModifierSequence")
        category = _code(segment, "SegmentedPropertyCategoryCodeSequence")
        type_code = _code(segment, "SegmentedPropertyTypeCodeSequence")
        type_mod = _code(seg_type, "SegmentedPropertyTypeModifierCodeSequence")

        number = getattr(segment, "SegmentNumber", None)

        row = {c: "" for c in COLUMNS}
        row.update(instance)
        row.update(
            {
                "SegmentNumber": "" if number is None else str(number),
                "SegmentLabel": _scalar(segment, "SegmentLabel"),
                "SegmentDescription": _scalar(segment, "SegmentDescription"),
                "TrackingID": _scalar(segment, "TrackingID"),
                "TrackingUID": _scalar(segment, "TrackingUID"),
                "AnatomicRegionCodeValue": region_code[0],
                "AnatomicRegionCodingSchemeDesignator": region_code[1],
                "AnatomicRegionCodeMeaning": region_code[2],
                "AnatomicRegionModifierCodeValue": region_mod[0],
                "AnatomicRegionModifierCodingSchemeDesignator": region_mod[1],
                "AnatomicRegionModifierCodeMeaning": region_mod[2],
                "SegmentedPropertyCategoryCodeValue": category[0],
                "SegmentedPropertyCategoryCodingSchemeDesignator": category[1],
                "SegmentedPropertyCategoryCodeMeaning": category[2],
                "SegmentedPropertyTypeCodeValue": type_code[0],
                "SegmentedPropertyTypeCodingSchemeDesignator": type_code[1],
                "SegmentedPropertyTypeCodeMeaning": type_code[2],
                "SegmentedPropertyTypeModifierCodeValue": type_mod[0],
                "SegmentedPropertyTypeModifierCodingSchemeDesignator": type_mod[1],
                "SegmentedPropertyTypeModifierCodeMeaning": type_mod[2],
                "SegmentAlgorithmType": _scalar(segment, "SegmentAlgorithmType"),
                "SegmentAlgorithmName": _scalar(segment, "SegmentAlgorithmName"),
                "RecommendedDisplayCIELabValue": _cielab(segment),
                # Labelmap objects carry a SegmentNumber 0 "Background" segment:
                # an artefact of the encoding, not a segmented finding. Exclude
                # it from any count of what was segmented.
                "isBackgroundSegment": str(number == 0),
                "multiValuedCodeSequence": str(_multi_valued(segment)),
            }
        )
        row.update(_algorithm_identification(segment))
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Source: local files
# ---------------------------------------------------------------------------


def read_files(directory, resolve_referenced=False):
    """Read every SEG object under `directory`. Returns (datasets, skip counts).

    Files are identified by reading the header, not by extension: vendor
    deliveries ship DICOM with no extension, and .dcm on things that are not
    DICOM.
    """
    pydicom = _require_pydicom()
    from pydicom.errors import InvalidDicomError

    datasets, referenced_uids = [], set()
    counts = Counter()

    paths = [p for p in Path(directory).rglob("*") if p.is_file()]
    for path in paths:
        counts["files"] += 1
        try:
            # Metadata is kilobytes; pixel data is not, and nothing here reads
            # a voxel. specific_tags is deliberately unused - it does not
            # descend into SegmentSequence.
            ds = pydicom.dcmread(str(path), stop_before_pixels=True)
        except (InvalidDicomError, OSError):
            counts["not_dicom"] += 1
            continue
        except Exception as exc:  # a truncated or malformed file is a finding
            counts["unreadable"] += 1
            print(f"  ! unreadable: {path.name}: {exc}", file=sys.stderr)
            continue

        sop_class = str(getattr(ds, "SOPClassUID", "") or "")
        if sop_class not in SEG_SOP_CLASSES:
            counts["not_seg"] += 1
            continue
        counts["seg"] += 1
        datasets.append(ds)
        ref = _first(getattr(ds, "ReferencedSeriesSequence", None))
        if ref is not None and getattr(ref, "SeriesInstanceUID", None):
            referenced_uids.add(str(ref.SeriesInstanceUID))

    referenced = {} if resolve_referenced else None
    if resolve_referenced and referenced_uids:
        # One instance of each referenced series is enough for Modality,
        # BodyPartExamined and the Frame of Reference; stop looking once every
        # series is accounted for. A series never found stays out of the dict,
        # which is how issue 17 learns the reference dangles.
        for path in paths:
            if len(referenced) == len(referenced_uids):
                break
            try:
                ds = pydicom.dcmread(str(path), stop_before_pixels=True)
            except Exception:
                continue
            uid = str(getattr(ds, "SeriesInstanceUID", "") or "")
            if uid in referenced_uids and uid not in referenced:
                referenced[uid] = _referenced_facts(ds)

    return datasets, counts, referenced


def _referenced_facts(ds):
    """What the per-segment table records about a referenced image series."""
    return {
        "Modality": str(getattr(ds, "Modality", "") or ""),
        "BodyPartExamined": str(getattr(ds, "BodyPartExamined", "") or ""),
        "FrameOfReferenceUID": str(getattr(ds, "FrameOfReferenceUID", "") or ""),
    }


# ---------------------------------------------------------------------------
# Source: DICOMweb
# ---------------------------------------------------------------------------


def _dicomweb_client(endpoint, use_gcp):
    try:
        from dicomweb_client.api import DICOMwebClient
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            "DICOMweb access needs dicomweb-client:\n"
            "  pip install 'dicomweb-client>=0.59'\n"
            f"({exc})"
        ) from exc

    url = endpoint.strip().rstrip("/")
    if not url.startswith("http"):
        # A Healthcare API resource name: projects/../dicomStores/<store>
        url = f"https://healthcare.googleapis.com/v1/{url}/dicomWeb"
    elif not url.endswith("/dicomWeb"):
        url = f"{url}/dicomWeb"

    session = None
    if use_gcp:
        try:
            from dicomweb_client.ext.gcp.session_utils import (
                create_session_from_gcp_credentials,
            )
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "--gcp needs the GCP extra:\n"
                "  pip install 'dicomweb-client[gcp]' google-auth\n"
                f"({exc})"
            ) from exc
        session = create_session_from_gcp_credentials()

    return DICOMwebClient(url=url, session=session)


def _resolve_referenced_dicomweb(client, datasets):
    """One instance of each referenced series, for Modality, BodyPartExamined
    and the Frame of Reference - one QIDO and one metadata request per distinct
    referenced series.

    The QIDO is an instance search filtered on SeriesInstanceUID at the root,
    which Healthcare API, dcm4chee and Orthanc all accept. A series the store
    does not hold stays out of the dict - issue 17's dangling reference. A
    request that FAILS is recorded as found-but-unknown instead, so a flaky
    connection cannot manufacture a missing series.
    """
    pydicom = _require_pydicom()
    uids = set()
    for ds in datasets:
        ref = _first(getattr(ds, "ReferencedSeriesSequence", None))
        if ref is not None and getattr(ref, "SeriesInstanceUID", None):
            uids.add(str(ref.SeriesInstanceUID))
    referenced = {}
    for uid in sorted(uids):
        try:
            hits = client.search_for_instances(
                search_filters={"SeriesInstanceUID": uid}, limit=1)
        except Exception as exc:
            print(f"  ! referenced series {uid}: search failed ({exc})", file=sys.stderr)
            referenced[uid] = {}
            continue
        if not hits:
            continue
        study = hits[0].get("0020000D", {}).get("Value", [""])[0]
        sop = hits[0].get("00080018", {}).get("Value", [""])[0]
        try:
            metadata = client.retrieve_instance_metadata(
                study_instance_uid=study, series_instance_uid=uid,
                sop_instance_uid=sop)
        except Exception as exc:
            print(f"  ! referenced series {uid}: metadata failed ({exc})",
                  file=sys.stderr)
            referenced[uid] = {}
            continue
        referenced[uid] = _referenced_facts(pydicom.Dataset.from_json(metadata))
    return referenced


def read_dicomweb(endpoint, use_gcp=False, workers=8, limit=None,
                  resolve_referenced=False):
    """Find SEG instances by QIDO, then fetch each series' metadata by WADO.

    QIDO never returns SegmentSequence, whatever includefield says, so the
    segments have to come from a WADO-RS metadata request. That request excludes
    bulk data, so PixelData never crosses the wire.
    """
    pydicom = _require_pydicom()
    client = _dicomweb_client(endpoint, use_gcp)
    counts = Counter()

    # Query each SOP class separately: a comma-separated list in one
    # SOPClassUID parameter is not portable across servers.
    instances = []
    for sop_class in SEG_SOP_CLASSES:
        try:
            instances.extend(
                client.search_for_instances(search_filters={"SOPClassUID": sop_class})
            )
        except Exception as exc:
            print(
                f"  ! SOPClassUID search failed ({exc}); falling back to Modality=SEG",
                file=sys.stderr,
            )
            instances = client.search_for_instances(search_filters={"Modality": "SEG"})
            break

    # One metadata request per SERIES, not per instance.
    series = []
    seen = set()
    for inst in instances:
        study = inst.get("0020000D", {}).get("Value", [""])[0]
        ser = inst.get("0020000E", {}).get("Value", [""])[0]
        if study and ser and (study, ser) not in seen:
            seen.add((study, ser))
            series.append((study, ser))
    if limit:
        series = series[:limit]
    counts["series"] = len(series)

    def fetch(pair):
        study, ser = pair
        return client.retrieve_series_metadata(
            study_instance_uid=study, series_instance_uid=ser
        )

    datasets = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, pair): pair for pair in series}
        for future in as_completed(futures):
            study, ser = futures[future]
            try:
                metadata = future.result()
            except Exception as exc:
                counts["failed_series"] += 1
                print(f"  ! {ser}: {exc}", file=sys.stderr)
                continue
            for item in metadata:
                ds = pydicom.Dataset.from_json(item)
                if str(getattr(ds, "SOPClassUID", "") or "") in SEG_SOP_CLASSES:
                    counts["seg"] += 1
                    datasets.append(ds)
                else:
                    counts["not_seg"] += 1

    referenced = None
    if resolve_referenced:
        referenced = _resolve_referenced_dicomweb(client, datasets)
        counts["referenced_series_resolved"] = sum(1 for v in referenced.values() if v)
    return datasets, counts, referenced


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Flatten DICOM SEG objects into one row per segment.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--files", metavar="DIR", help="directory of DICOM files")
    source.add_argument(
        "--dicomweb",
        metavar="URL_OR_STORE",
        help="DICOMweb base URL, or a Healthcare API store resource name",
    )
    parser.add_argument("-o", "--output", default="seg_attributes.csv")
    parser.add_argument(
        "--viewer-url",
        metavar="PATTERN",
        help="viewer URL pattern with {StudyInstanceUID} / {SeriesInstanceUID} "
        "placeholders. Findings without clickable examples are markedly less "
        "useful - supply this even if the data is not ingested yet.",
    )
    parser.add_argument(
        "--collection", default="", help="collection name for every row, if uniform"
    )
    parser.add_argument(
        "--resolve-referenced",
        action="store_true",
        help="read one instance of each referenced image series - from the same "
        "directory tree, or the same DICOMweb store - for its Modality, "
        "BodyPartExamined and FrameOfReferenceUID. Without it issue 17 (Frame "
        "of Reference) cannot be decided.",
    )
    parser.add_argument("--gcp", action="store_true", help="authenticate to Healthcare API")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, help="stop after N series (smoke test)")
    args = parser.parse_args()

    if args.files:
        print(f"Reading {args.files} ...", file=sys.stderr)
        datasets, counts, referenced = read_files(args.files, args.resolve_referenced)
    else:
        print(f"Querying {args.dicomweb} ...", file=sys.stderr)
        datasets, counts, referenced = read_dicomweb(
            args.dicomweb, args.gcp, args.workers, args.limit, args.resolve_referenced
        )

    rows = []
    for ds in datasets:
        rows.extend(
            extract_instance(ds, args.viewer_url, args.collection, referenced)
        )

    with open(args.output, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    # ---- summary. The skipped counts and the coverage block are findings in
    # their own right, not progress output.
    print(f"\nWrote {len(rows)} segment rows to {args.output}", file=sys.stderr)
    if counts:
        print("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())),
              file=sys.stderr)

    background = sum(1 for r in rows if r["isBackgroundSegment"] == "True")
    multi = sum(1 for r in rows if r["multiValuedCodeSequence"] == "True")
    for key, label in (
        ("SOPInstanceUID", "objects"),
        ("SeriesInstanceUID", "series"),
        ("StudyInstanceUID", "studies"),
        ("PatientID", "patients"),
    ):
        print(f"  {len({r[key] for r in rows if r[key]}):>7} {label}", file=sys.stderr)
    if background:
        print(
            f"  {background:>7} Background segments (SegmentNumber 0) - exclude "
            "these from any count of what was segmented",
            file=sys.stderr,
        )

    # Which sequence carries the anatomy. Category and type are Type 1, the
    # anatomic region Type 3, so this differs by producer - and a report that
    # does not say which is which sends its reader to an empty column.
    print("\n  Code sequences populated (all three are judged by seg_checks.py):",
          file=sys.stderr)
    for prefix in ("AnatomicRegion", "SegmentedPropertyType", "SegmentedPropertyCategory"):
        coded = sum(1 for r in rows if r[f"{prefix}CodeValue"])
        print(f"  {coded:>7} segments carry a {prefix} code", file=sys.stderr)
    if multi:
        print(
            f"\n  !! {multi} segments carry more than one code per sequence. Only the "
            "first\n     was extracted, so every check downstream understates. "
            "Handle these by hand.",
            file=sys.stderr,
        )

    # Issue 12 in one line. seg_encoding.py measures what compression would
    # save; this is only enough to see whether any was applied at all.
    syntaxes = Counter(r["TransferSyntaxUID"] for r in rows if r["TransferSyntaxUID"])
    if syntaxes:
        print("\n  Transfer syntaxes (one count per SEGMENT row):", file=sys.stderr)
        for uid, count in syntaxes.most_common():
            print(f"    {uid:<34} {count:>7}", file=sys.stderr)
        print(
            "    run scripts/seg_encoding.py to size what deflate would save - "
            "issue 12",
            file=sys.stderr,
        )

    empty = [c for c in COVERAGE_COLUMNS if not any(r[c] for r in rows)]
    if empty:
        print(
            "\n  Populated by no instance in this batch - each absence is itself a\n"
            "  finding (see references/access-dicomweb.md, 'What you lose'):",
            file=sys.stderr,
        )
        for column in empty:
            print(f"    - {column}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
