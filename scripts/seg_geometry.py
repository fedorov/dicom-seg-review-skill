#!/usr/bin/env python3
"""Issue 18: the segmentation's grid against the grid of the images it segments.

  python seg_geometry.py seg_attributes.csv -o findings/issue18_geometry.csv
  python seg_geometry.py seg_attributes.csv -o findings/issue18_geometry.csv \\
      --probe orientation
  python seg_geometry.py seg_attributes.csv -o findings/issue18_geometry.csv \\
      --files /path/to/source/images

One row per (segmentation series, referenced image series). It answers two
questions, and neither answer is a pass or a fail:

  1. Is the segmented series there at all? A reference IDC does not hold is
     worth recording - for a delivery derived from IDC it means nobody can
     re-fetch the images, and for a private one it means only that the images
     are private.
  2. Where it IS there, do the two grids agree - Rows and Columns, in-plane
     pixel spacing, image orientation, slice spacing and slice thickness?

A DIFFERENT GRID IS NOT A DEFECT. PS3.3 A.51.1 requires a segmentation to
share its source's Frame of Reference (that is issue 17, and it IS a
conformance rule), but it does not require the same sampling: a segmentation
resampled to an isotropic grid, or cropped to a bounding box, is conformant and
sometimes deliberate. What a mismatch costs is that a consumer has to resample
before the two can be overlaid or compared voxel-wise, so it belongs in the
report as a documented property of the delivery, not in the defect list.

Three ways to resolve the segmented series, tried in this order per series:

  --files DIR   the images on disk. Exact, free, and the whole geometry.
  IDC index     idc-index's series-level tables: existence, collection, and -
                for CT, MR and PT - Rows, Columns, PixelSpacing and
                SliceThickness. NO ImageOrientationPatient: that attribute is
                not in any index, so orientation is reported NOT_COMPARED
                unless --probe or --files supplied it.
  --probe       ranged HTTPS GETs of instance headers from IDC's public
                bucket, ~16 KB each, no credentials. `orientation` reads one
                instance per series and adds the orientation; `full` reads
                every instance and MEASURES the slice spacing from the
                positions. `full` is the only way to learn the real spacing:
                SliceThickness is a nominal value and says nothing about gaps
                or overlap between slices.

Nothing here is ever a conformance verdict, so nothing here is above Low.
Standard library plus, optionally, idc-index (for the IDC tiers) and pydicom
(for --files and --probe).
"""

import argparse
import csv
import io
import math
import sys
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# The geometry the SEG side of the comparison needs. A per-segment table
# written before these existed has none of them, and comparing nothing against
# something would report every series as matching.
REQUIRED_COLUMNS = (
    "Rows",
    "Columns",
    "PixelSpacing",
    "SliceThickness",
    "SpacingBetweenSlices",
    "ImageOrientationPatient",
    "geometrySource",
    "sourceImageReferenceLevel",
)

COLUMNS = [
    "PatientID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "referencedSeriesInstanceUID",
    "geometryObservation",
    "sourceResolvedBy",
    "sourceInIDC",
    "idcCollection",
    "idcAnalysisResult",
    "sourceModality",
    "sourceInstanceCount",
    "segRows",
    "sourceRows",
    "segColumns",
    "sourceColumns",
    "segPixelSpacing",
    "sourcePixelSpacing",
    "segImageOrientationPatient",
    "sourceImageOrientationPatient",
    "orientationAngleDegrees",
    "segSpacingBetweenSlices",
    "sourceSpacingBetweenSlices",
    "sourceSpacingBasis",
    "segSliceThickness",
    "sourceSliceThickness",
    "segGeometrySource",
    "sourceImageReferenceLevel",
    "sourceVolumeIsRegular",
    "objects",
    "referencedSeriesCount",
    "SeriesDescription",
    "viewer_url",
]

# Observation -> the triage tag it rolls up to in seg_checks.py. Two tags, not
# ten: the detail belongs in this CSV, and a triage list carrying a tag per
# geometry attribute would drown the findings that are actually defects.
TAGS = {
    "GRID_SIZE_DIFFERS": "GEOMETRY_DIFFERS",
    "PIXEL_SPACING_DIFFERS": "GEOMETRY_DIFFERS",
    "ORIENTATION_DIFFERS": "GEOMETRY_DIFFERS",
    "SLICE_SPACING_DIFFERS": "GEOMETRY_DIFFERS",
    "SLICE_THICKNESS_DIFFERS": "GEOMETRY_DIFFERS",
    "SOURCE_NOT_IN_IDC": "SOURCE_NOT_IN_IDC",
    "NO_REFERENCED_SERIES": "NO_REFERENCED_SERIES",
}

