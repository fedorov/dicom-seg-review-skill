#!/usr/bin/env python3
"""Run dciodvfy over a delivery of DICOM SEG files and normalise what it says.

dciodvfy (David Clunie's dicom3tools) validates an object against the IOD
definition in PS3.3: which modules a Segmentation must carry, which attributes
in them are Type 1, 1C, 2 or 3, their value multiplicity, and their enumerated
values. It also reads PixelData and checks its length against the geometry the
metadata declares - the only part of this review that leaves the metadata.

It is the authority on structure. It says nothing about whether a code denotes
the anatomy its CodeMeaning claims, and it interprets no voxel values - issues
1, 2, 3 and 8 are outside its reach entirely. See references/issue-catalogue.md,
issue 9.

  pip install dicom3tools
  python scripts/dciodvfy_check.py --files /path/to/delivery -o findings/

Always run with -new: it prints the path to the offending attribute, including
the sequence item index, which is what lets a message be attributed to a
segment rather than to a file. This script passes it, and -allpffgitems, always.

Needs pydicom to resolve UIDs and segment numbers; the parser itself is
stdlib-only and is what the tests exercise.
"""

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SEG_SOP_CLASSES = {
    "1.2.840.10008.5.1.4.1.1.66.4": "Segmentation Storage",
    "1.2.840.10008.5.1.4.1.1.66.7": "Labelmap Segmentation Storage",
}

# Free text -> a stable class, so findings aggregate across a batch. Ordered:
# first match wins, since "Missing attribute for Type 1C" also contains
# "Type 1". Anything unmatched keeps its full message and lands in OTHER,
# which is deliberate - an unrecognised message must not be silently dropped.
MESSAGE_CLASSES = [
    (r"Missing attribute for Type 1C", "MISSING_TYPE1C"),
    (r"Missing attribute for Type 1", "MISSING_TYPE1"),
    (r"Missing attribute for Type 2C", "MISSING_TYPE2C"),
    (r"Missing attribute for Type 2", "MISSING_TYPE2"),
    (r"Empty attribute .*for Type 1", "EMPTY_TYPE1"),
    (r"Empty attribute", "EMPTY_ATTRIBUTE"),
    (r"Bad attribute Value Multiplicity", "BAD_VM"),
    # The one class that is not about metadata: dciodvfy reads PixelData and
    # checks its length against Rows x Columns x Frames x BitsAllocated. On a
    # SEG this catches a truncated object, or one claiming more frames than it
    # carries - which nothing else in this review would notice.
    (r"has incorrect value length", "BAD_VALUE_LENGTH"),
    (r"Bad Pixel Image|Bad PixelRepresentation", "BAD_PIXEL_ENCODING"),
    (r"Bad Value Length", "BAD_VALUE_LENGTH"),
    (r"Bad Sequence number of Items", "BAD_SEQUENCE_ITEM_COUNT"),
    (r"does not match a SegmentNumber", "BAD_SEGMENT_REFERENCE"),
    (r"CodingSchemeDesignator is deprecated", "DEPRECATED_CODING_SCHEME"),
    (r"Code(Value|Meaning) is illegal or deprecated", "DEPRECATED_CODE"),
    (r"Unrecognized enumerated value", "BAD_ENUMERATED_VALUE"),
    (r"Unrecognized defined term", "BAD_DEFINED_TERM"),
    (r"Value dubious for this VR", "DUBIOUS_VALUE_FOR_VR"),
    (r"Bad attribute value", "BAD_ATTRIBUTE_VALUE"),
    (r"retired|Retired", "RETIRED"),
    (r"Unrecognized SOP Class", "UNRECOGNIZED_SOP_CLASS"),
]

