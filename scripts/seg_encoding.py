#!/usr/bin/env python3
"""How a SEG delivery is encoded: empty frames (issue 11) and compression (issue 12).

Both need the objects. A BigQuery export and a DICOMweb metadata response each
drop the file meta group, so neither carries the Transfer Syntax, and neither
carries a single voxel — so like dciodvfy (issue 9) this runs on the
local-files path only. Where it does not run, say so in the report rather than
letting the silence read as a pass.

  python scripts/seg_encoding.py --files /path/to/delivery -o findings/
  python scripts/seg_checks.py seg_attributes.csv \\
      --encoding findings/encoding_per_object.csv --outdir findings/

What it reads, and why it is affordable: the frames are scanned as BYTES, not
decoded into an array. A BINARY segment frame is empty exactly when every bit
of its bit range is zero, and that is an integer mask over a slice — no numpy,
no pixel handler, no per-voxel loop in Python.

Four outputs:

  encoding_per_object.csv       one row per object. The join contract, the way
                                issue9_iod_validation.csv is for dciodvfy.
  issue11_empty_frames.csv      objects that kept all-zero frames, worst first
  issue11_empty_segments.csv    segments with no voxels anywhere in the object
  issue12_compression.csv       one row per transfer syntax, with the MEASURED
                                deflated size beside the delivered one

Needs pydicom. Deflated Image Frame Compression is decoded here rather than by
pydicom — see DEFLATED_IMAGE_FRAME below.
"""

import argparse
import csv
import os
import struct
import sys
import zlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SEG_SOP_CLASSES = {
    "1.2.840.10008.5.1.4.1.1.66.4": "Segmentation Storage",
    "1.2.840.10008.5.1.4.1.1.66.7": "Labelmap Segmentation Storage",
}

# PS3.5 A.5: the whole Data Set through one deflate stream. pydicom inflates it
# transparently on read, so by the time we see PixelData it is plain bytes.
DEFLATED_DATASET = "1.2.840.10008.1.2.1.99"

# PS3.5 A.4.13, new enough that tooling has not caught up: each FRAME is
# deflated into its own encapsulated fragment. PS3.5 8.2.16 names bilevel
# (Bits Allocated == 1) Segmentation as its motivating application, so it is
# the compression the Standard designed for the objects this skill reviews.
#
# pydicom 3.0.1 does not know this UID at all - `UID.is_encapsulated` raises
# "UID is not a transfer syntax" and dcmwrite refuses it - though dcmread
# still parses such a file, falling back to Explicit VR Little Endian. That is
# why the fragments are split and inflated here: it costs ~20 lines and means
# the check works on the one transfer syntax a producer following the Standard
# is most likely to have used.
DEFLATED_IMAGE_FRAME = "1.2.840.10008.1.2.8.1"

# Uncompressed. Anything else encapsulates, and needs a codec to read.
NATIVE_SYNTAXES = {
    "1.2.840.10008.1.2",  # Implicit VR Little Endian
    "1.2.840.10008.1.2.1",  # Explicit VR Little Endian
    "1.2.840.10008.1.2.2",  # Explicit VR Big Endian (retired)
    DEFLATED_DATASET,
}

# Transfer syntaxes that discard information. PS3.3 C.8.20.2.2: "It is not
# advisable to lossy compress a Segmentation Instance. In particular, BINARY
# or LABELMAP Segmentation Instances should not be lossy compressed."
LOSSY_SYNTAXES = {
    "1.2.840.10008.1.2.4.50": "JPEG Baseline",
    "1.2.840.10008.1.2.4.51": "JPEG Extended",
    "1.2.840.10008.1.2.4.81": "JPEG-LS Lossy",
    "1.2.840.10008.1.2.4.91": "JPEG 2000",
    "1.2.840.10008.1.2.4.93": "JPEG 2000 Part 2 Multi-component",
    "1.2.840.10008.1.2.4.203": "High-Throughput JPEG 2000",
}