# Ordering for the geometryObservation column: what differs first, then what
# could not be compared, then the clean statement.
OBSERVATION_ORDER = [
    "GRID_SIZE_DIFFERS",
    "PIXEL_SPACING_DIFFERS",
    "ORIENTATION_DIFFERS",
    "SLICE_SPACING_DIFFERS",
    "SLICE_THICKNESS_DIFFERS",
    "SEG_GEOMETRY_PER_FRAME",
    "SEG_GEOMETRY_ABSENT",
    "NO_REFERENCED_SERIES",
    "SOURCE_NOT_IN_IDC",
    "SOURCE_NOT_RESOLVED",
    "SOURCE_GEOMETRY_UNAVAILABLE",
    "ORIENTATION_NOT_COMPARED",
    "SLICE_SPACING_NOT_COMPARED",
    "SLICE_THICKNESS_NOT_COMPARED",
    "MULTIPLE_REFERENCED_SERIES",
    "GEOMETRY_MATCHES_PARTIAL",
    "GEOMETRY_MATCHES",
]

SEG_SOP_CLASSES = {
    "1.2.840.10008.5.1.4.1.1.66.4",
    "1.2.840.10008.5.1.4.1.1.66.7",
}

# 16 KB reaches PixelData in every single-frame CT, MR and PT this was tried
# on - the whole data set before the pixels is a few kilobytes. A header that
# does not fit is retried once at 1 MB rather than being reported as
# unreadable, because "the header is unusually long" is not a finding about
# the segmentation.
PROBE_BYTES = 16384
PROBE_RETRY_BYTES = 1 << 20


# ---------------------------------------------------------------------------
# Parsing the per-segment table's geometry
# ---------------------------------------------------------------------------


def numbers(value):
    """"1.0/0.0/0.0" -> [1.0, 0.0, 0.0]; anything unparseable -> []."""
    if not value:
        return []
    try:
        return [float(part) for part in str(value).replace("\\", "/").split("/")]
    except ValueError:
        return []


def finite(value):
    """A number from a DataFrame cell, or None.

    pandas writes a missing integer as NaN, which is not None and which int()
    raises on - so an unpopulated Rows in one of IDC's modality indices would
    crash the comparison rather than reporting the geometry as unavailable.
    """
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) or math.isinf(value) else value


def scalar(value):
    """One number, or None. Takes the first of a multi-valued string."""
    parsed = numbers(value)
    return parsed[0] if parsed else None


def format_numbers(values, places=6):
    """A computed vector back to the table's "a/b/c" form.

    Trailing zeros are trimmed so a measured 2.5 reads as "2.5" and not
    "2.500000": these are this script's own numbers, unlike the verbatim
    strings the extractor copies out of the object.
    """
    if values is None:
        return ""
    if not isinstance(values, (list, tuple)):
        values = [values]
    out = []
    for value in values:
        if value is None:
            return ""
        text = f"{float(value):.{places}f}".rstrip("0").rstrip(".")
        out.append(text or "0")
    return "/".join(out)


def close(a, b, relative):
    """Two lengths in mm, equal within a RELATIVE tolerance.

    Relative rather than absolute because the same rounding that makes
    0.9765625 into 0.976562 in one producer's DS scales with the value, and an
    absolute tolerance tight enough for a 0.5 mm spacing would reject a 5 mm
    one that differs only in its last written digit.
    """
    if a is None or b is None:
        return None
    if a == b:
        return True
    scale = max(abs(a), abs(b))
    return scale == 0 or abs(a - b) <= relative * scale


def cross(u, v):
    return [
        u[1] * v[2] - u[2] * v[1],
        u[2] * v[0] - u[0] * v[2],
        u[0] * v[1] - u[1] * v[0],
    ]


def unit(v):
    norm = math.sqrt(sum(c * c for c in v))
    return [c / norm for c in v] if norm else None