# dciodvfy's own Error/Warning split maps onto the skill's severity scale
# almost exactly: an Error is non-conformant but detectable (Medium), a Warning
# is cosmetic (Low). NOTHING dciodvfy reports is High - High is reserved for an
# assertion that is false and undetectable, and a message here is by definition
# the detection. See SKILL.md, "Severity is about consumer harm".
#
# One exception. A retired CodingSchemeDesignator is only a Warning to dciodvfy
# but it silently breaks every terminology lookup downstream, so it is promoted
# and carries its own tag. That is issue 10, and it is computable from the
# per-segment table alone - seg_checks.py runs it on all three access paths.
PROMOTED = {"DEPRECATED_CODING_SCHEME": "Medium"}
SEVERITY_FROM_DCIODVFY = {"Error": "Medium", "Warning": "Low"}

# Filename: "some/path.dcm"
FILENAME_LINE = re.compile(r'^Filename:\s*"(?P<path>.*)"\s*$')
# Error - </Seq(0062,0002)[1]/Attr(0062,0005)> - message - Module=<X>
MESSAGE_LINE = re.compile(r"^(?P<severity>Error|Warning)\s+-\s+(?P<body>.*)$")
PATH_PREFIX = re.compile(r"^<(?P<path>/[^>]*)>\s+-\s+(?P<rest>.*)$")
# Trailing " - Module=<X>" or, after a value, a bare " Module=<X>".
MODULE_SUFFIX = re.compile(r"\s+-?\s*Module=<(?P<module>[^>]*)>\s*$")
# Path component: AttributeName(gggg,eeee) with an optional [item] index.
PATH_COMPONENT = re.compile(
    r"^(?P<name>[A-Za-z0-9_]+)(?:\((?P<tag>[0-9a-fA-Fx,]+)\))?(?:\[(?P<item>\d+)\])?$"
)
VALUE_SUFFIX = re.compile(r"=\s*<(?P<value>[^>]*)>")
ANGLED = re.compile(r"<[^>]*>")

RANK = {"High": 0, "Medium": 1, "Low": 2}

FINDING_COLUMNS = [
    "PatientID", "StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID",
    "SegmentNumber", "SegmentLabel", "frameNumber", "dciodvfySeverity",
    "severity", "messageClass", "attributeName", "attributeTag", "module",
    "valueSeen", "attributePath", "message", "iod", "filePath", "viewer_url",
]
SUMMARY_COLUMNS = [
    "severity", "dciodvfySeverity", "messageClass", "attributeName",
    "attributeTag", "module", "messageSignature", "instances", "series",
    "segments", "exampleValue", "exampleSOPInstanceUID",
    "exampleSeriesInstanceUID", "viewer_url",
]


# ---------------------------------------------------------------------------
# Parsing. Stdlib only, no subprocess - this is the part under test.
# ---------------------------------------------------------------------------


def classify(message):
    for pattern, name in MESSAGE_CLASSES:
        if re.search(pattern, message):
            return name
    return "OTHER"


def signature(message):
    """The message with every <value> blanked, so counts aggregate by defect."""
    return ANGLED.sub("<>", message).strip()


def split_path(path):
    """`/SegmentSequence(0062,0002)[1]/SegmentLabel(0062,0005)` -> its parts.

    Returns (attributeName, attributeTag, itemIndexBySequenceName). The item
    indices are dciodvfy's, which are 1-based positions in the sequence and NOT
    SegmentNumbers - resolve_segment() turns one into the other.
    """
    name = tag = ""
    items = {}
    for part in path.strip("/").split("/"):
        match = PATH_COMPONENT.match(part)
        if not match:
            name, tag = part, ""
            continue
        name = match.group("name")
        tag = (match.group("tag") or "").replace("0x", "")
        if match.group("item"):
            items[name] = int(match.group("item"))
    return name, tag, items