SYNTAX_NAMES = {
    "1.2.840.10008.1.2": "Implicit VR Little Endian",
    "1.2.840.10008.1.2.1": "Explicit VR Little Endian",
    "1.2.840.10008.1.2.2": "Explicit VR Big Endian (retired)",
    DEFLATED_DATASET: "Deflated Explicit VR Little Endian",
    DEFLATED_IMAGE_FRAME: "Deflated Image Frame Compression",
    "1.2.840.10008.1.2.5": "RLE Lossless",
    "1.2.840.10008.1.2.4.70": "JPEG Lossless",
    "1.2.840.10008.1.2.4.80": "JPEG-LS Lossless",
    "1.2.840.10008.1.2.4.90": "JPEG 2000 Lossless",
    "1.2.840.10008.1.2.4.201": "High-Throughput JPEG 2000 Lossless",
}
SYNTAX_NAMES.update(LOSSY_SYNTAXES)

OBJECT_COLUMNS = [
    "PatientID", "StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID",
    "SegmentationType", "TransferSyntaxUID", "transferSyntaxName",
    "isCompressed", "isLossy", "LossyImageCompression", "BitsAllocated",
    "Rows", "Columns", "numberOfFrames", "segmentsInInstance",
    "framesScanned", "emptyFrames", "emptyFrameFraction", "emptySegmentCount",
    "emptySegmentNumbers", "fileSizeBytes", "pixelDataBytes",
    "uncompressedPixelDataBytes", "deflatedPixelDataBytes",
    "deflateSavingFraction", "decodeStatus",
    "SeriesDescription", "filePath", "viewer_url",
]

EMPTY_FRAME_COLUMNS = [
    "PatientID", "StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID",
    "SegmentationType", "numberOfFrames", "emptyFrames", "emptyFrameFraction",
    "wastedBytesEstimate", "fileSizeBytes", "isCompressed",
    "SeriesDescription", "viewer_url",
]

EMPTY_SEGMENT_COLUMNS = [
    "PatientID", "StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID",
    "SegmentNumber", "SegmentLabel", "SegmentationType", "framesForSegment",
    "SeriesDescription", "viewer_url",
]