def orientation_angle(a, b):
    """Angle in degrees between two Image Orientation (Patient) values, or None.

    Compared as geometry rather than as text. The two vectors are direction
    cosines, so "1\\0\\0\\0\\1\\0" and "0.9999999\\0\\0\\0\\1\\0" are the same
    plane written twice, and a string comparison would call them different. The
    angle reported is the larger of the row and column rotations, which is zero
    only when both axes agree - a slice normal alone would miss an in-plane
    rotation, which changes which voxel is which.
    """
    if len(a) != 6 or len(b) != 6:
        return None
    worst = 0.0
    for offset in (0, 3):
        u = unit(a[offset:offset + 3])
        v = unit(b[offset:offset + 3])
        if u is None or v is None:
            return None
        dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(u, v))))
        worst = max(worst, math.degrees(math.acos(dot)))
    return worst


def slice_spacing(orientation, positions, relative=1e-3):
    """(spacing, regular) measured from Image Position (Patient) values.

    The positions are projected onto the slice normal and sorted, because the
    order instances are listed in is not the order they are stacked in - IDC's
    file names are UUIDs, and a directory listing is alphabetical. The spacing
    is the median consecutive gap; `regular` says whether every gap agreed with
    it, which is what separates a spacing from an average of a series with a
    hole in it.

    Needs ALL the positions. A sample cannot answer this: the smallest gap
    between a random half of the slices is about twice the true spacing, which
    would look like a real disagreement with the segmentation.
    """
    if len(orientation) != 6 or len(positions) < 2:
        return None, None
    normal = unit(cross(orientation[0:3], orientation[3:6]))
    if normal is None:
        return None, None
    projections = sorted(sum(p * n for p, n in zip(pos, normal))
                         for pos in positions if len(pos) == 3)
    gaps = [b - a for a, b in zip(projections, projections[1:])]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return None, None
    gaps.sort()
    median = gaps[len(gaps) // 2]
    regular = all(close(g, median, max(relative, 1e-3)) for g in gaps)
    return median, regular


# ---------------------------------------------------------------------------
# Source: local files
# ---------------------------------------------------------------------------


def _require_pydicom(what):
    try:
        import pydicom
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            f"{what} needs pydicom:\n  pip install 'pydicom>=3.0'\n({exc})"
        ) from exc
    return pydicom


def _from_dataset(ds):
    """The geometry attributes of one single-frame image instance."""
    return {
        "Rows": getattr(ds, "Rows", None),
        "Columns": getattr(ds, "Columns", None),
        "PixelSpacing": [float(v) for v in (getattr(ds, "PixelSpacing", None) or [])],
        "ImageOrientationPatient": [
            float(v) for v in (getattr(ds, "ImageOrientationPatient", None) or [])
        ],
        "ImagePositionPatient": [
            float(v) for v in (getattr(ds, "ImagePositionPatient", None) or [])
        ],
        "SliceThickness": getattr(ds, "SliceThickness", None),
        "SpacingBetweenSlices": getattr(ds, "SpacingBetweenSlices", None),
        "Modality": str(getattr(ds, "Modality", "") or ""),
    }


def _fold(instances, relative):
    """Per-instance geometry -> one series-level record.

    An attribute whose instances DISAGREE is dropped rather than averaged: a
    series whose slices have different in-plane spacings has no one spacing,
    and reporting the first instance's would compare the segmentation against a
    grid that does not exist.
    """
    if not instances:
        return None
    record = {"instanceCount": len(instances),
              "Modality": instances[0].get("Modality", ""),
              "spacingBasis": "",
              "volumeIsRegular": None}
    for field in ("Rows", "Columns", "SliceThickness", "SpacingBetweenSlices"):
        values = {str(i[field]) for i in instances if i.get(field) is not None}
        record[field] = (
            float(values.pop()) if len(values) == 1 else None
        )
    for field in ("PixelSpacing", "ImageOrientationPatient"):
        values = {tuple(i[field]) for i in instances if i.get(field)}
        record[field] = list(values.pop()) if len(values) == 1 else []
    positions = [i["ImagePositionPatient"] for i in instances
                 if len(i.get("ImagePositionPatient") or []) == 3]
    spacing, regular = slice_spacing(
        record["ImageOrientationPatient"], positions, relative)
    if spacing is not None:
        record["SpacingBetweenSlices"] = spacing
        record["spacingBasis"] = "measured" if regular else "measured_irregular"
        record["volumeIsRegular"] = regular
    elif record.get("SpacingBetweenSlices") is not None:
        record["spacingBasis"] = "SpacingBetweenSlices"
    return record