def parse_output(text):
    """dciodvfy -new output -> (iod, [message dicts]).

    `iod` is the Information Object dciodvfy chose to validate against - the
    one bare line it prints, e.g. "Segmentation". Confirm it: if a batch is
    validated against the wrong IOD, every message below it is about the wrong
    rules. It is empty when dciodvfy could not identify the object at all.
    """
    iod = ""
    findings = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line or FILENAME_LINE.match(line):
            continue
        match = MESSAGE_LINE.match(line)
        if not match:
            # The only non-message line dciodvfy emits is the IOD name. Keep
            # the first; a second would mean output from two files got mixed.
            if not iod and not line.startswith(("Usage:", "\t", " ")):
                iod = line.strip()
            continue

        body = match.group("body")
        path = ""
        prefix = PATH_PREFIX.match(body)
        if prefix:
            path, body = prefix.group("path"), prefix.group("rest")

        module = ""
        module_match = MODULE_SUFFIX.search(body)
        if module_match:
            module = module_match.group("module")
            body = body[: module_match.start()].rstrip()

        values = VALUE_SUFFIX.findall(body)
        name, tag, items = split_path(path)
        message_class = classify(body)

        findings.append({
            "dciodvfySeverity": match.group("severity"),
            "severity": PROMOTED.get(
                message_class, SEVERITY_FROM_DCIODVFY[match.group("severity")]
            ),
            "messageClass": message_class,
            "attributeName": name,
            "attributeTag": tag,
            "module": module,
            "valueSeen": values[-1] if values else "",
            "attributePath": path,
            "message": body,
            "messageSignature": signature(body),
            "segmentItem": items.get("SegmentSequence", ""),
            "frameItem": items.get("PerFrameFunctionalGroupsSequence", ""),
        })
    return iod, findings


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------


def find_binary(explicit=None):
    binary = explicit or shutil.which("dciodvfy")
    if not binary:
        raise SystemExit(
            "dciodvfy not on PATH:\n  pip install dicom3tools\n"
            "or pass --dciodvfy /path/to/dciodvfy"
        )
    return binary


def tool_provenance(binary):
    """The line to quote in the report. Both terminology and validator
    versions belong there - see references/reporting.md."""
    try:
        from importlib.metadata import version

        return f"dicom3tools {version('dicom3tools')} ({binary})"
    except Exception:
        return f"dicom3tools, build unknown ({binary})"