COMPRESSION_COLUMNS = [
    "TransferSyntaxUID", "transferSyntaxName", "isCompressed", "isLossy",
    "objects", "series", "fileSizeBytes", "pixelDataBytes",
    "uncompressedPixelDataBytes", "deflatedPixelDataBytes",
    "deflateSavingFraction", "exampleSOPInstanceUID",
    "viewer_url",
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
# Frame scanning. Stdlib only, no pydicom - this is the part under test.
# ---------------------------------------------------------------------------


def split_fragments(data):
    """Encapsulated Pixel Data -> the list of fragment payloads.

    Parsed here rather than through pydicom.encaps so that a transfer syntax
    pydicom does not recognise still yields its frames. The Basic Offset Table
    is the first item and is dropped: it is an index, not pixel data, and is
    routinely empty.
    """
    fragments = []
    offset = 0
    first = True
    while offset + 8 <= len(data):
        group, element, length = struct.unpack_from("<HHI", data, offset)
        offset += 8
        if (group, element) == (0xFFFE, 0xE0DD):  # Sequence Delimiter
            break
        if (group, element) != (0xFFFE, 0xE000):  # not an Item
            raise ValueError(
                f"not an encapsulated item at offset {offset - 8}: "
                f"({group:04x},{element:04x})"
            )
        payload = data[offset:offset + length]
        offset += length
        if first:  # Basic Offset Table
            first = False
            continue
        fragments.append(payload)
    return fragments


def inflate(payload):
    """One deflated frame -> its bytes.

    PS3.5 A.4.13 says RFC1951, i.e. a raw deflate stream with no zlib wrapper,
    and allows a single trailing NULL pad byte. Writers that wrap it in a zlib
    header exist, so both are accepted — being strict here would report a
    decode failure where the frames are perfectly readable.
    """
    try:
        return zlib.decompress(payload, -zlib.MAX_WBITS)
    except zlib.error:
        return zlib.decompress(payload)


def frame_is_empty(data, frame, rows, columns, bits_allocated):
    """Does frame `frame` of a NATIVE-format Pixel Data value hold only zeros?

    The bit-packed case is the one that bites. PS3.5 §8.1: "In a Multi-frame
    Image with a Bits Allocated (0028,0100) of 1 that is transmitted in Native
    Format, the individual Frames are not padded ... a frame other than the
    first frame may start in the middle of a byte or word." So a BINARY SEG's
    frames are one continuous bit stream, and slicing it at byte boundaries
    silently reads a neighbour's pixels — which, for this check, turns an empty
    frame into a non-empty one whenever the frame before it ended mid-byte.

    Within a byte the first pixel is the least significant bit, so reading the
    byte range little-endian puts pixel n at bit n of the integer and the mask
    is a plain shift.

    Encapsulated frames are the opposite case and go through
    `payload_is_empty`: each frame is its own fragment and starts on a byte.
    """
    pixels = rows * columns
    if bits_allocated == 1:
        bit_low = frame * pixels
        bit_high = bit_low + pixels
        byte_low = bit_low // 8
        byte_high = (bit_high + 7) // 8
        chunk = data[byte_low:byte_high]
        if len(chunk) < byte_high - byte_low:
            raise ValueError(
                f"PixelData is short: frame {frame} needs bytes "
                f"{byte_low}:{byte_high}, have {len(data)}"
            )
        value = int.from_bytes(chunk, "little")
        mask = ((1 << pixels) - 1) << (bit_low - byte_low * 8)
        return value & mask == 0

    stride = pixels * (bits_allocated // 8)
    chunk = data[frame * stride:(frame + 1) * stride]
    if len(chunk) < stride:
        raise ValueError(
            f"PixelData is short: frame {frame} needs {stride} bytes, "
            f"have {len(chunk)}"
        )
    return not any(chunk)


def payload_is_empty(payload, rows, columns, bits_allocated):
    """Does one ENCAPSULATED frame's own byte stream hold only zeros?

    PS3.5 A.4.13: "Each frame shall be encoded in one and only one Fragment",
    so unlike the native case a frame starts on a byte boundary and the
    continuous-bit-stream arithmetic above does not apply — running it over a
    fragment would read past the frame. The trailing bits of the last byte
    belong to no pixel and are masked off rather than trusted to be zero.
    """
    pixels = rows * columns
    if bits_allocated == 1:
        needed = (pixels + 7) // 8
        if len(payload) < needed:
            raise ValueError(
                f"fragment is short: needs {needed} bytes, have {len(payload)}"
            )
        value = int.from_bytes(payload[:needed], "little")
        return value & ((1 << pixels) - 1) == 0
    needed = pixels * (bits_allocated // 8)
    if len(payload) < needed:
        raise ValueError(
            f"fragment is short: needs {needed} bytes, have {len(payload)}"
        )
    return not any(payload[:needed])


def labelmap_values(data, bits_allocated):
    """The set of segment numbers a LABELMAP object's pixel data actually uses.

    A LABELMAP frame is not per-segment: one frame carries every segment, as
    integer values. Which segments are present is therefore a question about
    values, not about frames, and PS3.3 C.8.20.2.3.3 explicitly permits a
    Segment Sequence that describes segments the pixel data never uses.
    """
    if bits_allocated == 8:
        return set(data)
    if bits_allocated == 16:
        usable = len(data) - len(data) % 2
        return set(struct.unpack(f"<{usable // 2}H", data[:usable]))
    raise ValueError(f"LABELMAP with Bits Allocated {bits_allocated}")


# ---------------------------------------------------------------------------
# One object
# ---------------------------------------------------------------------------


def segment_frames(dataset):
    """frame index -> ReferencedSegmentNumber, from the per-frame groups.

    Empty when the object does not carry them, in which case empty frames are
    still counted but cannot be attributed to a segment.
    """
    mapping = {}
    groups = getattr(dataset, "PerFrameFunctionalGroupsSequence", None) or []
    for index, item in enumerate(groups):
        identification = getattr(item, "SegmentIdentificationSequence", None)
        if not identification:
            continue
        number = getattr(identification[0], "ReferencedSegmentNumber", None)
        if number is not None:
            mapping[index] = int(number)
    return mapping


def scan(dataset, path, viewer_url_pattern=None):
    """One object -> its encoding row, plus the segments that hold no voxels."""
    syntax = str(
        getattr(getattr(dataset, "file_meta", None), "TransferSyntaxUID", "") or ""
    )
    rows_ = int(getattr(dataset, "Rows", 0) or 0)
    columns = int(getattr(dataset, "Columns", 0) or 0)
    bits = int(getattr(dataset, "BitsAllocated", 0) or 0)
    declared_frames = int(getattr(dataset, "NumberOfFrames", 0) or 0)
    segmentation_type = str(getattr(dataset, "SegmentationType", "") or "")
    segments = getattr(dataset, "SegmentSequence", None) or []

    study = str(getattr(dataset, "StudyInstanceUID", "") or "")
    series = str(getattr(dataset, "SeriesInstanceUID", "") or "")
    sop = str(getattr(dataset, "SOPInstanceUID", "") or "")
    viewer_url = ""
    if viewer_url_pattern:
        viewer_url = viewer_url_pattern.format(
            StudyInstanceUID=study, SeriesInstanceUID=series, SOPInstanceUID=sop
        )

    row = {column: "" for column in OBJECT_COLUMNS}
    row.update({
        "PatientID": str(getattr(dataset, "PatientID", "") or ""),
        "StudyInstanceUID": study,
        "SeriesInstanceUID": series,
        "SOPInstanceUID": sop,
        "SegmentationType": segmentation_type,
        "TransferSyntaxUID": syntax,
        "transferSyntaxName": SYNTAX_NAMES.get(syntax, "(unrecognised)"),
        "isCompressed": str(syntax not in NATIVE_SYNTAXES or syntax == DEFLATED_DATASET),
        "isLossy": str(syntax in LOSSY_SYNTAXES),
        "LossyImageCompression": str(
            getattr(dataset, "LossyImageCompression", "") or ""
        ),
        "BitsAllocated": bits,
        "Rows": rows_,
        "Columns": columns,
        "numberOfFrames": declared_frames,
        "segmentsInInstance": len(segments),
        "fileSizeBytes": os.path.getsize(path) if path else "",
        "SeriesDescription": str(getattr(dataset, "SeriesDescription", "") or ""),
        "filePath": str(path or ""),
        "viewer_url": viewer_url,
    })

    # getattr, not Dataset.get: on a keyword the latter already returns the
    # value, and the difference is invisible until it raises.
    data = getattr(dataset, "PixelData", None)
    if not data:
        row["decodeStatus"] = "NO_PIXEL_DATA"
        return row, []

    row["pixelDataBytes"] = len(data)

    # Two shapes, and they must not be confused. `stream` is one continuous
    # bit stream across all frames (native encoding); `payloads` is one byte
    # string per frame (encapsulated). Only one is ever set.
    stream, payloads = None, None
    if syntax in NATIVE_SYNTAXES:
        stream = data
    elif syntax == DEFLATED_IMAGE_FRAME:
        try:
            payloads = [inflate(fragment) for fragment in split_fragments(data)]
        except (ValueError, zlib.error) as exc:
            row["decodeStatus"] = f"DEFLATE_FAILED: {exc}"
            return row, []
    else:
        # RLE and the JPEG family need a codec. Not worth the dependency: no
        # producer should be emitting them for a segmentation, and issue 12
        # reports the transfer syntax whether or not the frames were read.
        row["decodeStatus"] = "NOT_DECODED: needs a codec"
        return row, []

    flat = stream if stream is not None else b"".join(payloads)
    row["uncompressedPixelDataBytes"] = len(flat)

    # What deflate would actually save, MEASURED on this object's own pixel
    # data rather than asserted. Compressing the whole stream at once is what
    # the dataset-level syntax does; per-frame deflate gives up a little of
    # this, so read the number as the optimistic end of the range and say so.
    if syntax in NATIVE_SYNTAXES and syntax != DEFLATED_DATASET and flat:
        deflated = len(zlib.compress(flat, 9))
        row["deflatedPixelDataBytes"] = deflated
        row["deflateSavingFraction"] = round(1 - deflated / len(flat), 4)

    if not (rows_ and columns and bits):
        row["decodeStatus"] = "NO_GEOMETRY"
        return row, []

    if payloads is not None:
        available = len(payloads)
        frames = declared_frames or available
        if available != frames:
            # A.4.13: one frame, one fragment. Anything else and the frames
            # cannot be attributed, so do not pretend otherwise.
            row["decodeStatus"] = (
                f"FRAGMENT_COUNT_MISMATCH: {available} fragments, "
                f"{frames} frames declared"
            )
            frames = min(available, frames)
    else:
        per_frame_bits = rows_ * columns * bits
        available = len(flat) * 8 // per_frame_bits if per_frame_bits else 0
        frames = declared_frames or available
        if available < frames:
            row["decodeStatus"] = (
                f"SHORT_PIXEL_DATA: {available} frames of {frames} declared"
            )
            frames = available

    empty_frames = set()
    try:
        for index in range(frames):
            if payloads is not None:
                empty = payload_is_empty(payloads[index], rows_, columns, bits)
            else:
                empty = frame_is_empty(flat, index, rows_, columns, bits)
            if empty:
                empty_frames.add(index)
    except ValueError as exc:
        row["decodeStatus"] = f"SHORT_PIXEL_DATA: {exc}"

    row["framesScanned"] = frames
    row["emptyFrames"] = len(empty_frames)
    row["emptyFrameFraction"] = round(len(empty_frames) / frames, 4) if frames else ""
    if not row["decodeStatus"]:
        row["decodeStatus"] = "OK"

    # Which segments hold nothing at all.
    empty_segments = []
    declared = [
        (int(getattr(item, "SegmentNumber", -1) or -1),
         str(getattr(item, "SegmentLabel", "") or ""))
        for item in segments
    ]
    if segmentation_type == "LABELMAP":
        try:
            present = labelmap_values(flat, bits)
        except (ValueError, struct.error):
            present = None
        if present is not None:
            for number, label in declared:
                # Segment 0 is the Background artefact, not a finding.
                if number > 0 and number not in present:
                    empty_segments.append((number, label, 0))
    else:
        mapping = segment_frames(dataset)
        if mapping:
            per_segment = defaultdict(list)
            for index, number in mapping.items():
                if index < frames:
                    per_segment[number].append(index)
            for number, label in declared:
                owned = per_segment.get(number, [])
                if owned and all(index in empty_frames for index in owned):
                    empty_segments.append((number, label, len(owned)))

    row["emptySegmentCount"] = len(empty_segments)
    row["emptySegmentNumbers"] = ";".join(str(n) for n, _, _ in empty_segments)

    findings = [
        {
            "PatientID": row["PatientID"],
            "StudyInstanceUID": study,
            "SeriesInstanceUID": series,
            "SOPInstanceUID": sop,
            "SegmentNumber": number,
            "SegmentLabel": label,
            "SegmentationType": segmentation_type,
            "framesForSegment": owned,
            "SeriesDescription": row["SeriesDescription"],
            "viewer_url": viewer_url,
        }
        for number, label, owned in empty_segments
    ]
    return row, findings


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


def read_batch(directory, viewer_url_pattern, workers, limit=None):
    pydicom = _require_pydicom()
    from pydicom.errors import InvalidDicomError

    counts = Counter()
    paths = [path for path in Path(directory).rglob("*") if path.is_file()]
    targets = []
    for path in paths:
        counts["files"] += 1
        try:
            header = pydicom.dcmread(str(path), stop_before_pixels=True)
        except (InvalidDicomError, OSError):
            counts["not_dicom"] += 1
            continue
        except Exception:
            counts["unreadable"] += 1
            continue
        if str(getattr(header, "SOPClassUID", "") or "") not in SEG_SOP_CLASSES:
            counts["not_seg"] += 1
            continue
        counts["seg"] += 1
        targets.append(path)
    if limit:
        targets = targets[:limit]

    def work(path):
        # The whole object this time: this is the pass that reads PixelData.
        dataset = pydicom.dcmread(str(path))
        return scan(dataset, path, viewer_url_pattern)

    objects, empty_segments = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, path): path for path in targets}
        for future in as_completed(futures):
            path = futures[future]
            try:
                row, findings = future.result()
            except Exception as exc:
                counts["failed"] += 1
                print(f"  ! {path.name}: {exc}", file=sys.stderr)
                continue
            objects.append(row)
            empty_segments.extend(findings)
    return objects, empty_segments, counts


def compression_summary(objects):
    """One row per transfer syntax. This is what the report quotes."""
    grouped = defaultdict(list)
    for row in objects:
        grouped[row["TransferSyntaxUID"]].append(row)

    summary = []
    for syntax, group in grouped.items():
        delivered = sum(int(r["fileSizeBytes"] or 0) for r in group)
        pixels = sum(int(r["pixelDataBytes"] or 0) for r in group)
        deflated = sum(int(r["deflatedPixelDataBytes"] or 0) for r in group)
        summary.append({
            "TransferSyntaxUID": syntax,
            "transferSyntaxName": SYNTAX_NAMES.get(syntax, "(unrecognised)"),
            "isCompressed": group[0]["isCompressed"],
            "isLossy": group[0]["isLossy"],
            "objects": len(group),
            "series": len({r["SeriesInstanceUID"] for r in group}),
            "fileSizeBytes": delivered,
            "pixelDataBytes": pixels,
            "deflatedPixelDataBytes": deflated or "",
            "deflateSavingFraction": (
                round(1 - deflated / pixels, 4) if deflated and pixels else ""
            ),
            "exampleSOPInstanceUID": group[0]["SOPInstanceUID"],
            "viewer_url": group[0]["viewer_url"],
        })
    summary.sort(key=lambda r: -r["objects"])
    return summary


def empty_frame_findings(objects):
    findings = []
    for row in objects:
        if not row["emptyFrames"]:
            continue
        frames = int(row["framesScanned"] or 0)
        pixels = int(row["pixelDataBytes"] or 0)
        findings.append({
            "PatientID": row["PatientID"],
            "StudyInstanceUID": row["StudyInstanceUID"],
            "SeriesInstanceUID": row["SeriesInstanceUID"],
            "SOPInstanceUID": row["SOPInstanceUID"],
            "SegmentationType": row["SegmentationType"],
            "numberOfFrames": frames,
            "emptyFrames": row["emptyFrames"],
            "emptyFrameFraction": row["emptyFrameFraction"],
            # Uncompressed bytes the empty frames occupy. On a compressed
            # object they cost far less than this, which is the point: the
            # number quantifies the object as delivered, so state the transfer
            # syntax beside it.
            "wastedBytesEstimate": (
                int(pixels * row["emptyFrames"] / frames) if frames else ""
            ),
            "fileSizeBytes": row["fileSizeBytes"],
            "isCompressed": row["isCompressed"],
            "SeriesDescription": row["SeriesDescription"],
            "viewer_url": row["viewer_url"],
        })
    findings.sort(key=lambda r: -int(r["wastedBytesEstimate"] or 0))
    return findings


def write(outdir, name, rows, columns):
    path = Path(outdir) / name
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def human(count):
    value = float(count)
    for unit in ("B", "kB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024


def main():
    parser = argparse.ArgumentParser(
        description="Empty frames and compression across a SEG delivery.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--files", required=True, help="directory of DICOM files")
    parser.add_argument("-o", "--outdir", default="findings")
    parser.add_argument(
        "--viewer-url",
        help="pattern with {StudyInstanceUID} / {SeriesInstanceUID}. Findings "
        "without a clickable example are markedly less useful.",
    )
    parser.add_argument("--workers", type=int, default=min(8, (os.cpu_count() or 2)))
    parser.add_argument("--limit", type=int, help="scan only the first N objects")
    args = parser.parse_args()

    objects, empty_segments, counts = read_batch(
        args.files, args.viewer_url, args.workers, args.limit
    )
    if not objects:
        sys.exit(
            f"no segmentation objects read under {args.files} "
            f"({counts['files']} files, {counts['not_seg']} not SEG)"
        )

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    empty_frames = empty_frame_findings(objects)
    compression = compression_summary(objects)

    join_path = write(outdir, "encoding_per_object.csv", objects, OBJECT_COLUMNS)
    write(outdir, "issue11_empty_frames.csv", empty_frames, EMPTY_FRAME_COLUMNS)
    write(outdir, "issue11_empty_segments.csv", empty_segments,
          EMPTY_SEGMENT_COLUMNS)
    write(outdir, "issue12_compression.csv", compression, COMPRESSION_COLUMNS)

    print(f"{counts['files']} files, {counts['seg']} segmentation objects, "
          f"{counts['not_seg']} not SEG, {counts['not_dicom']} not DICOM")
    statuses = Counter(row["decodeStatus"] for row in objects)
    undecoded = sum(n for status, n in statuses.items() if status != "OK")
    if undecoded:
        print("  ! pixel data not read for some objects - issue 11 understates "
              "by that much:")
        for status, count in statuses.most_common():
            if status != "OK":
                print(f"      {count:>6}  {status}")

    binary = [r for r in objects if r["SegmentationType"] == "BINARY"]
    scanned = [r for r in objects if r["decodeStatus"] == "OK"]
    total_frames = sum(int(r["framesScanned"] or 0) for r in scanned)
    total_empty = sum(int(r["emptyFrames"] or 0) for r in scanned)
    wasted = sum(int(r["wastedBytesEstimate"] or 0) for r in empty_frames)
    print(f"\nIssue 11 empty frames    {len(empty_frames):>6} objects of "
          f"{len(scanned)} scanned keep at least one all-zero frame")
    if total_frames:
        print(f"           {total_empty} of {total_frames} frames are empty "
              f"({total_empty / total_frames:.1%}), about {human(wasted)} "
              "uncompressed")
    if binary:
        binary_empty = [r for r in empty_frames if r["SegmentationType"] == "BINARY"]
        print(f"           {len(binary_empty)} of {len(binary)} BINARY objects - "
              "this is where omitting them is the norm")
    if empty_segments:
        print(f"           {len(empty_segments)} segments have NO voxels at all "
              "-> issue11_empty_segments.csv")
    else:
        print("           ok: every segment declared has voxels somewhere")

    print(f"\nIssue 12 compression     {len(compression)} transfer "
          f"synta{'x' if len(compression) == 1 else 'xes'}")
    for row in compression:
        note = ""
        if row["isLossy"] == "True":
            note = "  <- LOSSY; PS3.3 C.8.20.2.2 says segmentations should not be"
        elif row["deflateSavingFraction"] != "":
            note = f"  <- deflate would save {row['deflateSavingFraction']:.0%}"
        print(f"    {row['objects']:>6} objects  "
              f"{human(row['fileSizeBytes']):>10}  "
              f"{row['TransferSyntaxUID']:<26} {row['transferSyntaxName']}{note}")

    print(f"\nFold into the triage list:\n"
          f"  python scripts/seg_checks.py seg_attributes.csv "
          f"--encoding {join_path} --outdir {outdir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