def read_local(directory, wanted, relative):
    """Series-level geometry for `wanted`, from single-frame images under `directory`.

    Anything that is not a readable DICOM image instance is skipped in silence:
    the directory is expected to hold the segmentations too, and often a
    manifest, a README and a .DS_Store.
    """
    pydicom = _require_pydicom("--files")
    from pydicom.errors import InvalidDicomError

    collected = defaultdict(list)
    for path in Path(directory).rglob("*"):
        if not path.is_file():
            continue
        try:
            ds = pydicom.dcmread(str(path), stop_before_pixels=True)
        except (InvalidDicomError, OSError):
            continue
        except Exception:
            continue
        uid = str(getattr(ds, "SeriesInstanceUID", "") or "")
        if uid not in wanted or str(getattr(ds, "SOPClassUID", "")) in SEG_SOP_CLASSES:
            continue
        collected[uid].append(_from_dataset(ds))
    return {uid: _fold(items, relative) for uid, items in collected.items()}


# ---------------------------------------------------------------------------
# Source: the IDC index
# ---------------------------------------------------------------------------

# The series-level indices carrying Rows, Columns, PixelSpacing and
# SliceThickness. One per SOP class that has them; there is no equivalent for
# any other modality, so a referenced series that is neither CT, MR nor PT
# resolves in IDC but yields no geometry.
GEOMETRY_INDICES = ("ct_index", "mr_index", "pt_index")


def idc_lookup(uids):
    """{SeriesInstanceUID: record} for everything IDC holds, plus a note.

    Returns (records, note). `records` has an entry only for series IDC HAS,
    so a UID missing from it is the absence this check documents. `note` is a
    string for the report when a tier could not run - an old idc-index without
    the modality indices still answers "is it in IDC", which is half the check,
    and that has to be visible rather than looking like "no geometry found".
    """
    try:
        from idc_index import index as idc
    except ImportError:
        return None, ("idc-index is not installed - the IDC lookup did not run:\n"
                      "  pip install 'idc-index>=0.12.0'")

    client = idc.IDCClient()
    main = client.index
    hits = main[main["SeriesInstanceUID"].isin(uids)]
    records = {}
    for row in hits.to_dict("records"):
        records[row["SeriesInstanceUID"]] = {
            "collection_id": row.get("collection_id") or "",
            "analysis_result_id": row.get("analysis_result_id") or "",
            "Modality": row.get("Modality") or "",
            "instanceCount": finite(row.get("instanceCount")),
            "aws_bucket": row.get("aws_bucket") or "",
        }

    note = ""
    missing = []
    for name in GEOMETRY_INDICES:
        frame = _fetch(client, name)
        if frame is None:
            missing.append(name)
            continue
        subset = frame[frame["SeriesInstanceUID"].isin(records.keys())]
        for row in subset.to_dict("records"):
            record = records.get(row["SeriesInstanceUID"])
            if record is None:
                continue
            spacing = [finite(row.get("PixelSpacing_row_mm")),
                       finite(row.get("PixelSpacing_col_mm"))]
            record.update({
                "Rows": finite(row.get("Rows")),
                "Columns": finite(row.get("Columns")),
                "PixelSpacing": spacing if None not in spacing else [],
                "SliceThickness": finite(row.get("SliceThickness")),
                # Deliberately not filled from SliceThickness: a nominal
                # thickness is not a spacing, and equating them would compare
                # the segmentation against a number nobody measured.
                "spacingBasis": "",
            })

    geometry = _fetch(client, "volume_geometry_index")
    if geometry is not None:
        subset = geometry[geometry["SeriesInstanceUID"].isin(records.keys())]
        for row in subset.to_dict("records"):
            record = records.get(row["SeriesInstanceUID"])
            regular = row.get("regularly_spaced_3d_volume")
            if record is not None and regular is not None:
                record["volumeIsRegular"] = bool(regular)
    else:
        missing.append("volume_geometry_index")

    if missing:
        note = ("this idc-index has no " + ", ".join(missing) + " - existence was "
                "checked, geometry was not:\n    pip install -U 'idc-index>=0.12.0'")
    return records, note


def _fetch(client, name):
    """One of idc-index's optional indices as a DataFrame, or None."""
    try:
        client.fetch_index(name)
        return getattr(client, name)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Source: probing instance headers over HTTPS