def run_one(binary, path, timeout):
    """One invocation. dciodvfy takes a single file and writes to stderr."""
    try:
        proc = subprocess.run(
            [binary, "-new", "-filename", "-allpffgitems", str(path)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"dciodvfy timed out after {timeout}s"
    # Exit status is NOT a pass/fail signal: 1 means "errors OR the file could
    # not be read", 0 means "clean OR warnings only". The output is the signal.
    return (proc.stderr or "") + (proc.stdout or ""), None


def segment_index(dataset):
    """Item position (1-based, as dciodvfy prints it) -> (SegmentNumber, label).

    Not the identity map. A labelmap SEG carries a Background segment at
    SegmentNumber 0, so item 1 is segment 0 and every later index is off by
    one. Reading the actual SegmentNumber out of the item avoids guessing.
    """
    index = {}
    for position, segment in enumerate(getattr(dataset, "SegmentSequence", []) or [], 1):
        number = getattr(segment, "SegmentNumber", None)
        index[position] = (
            "" if number is None else str(number),
            str(getattr(segment, "SegmentLabel", "") or ""),
        )
    return index


def frame_index(dataset):
    """Frame position (1-based) -> ReferencedSegmentNumber, so a per-frame
    functional group error lands on a segment rather than just on a file."""
    index = {}
    frames = getattr(dataset, "PerFrameFunctionalGroupsSequence", []) or []
    for position, frame in enumerate(frames, 1):
        seq = getattr(frame, "SegmentIdentificationSequence", None)
        if seq:
            number = getattr(seq[0], "ReferencedSegmentNumber", None)
            if number is not None:
                index[position] = str(number)
    return index


def read_headers(directory):
    """Every SEG under `directory`, identified by SOPClassUID rather than by
    extension. Mirrors seg_attributes.read_files; the counts are a finding."""
    try:
        import pydicom
        from pydicom.errors import InvalidDicomError
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            f"reading DICOM needs pydicom:\n  pip install 'pydicom>=3.0'\n({exc})"
        ) from exc

    found, counts = [], Counter()
    for path in sorted(p for p in Path(directory).rglob("*") if p.is_file()):
        counts["files"] += 1
        try:
            dataset = pydicom.dcmread(str(path), stop_before_pixels=True)
        except (InvalidDicomError, OSError):
            counts["not_dicom"] += 1
            continue
        except Exception as exc:
            counts["unreadable"] += 1
            print(f"  ! unreadable: {path.name}: {exc}", file=sys.stderr)
            continue
        if str(getattr(dataset, "SOPClassUID", "") or "") not in SEG_SOP_CLASSES:
            counts["not_seg"] += 1
            continue
        counts["seg"] += 1
        found.append((path, dataset))
    return found, counts


def validate(binary, targets, viewer_url_pattern, timeout, workers):
    """Validate each SEG and attribute every message to a segment where the
    path allows it. Returns (finding rows, per-file rows)."""
    def work(item):
        path, dataset = item
        text, failure = run_one(binary, path, timeout)
        return path, dataset, text, failure

    rows, per_file = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for path, dataset, text, failure in pool.map(work, targets):
            series_uid = str(getattr(dataset, "SeriesInstanceUID", "") or "")
            viewer_url = (
                viewer_url_pattern.format(
                    StudyInstanceUID=str(getattr(dataset, "StudyInstanceUID", "") or ""),
                    SeriesInstanceUID=series_uid,
                )
                if viewer_url_pattern
                else ""
            )
            common = {
                "PatientID": str(getattr(dataset, "PatientID", "") or ""),
                "StudyInstanceUID": str(getattr(dataset, "StudyInstanceUID", "") or ""),
                "SeriesInstanceUID": series_uid,
                "SOPInstanceUID": str(getattr(dataset, "SOPInstanceUID", "") or ""),
                "filePath": str(path),
                "viewer_url": viewer_url,
            }

            if failure:
                row = dict(common, iod="", dciodvfySeverity="Error",
                           severity="Medium", messageClass="VALIDATION_FAILED",
                           attributeName="", attributeTag="", module="",
                           valueSeen="", attributePath="", message=failure,
                           messageSignature=failure, SegmentNumber="",
                           SegmentLabel="", frameNumber="")
                rows.append(row)
                per_file.append(dict(common, iod="", errors=1, warnings=0,
                                     status="FAILED"))
                continue

            iod, findings = parse_output(text)
            segments = segment_index(dataset)
            frames = frame_index(dataset)
            by_number = {num: lab for num, lab in segments.values() if num != ""}
            for finding in findings:
                number, label = "", ""
                if finding["segmentItem"]:
                    number, label = segments.get(finding["segmentItem"], ("", ""))
                elif finding["frameItem"]:
                    # A frame names its segment by ReferencedSegmentNumber. Use
                    # it only when a segment with that number actually exists -
                    # the dangling reference IS the finding in some messages,
                    # and echoing it into SegmentNumber would invent a segment.
                    referenced = frames.get(finding["frameItem"], "")
                    if referenced in by_number:
                        number, label = referenced, by_number[referenced]
                row = dict(common, **finding)
                row["frameNumber"] = row.pop("frameItem", "") or ""
                row.pop("segmentItem", None)
                row["SegmentNumber"] = number
                row["SegmentLabel"] = label
                row["iod"] = iod
                rows.append(row)
            counts = Counter(f["dciodvfySeverity"] for f in findings)
            per_file.append(dict(common, iod=iod, errors=counts["Error"],
                                 warnings=counts["Warning"],
                                 status="OK" if not counts["Error"] else "ERRORS"))
    return rows, per_file


def summarise(rows):
    groups = defaultdict(lambda: {"instances": set(), "series": set(),
                                  "segments": set(), "example": None})
    for row in rows:
        key = (row["severity"], row["dciodvfySeverity"], row["messageClass"],
               row["attributeName"], row["attributeTag"], row["module"],
               row["messageSignature"])
        group = groups[key]
        group["instances"].add(row["SOPInstanceUID"])
        group["series"].add(row["SeriesInstanceUID"])
        if row["SegmentNumber"] != "":
            group["segments"].add((row["SOPInstanceUID"], row["SegmentNumber"]))
        if group["example"] is None:
            group["example"] = row

    out = []
    for key, group in groups.items():
        example = group["example"]
        out.append({
            "severity": key[0], "dciodvfySeverity": key[1], "messageClass": key[2],
            "attributeName": key[3], "attributeTag": key[4], "module": key[5],
            "messageSignature": key[6],
            "instances": len(group["instances"]),
            "series": len(group["series"]),
            "segments": len(group["segments"]),
            "exampleValue": example["valueSeen"],
            "exampleSOPInstanceUID": example["SOPInstanceUID"],
            "exampleSeriesInstanceUID": example["SeriesInstanceUID"],
            "viewer_url": example["viewer_url"],
        })
    out.sort(key=lambda r: (RANK[r["severity"]], -r["instances"], r["messageClass"]))
    return out


def write(outdir, name, rows, columns):
    path = Path(outdir) / name
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Validate a SEG delivery against its IOD with dciodvfy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--files", required=True, help="directory of DICOM files")
    parser.add_argument("-o", "--outdir", default="findings")
    parser.add_argument("--dciodvfy", help="path to the binary if not on PATH")
    parser.add_argument(
        "--viewer-url",
        help="pattern with {StudyInstanceUID} / {SeriesInstanceUID}. Findings "
        "without a clickable example are markedly less useful.",
    )
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="per file, seconds (default 300)")
    parser.add_argument("--workers", type=int, default=min(8, (os.cpu_count() or 2)),
                        help="parallel dciodvfy invocations")
    parser.add_argument("--limit", type=int, help="validate only the first N objects")
    args = parser.parse_args()

    binary = find_binary(args.dciodvfy)
    targets, counts = read_headers(args.files)
    if not targets:
        sys.exit(f"no segmentation objects under {args.files} "
                 f"({counts['files']} files, {counts['not_seg']} not SEG)")
    if args.limit:
        targets = targets[: args.limit]

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    print(tool_provenance(binary))
    print(f"{counts['files']} files, {counts['seg']} segmentation objects, "
          f"{counts['not_seg']} not SEG, {counts['not_dicom']} not DICOM, "
          f"{counts['unreadable']} unreadable")
    print(f"validating {len(targets)} objects with -new -allpffgitems "
          f"({args.workers} workers)")

    rows, per_file = validate(binary, targets, args.viewer_url,
                              args.timeout, args.workers)
    summary = summarise(rows)

    findings_path = write(outdir, "issue9_iod_validation.csv", rows, FINDING_COLUMNS)
    write(outdir, "issue9_iod_validation_summary.csv", summary, SUMMARY_COLUMNS)
    write(outdir, "issue9_iod_validation_files.csv", per_file,
          ["PatientID", "StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID",
           "iod", "status", "errors", "warnings", "filePath", "viewer_url"])

    iods = Counter(f["iod"] for f in per_file)
    clean = sum(1 for f in per_file if f["status"] == "OK" and not f["warnings"])
    print(f"\n{len(rows)} messages over {len(targets)} objects -> {findings_path}")
    print(f"  IOD validated against: "
          + ", ".join(f"{name or '(unidentified)'} x{n}" for name, n in iods.most_common()))
    print(f"  {clean} objects with nothing reported, "
          f"{sum(1 for f in per_file if f['errors'])} with at least one Error")
    for severity in ("Medium", "Low"):
        classes = Counter(r["messageClass"] for r in rows if r["severity"] == severity)
        if not classes:
            continue
        print(f"  {severity}:")
        for name, count in classes.most_common():
            print(f"    {name:<28} {count:>7} messages")
    if any(r["messageClass"] == "OTHER" for r in rows):
        print("  ! some messages did not match a known class - read them in the CSV "
              "before\n    quoting any count as complete")
    print("\nFold into the triage list:\n"
          f"  python scripts/seg_checks.py seg_attributes.csv --iod {findings_path} "
          "--outdir findings/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