# ---------------------------------------------------------------------------


def _https(url):
    """s3://bucket/key -> an anonymous HTTPS URL for the same object.

    IDC's buckets are public, so no credentials, no boto3 and no s5cmd: a
    plain GET with a Range header reads the header of one instance.
    """
    if url.startswith("s3://"):
        bucket, _, key = url[len("s3://"):].partition("/")
        return f"https://{bucket}.s3.amazonaws.com/{key}"
    if url.startswith("gs://"):
        bucket, _, key = url[len("gs://"):].partition("/")
        return f"https://storage.googleapis.com/{bucket}/{key}"
    return url


def _read_header(url, timeout=60):
    """The data set before PixelData of one instance, by ranged GET, or None."""
    pydicom = _require_pydicom("--probe")
    for size in (PROBE_BYTES, PROBE_RETRY_BYTES):
        request = urllib.request.Request(
            _https(url), headers={"Range": f"bytes=0-{size - 1}"})
        try:
            data = urllib.request.urlopen(request, timeout=timeout).read()
        except Exception:
            return None
        try:
            return pydicom.dcmread(
                io.BytesIO(data), stop_before_pixels=True, force=True)
        except Exception:
            continue
    return None


def probe(uids, records, depth, workers, relative, limit, quiet=False):
    """Fill in what the indices cannot: orientation, and the measured spacing.

    `depth` is "orientation" (one instance per series) or "full" (all of them).
    Only "full" can measure a spacing - see slice_spacing - so a series with
    more than `limit` instances is left alone rather than sampled, and its
    spacing stays NOT_COMPARED. Failures are silent per instance: a series
    whose headers cannot be read keeps whatever the index gave it.
    """
    try:
        from idc_index import index as idc
    except ImportError:
        return
    client = idc.IDCClient()

    jobs = []
    for uid in uids:
        record = records.get(uid)
        if record is None:
            continue
        try:
            urls = client.get_series_file_URLs(uid)
        except Exception:
            continue
        if not urls:
            continue
        if depth == "full" and len(urls) <= limit:
            jobs.append((uid, list(urls)))
        else:
            if depth == "full" and not quiet:
                print(f"  . {uid}: {len(urls)} instances above --probe-limit "
                      f"{limit}; orientation only", file=sys.stderr)
            jobs.append((uid, list(urls)[:1]))

    total = sum(len(urls) for _, urls in jobs)
    if not quiet and total:
        print(f"  probing {total} instance headers over {len(jobs)} series "
              f"(~{total * PROBE_BYTES // 1024} KB)", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for uid, urls in jobs:
            datasets = [ds for ds in pool.map(_read_header, urls) if ds is not None]
            if not datasets:
                continue
            probed = _fold([_from_dataset(ds) for ds in datasets], relative)
            if probed is None:
                continue
            record = records[uid]
            # The probe read the objects; the index only summarised them, so
            # where the two disagree the probe wins.
            for field, value in probed.items():
                if field == "instanceCount":
                    continue
                if value not in (None, [], ""):
                    record[field] = value
            record["probed"] = len(datasets)


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def compare(seg, source, resolved_by, in_idc, spacing_tolerance,
            orientation_tolerance):
    """One (segmentation series, referenced series) row: both grids, and what
    differs.

    Every dimension has three outcomes, never two: differs, agrees, or was not
    compared. The third is the one that matters - an empty geometry column in
    the source looks exactly like a match unless it is said out loud.
    """
    out = {
        "sourceResolvedBy": resolved_by,
        "sourceInIDC": in_idc,
        "segRows": seg.get("Rows", ""),
        "segColumns": seg.get("Columns", ""),
        "segPixelSpacing": seg.get("PixelSpacing", ""),
        "segImageOrientationPatient": seg.get("ImageOrientationPatient", ""),
        "segSliceThickness": seg.get("SliceThickness", ""),
        "segSpacingBetweenSlices": seg.get("SpacingBetweenSlices", ""),
        "segGeometrySource": seg.get("geometrySource", ""),
        "sourceImageReferenceLevel": seg.get("sourceImageReferenceLevel", ""),
    }
    observations = set()

    geometry_source = seg.get("geometrySource", "")
    if geometry_source == "PER_FRAME_VARYING":
        observations.add("SEG_GEOMETRY_PER_FRAME")
    elif geometry_source == "ABSENT":
        observations.add("SEG_GEOMETRY_ABSENT")

    if source is None:
        observations.add(
            "SOURCE_NOT_IN_IDC" if in_idc == "False" else "SOURCE_NOT_RESOLVED")
        out["geometryObservation"] = join(observations)
        return out

    out.update({
        "idcCollection": source.get("collection_id", ""),
        "idcAnalysisResult": source.get("analysis_result_id", ""),
        "sourceModality": source.get("Modality", ""),
        "sourceInstanceCount": (
            "" if source.get("instanceCount") is None
            else str(int(source["instanceCount"]))),
        "sourceRows": (
            "" if source.get("Rows") is None else str(int(source["Rows"]))),
        "sourceColumns": (
            "" if source.get("Columns") is None else str(int(source["Columns"]))),
        "sourcePixelSpacing": format_numbers(source.get("PixelSpacing") or None),
        "sourceImageOrientationPatient": format_numbers(
            source.get("ImageOrientationPatient") or None),
        "sourceSliceThickness": format_numbers(source.get("SliceThickness")),
        "sourceSpacingBetweenSlices": format_numbers(
            source.get("SpacingBetweenSlices")),
        "sourceSpacingBasis": source.get("spacingBasis", ""),
        "sourceVolumeIsRegular": (
            "" if source.get("volumeIsRegular") is None
            else str(bool(source["volumeIsRegular"]))),
    })

    compared = False

    # Rows and Columns. Integers on both sides, so exact - a grid is 512 or it
    # is not.
    seg_rows, seg_columns = scalar(seg.get("Rows")), scalar(seg.get("Columns"))
    src_rows, src_columns = source.get("Rows"), source.get("Columns")
    if None not in (seg_rows, seg_columns) and None not in (src_rows, src_columns):
        compared = True
        if (int(seg_rows), int(seg_columns)) != (int(src_rows), int(src_columns)):
            observations.add("GRID_SIZE_DIFFERS")

    seg_spacing = numbers(seg.get("PixelSpacing"))
    src_spacing = source.get("PixelSpacing") or []
    if len(seg_spacing) == 2 and len(src_spacing) == 2:
        compared = True
        if not all(close(a, b, spacing_tolerance)
                   for a, b in zip(seg_spacing, src_spacing)):
            observations.add("PIXEL_SPACING_DIFFERS")

    seg_orientation = numbers(seg.get("ImageOrientationPatient"))
    src_orientation = source.get("ImageOrientationPatient") or []
    angle = orientation_angle(seg_orientation, src_orientation)
    if angle is None:
        observations.add("ORIENTATION_NOT_COMPARED")
    else:
        compared = True
        out["orientationAngleDegrees"] = format_numbers(angle, places=4)
        if angle > orientation_tolerance:
            observations.add("ORIENTATION_DIFFERS")

    for seg_field, source_field, differs, absent in (
        ("SpacingBetweenSlices", "SpacingBetweenSlices",
         "SLICE_SPACING_DIFFERS", "SLICE_SPACING_NOT_COMPARED"),
        ("SliceThickness", "SliceThickness",
         "SLICE_THICKNESS_DIFFERS", "SLICE_THICKNESS_NOT_COMPARED"),
    ):
        mine, theirs = scalar(seg.get(seg_field)), source.get(source_field)
        if mine is None or theirs is None:
            observations.add(absent)
            continue
        compared = True
        if not close(mine, float(theirs), spacing_tolerance):
            observations.add(differs)

    # The positive statement, and it is split in two on purpose. A row that
    # says GEOMETRY_MATCHES next to ORIENTATION_NOT_COMPARED reads as "the
    # grids agree" when what happened is that the orientation was never looked
    # at - the same silence-as-clean trap the rest of this skill is built to
    # avoid. MATCHES means every dimension agreed; MATCHES_PARTIAL means every
    # dimension that COULD be compared agreed.
    incomplete = any(o.endswith("_NOT_COMPARED") for o in observations)
    if not compared:
        observations.add("SOURCE_GEOMETRY_UNAVAILABLE")
    elif not any(o in TAGS for o in observations):
        observations.add(
            "GEOMETRY_MATCHES_PARTIAL" if incomplete else "GEOMETRY_MATCHES")

    out["geometryObservation"] = join(observations)
    return out


def join(observations):
    return "; ".join(o for o in OBSERVATION_ORDER if o in observations)


# ---------------------------------------------------------------------------


def load(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def references(rows):
    """One entry per (segmentation series, referenced series).

    Keyed on the series and not the object because the grid is a property of
    the series in practice - and where a series' objects disagree, the first
    object's geometry is reported with `objects` saying how many there were.
    """
    seen = {}
    objects = defaultdict(set)
    for row in rows:
        key = (row["SeriesInstanceUID"],
               (row.get("referencedSeriesInstanceUID") or "").strip())
        seen.setdefault(key, row)
        objects[key].add(row["SOPInstanceUID"])
    return [(key, row, len(objects[key])) for key, row in seen.items()]


def main():
    parser = argparse.ArgumentParser(
        description="Issue 18: the segmentation's grid against the segmented "
                    "series', and whether IDC holds that series at all.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("table", help="CSV from seg_attributes.py")
    parser.add_argument("-o", "--output", default="issue18_geometry.csv")
    parser.add_argument(
        "--files",
        help="directory holding the SEGMENTED images. Tried before IDC and "
        "exact where it hits: the whole geometry comes from the objects.",
    )
    parser.add_argument(
        "--no-idc", action="store_true",
        help="skip the IDC lookup. With --files, makes this an offline check; "
        "without it, nothing is resolved and every row is SOURCE_NOT_RESOLVED.",
    )
    parser.add_argument(
        "--probe", choices=("none", "orientation", "full"), default="none",
        help="read instance headers from IDC's public bucket over HTTPS "
        "(default: none). 'orientation' reads ONE instance per series, which "
        "is the only way to get ImageOrientationPatient - no IDC index carries "
        "it. 'full' reads every instance and measures the slice spacing from "
        "the positions.",
    )
    parser.add_argument(
        "--probe-limit", type=int, default=512, metavar="N",
        help="with --probe full, the most instances to read for one series "
        "(default 512). A larger series falls back to one instance: a SAMPLE "
        "cannot measure a spacing, and reporting one would invent a mismatch.",
    )
    parser.add_argument("--workers", type=int, default=8,
                        help="parallel header reads (default 8)")
    parser.add_argument(
        "--spacing-tolerance", type=float, default=1e-3, metavar="REL",
        help="relative tolerance for spacings and thicknesses (default 0.001, "
        "i.e. 0.1%%). Loose enough for DS rounding, tight enough that 1.0 and "
        "1.25 mm differ.",
    )
    parser.add_argument(
        "--orientation-tolerance", type=float, default=0.1, metavar="DEGREES",
        help="how far two Image Orientation (Patient) values may rotate apart "
        "and still count as the same plane (default 0.1 degrees).",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    rows = load(args.table)
    if not rows:
        sys.exit(f"{args.table} is empty")
    missing = [c for c in REQUIRED_COLUMNS if c not in rows[0]]
    if missing:
        sys.exit(
            f"{args.table} has no {', '.join(missing)} column: it was written "
            "before issue 18 existed.\nRe-extract with seg_attributes.py, or "
            "re-deploy sql/01_seg_attributes.sql.")

    pairs = references(rows)
    wanted = {referenced for (_, referenced), _, _ in pairs if referenced}
    print(f"{len(rows)} segment rows; {len(pairs)} (segmentation series, "
          f"referenced series) pairs over {len(wanted)} referenced series")

    local = {}
    if args.files:
        local = read_local(args.files, wanted, args.spacing_tolerance)
        local = {uid: record for uid, record in local.items() if record}
        print(f"  --files {args.files}: {len(local)} of {len(wanted)} referenced "
              "series found on disk")

    idc, idc_note = {}, ""
    unresolved = wanted - set(local)
    if not args.no_idc and unresolved:
        found, idc_note = idc_lookup(unresolved)
        if found is None:
            idc = None
        else:
            idc = found
            print(f"  IDC: {len(idc)} of {len(unresolved)} referenced series are "
                  f"in IDC, {len(unresolved) - len(idc)} are not")
            if args.probe != "none":
                probe(set(idc), idc, args.probe, args.workers,
                      args.spacing_tolerance, args.probe_limit, args.quiet)
                probed = sum(1 for r in idc.values() if r.get("probed"))
                print(f"  --probe {args.probe}: headers read for {probed} series")
    elif args.no_idc:
        idc = None

    findings = []
    for (series, referenced), row, objects in pairs:
        if not referenced:
            # The absence this check exists to document. A SEG naming no
            # segmented series is not skipped, because a pair missing from
            # this CSV would read as "nothing to say about it".
            finding = {
                "geometryObservation": "NO_REFERENCED_SERIES",
                "segRows": row.get("Rows", ""),
                "segColumns": row.get("Columns", ""),
                "segPixelSpacing": row.get("PixelSpacing", ""),
                "segImageOrientationPatient": row.get("ImageOrientationPatient", ""),
                "segSliceThickness": row.get("SliceThickness", ""),
                "segSpacingBetweenSlices": row.get("SpacingBetweenSlices", ""),
                "segGeometrySource": row.get("geometrySource", ""),
                "sourceImageReferenceLevel": row.get(
                    "sourceImageReferenceLevel", ""),
            }
        elif referenced in local:
            source, resolved_by, in_idc = local[referenced], "local_files", ""
        elif idc and referenced in idc:
            source = idc[referenced]
            resolved_by = "idc_probe" if source.get("probed") else "idc_index"
            in_idc = "True"
        else:
            source, resolved_by = None, ""
            in_idc = "" if idc is None else "False"

        if referenced:
            finding = compare(
                source=source, seg=row, resolved_by=resolved_by, in_idc=in_idc,
                spacing_tolerance=args.spacing_tolerance,
                orientation_tolerance=args.orientation_tolerance)
        count = row.get("referencedSeriesCount") or ""
        if count not in ("", "0", "1"):
            # Only the FIRST item of ReferencedSeriesSequence reaches the
            # per-segment table, so everything above describes one of several
            # segmented series.
            finding["geometryObservation"] = join(
                set(finding["geometryObservation"].split("; "))
                | {"MULTIPLE_REFERENCED_SERIES"})
        finding.update({
            "PatientID": row["PatientID"],
            "StudyInstanceUID": row["StudyInstanceUID"],
            "SeriesInstanceUID": series,
            "referencedSeriesInstanceUID": referenced,
            "objects": objects,
            "referencedSeriesCount": count,
            "SeriesDescription": row.get("SeriesDescription", ""),
            "viewer_url": row.get("viewer_url", ""),
        })
        findings.append(finding)

    # Worst first, on the same principle as the triage list: rows where
    # something differs, then rows where something could not be compared, then
    # the clean ones.
    def rank(finding):
        observations = finding["geometryObservation"].split("; ")
        return (
            0 if any(o in TAGS for o in observations) else 1,
            0 if any(o.endswith(("_NOT_COMPARED", "_UNAVAILABLE",
                                 "_NOT_RESOLVED")) for o in observations) else 1,
            finding["PatientID"],
            finding["SeriesInstanceUID"],
        )

    findings.sort(key=rank)

    output = Path(args.output)
    if output.parent != Path(""):
        output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for finding in findings:
            writer.writerow({c: finding.get(c, "") for c in COLUMNS})

    counts = Counter(o for f in findings
                     for o in f["geometryObservation"].split("; ") if o)
    print(f"\n{len(findings)} pairs -> {output}")
    for name in OBSERVATION_ORDER:
        if counts[name]:
            print(f"  {name:<28} {counts[name]:>6}")
    levels = Counter(f["sourceImageReferenceLevel"] for f in findings)
    if counts["NO_REFERENCED_SERIES"]:
        print(f"\n  {counts['NO_REFERENCED_SERIES']} pairs name no segmented "
              "series at all. ReferencedSeriesSequence (0008,1115) is\n  the "
              "only series-level link a SEG has: "
              f"{levels['INSTANCE_ONLY']} of them do record source SOP "
              "instances\n  (Source Image, Referenced Image or per-frame "
              "Derivation Image), which no series-level\n  lookup can follow, "
              f"and {levels['NONE']} record no image reference of any kind.")
    if not any(counts[o] for o in TAGS):
        print("  ok: nothing that could be compared differs, and every "
              "referenced series was found")
    print("\n  A different grid is NOT a conformance failure - PS3.3 A.51.1 "
          "requires a shared\n  Frame of Reference (issue 17), not a shared "
          "sampling. Report these as properties\n  of the delivery, and fold "
          "them in with `seg_checks.py --geometry`.")
    if idc_note:
        print(f"\n  ! {idc_note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
