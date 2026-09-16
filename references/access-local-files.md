# Access path: local DICOM files

Use when you have the objects on disk and no BigQuery table or DICOMweb endpoint —
a vendor delivery on a drive, a batch pulled from a bucket, output of a segmentation
pipeline before it is ingested anywhere.

`scripts/seg_attributes.py --files <dir>` implements this; `pydicom` is its only
dependency. `scripts/dciodvfy_check.py` adds the IOD validator, which needs
`dicom3tools` as well, and `scripts/seg_encoding.py` adds the two checks that read
the pixel data and the file meta group.

```bash
pip install 'pydicom>=3.0' dicom3tools
python scripts/seg_attributes.py --files /path/to/delivery -o seg_attributes.csv
python scripts/dciodvfy_check.py --files /path/to/delivery -o findings/
python scripts/seg_encoding.py   --files /path/to/delivery -o findings/
python scripts/seg_checks.py seg_attributes.csv \
    --iod findings/issue9_iod_validation.csv \
    --encoding findings/encoding_per_object.csv --outdir findings/
```

**This is the only path that can run issues 9, 11 and 12.** `dciodvfy` validates an
object against the Segmentation IOD; the empty-frame check needs the voxels; the
compression check needs `TransferSyntaxUID` (0002,0010), which lives in the file meta
group. Neither a BigQuery row nor a DICOMweb metadata response carries any of the
three. If the delivery is reachable as files at any point — before ingestion, or by
retrieving a sample afterwards — run them here, because nothing downstream can.

## Reading only what is needed

Two things make this fast enough to run over a whole delivery:

- **`stop_before_pixels=True`.** A segmentation's metadata is kilobytes; its pixel data
  can be hundreds of megabytes, and the extraction interprets no voxel. Two separate
  passes do read it, and neither can be made cheaper: `dciodvfy` checks `PixelData`'s
  length against the declared geometry (issue 9), and `seg_encoding.py` tests each
  frame for emptiness (issue 11). Run them as their own step, not inside the extract.
- **`specific_tags`** is *not* used, deliberately. It does not descend into sequences,
  so it cannot fetch `SegmentSequence` selectively, and the saving over
  `stop_before_pixels` alone is negligible.

The directory is walked recursively. Files are identified by reading the header, not by
extension — vendor deliveries routinely ship DICOM with no extension, or with `.dcm`
on things that are not DICOM. Anything that is not a Part 10 file, or whose
`SOPClassUID` is not a segmentation storage class, is counted and skipped; the counts
are printed at the end. **Read them.** A delivery where 40% of files were skipped is
itself the finding.

## SOP classes accepted

| UID | Name |
|---|---|
| `1.2.840.10008.5.1.4.1.1.66.4` | Segmentation Storage |
| `1.2.840.10008.5.1.4.1.1.66.7` | Labelmap Segmentation Storage |

Filter on `SOPClassUID` (0008,0016), not `Modality`. Both are usually right, but
`Modality` is free enough that producers get it wrong, and the SOP class is what
determines how the object is encoded.

## Absent versus empty

Issue 4 turns on the distinction, and `pydicom` blurs it if you let it:

```python
ds.get("SegmentLabel")            # None whether absent OR present-and-empty
"SegmentLabel" in ds              # False only if genuinely absent
ds["SegmentLabel"].is_empty       # True for a zero-length Type 2 value
```

The extractor writes an empty CSV field for both, and the conformance check treats
both as a Type 1 violation — which is correct, since Type 1 requires a *value*, not
merely the attribute. Where you need to tell them apart for a bug report, go back to
the object.

## Validating against the IOD

`scripts/dciodvfy_check.py` invokes `dciodvfy` once per object and normalises the
output into the per-segment shape everything else uses. Detail, flags and traps are in
`references/issue-catalogue.md`, issue 9; the essentials for this path:

- `pip install dicom3tools` provides the binary. The script finds it on `PATH`, or
  takes `--dciodvfy /path/to/dciodvfy`.
- It always passes `-new -allpffgitems -filename`. `-new` gives the attribute path
  that lets a message be attributed to a segment; `-allpffgitems` is what makes
  per-frame defects visible at all, since the default checks only frame 1.
- `-allpffgitems` costs roughly 10× — about 4 ms per per-frame item, so ~4 s for a
  1000-frame object. `--workers` parallelises across files; `--limit N` validates a
  sample first so you can size the full run.
- Pass `--viewer-url` here too, with the same pattern used for the extract.

Three CSVs come out: `issue9_iod_validation.csv` (one row per message),
`issue9_iod_validation_summary.csv` (one row per distinct defect — this is what the
report quotes) and `issue9_iod_validation_files.csv` (one row per object, including
any that failed to validate at all).

Read the printed `IOD validated against:` line. It should be `Segmentation` for every
object; anything else means dciodvfy applied different rules to part of the batch and
nothing below it can be quoted until that is explained.

## Empty frames and compression

`scripts/seg_encoding.py` is the second pass over the files, and the only one that
looks at the voxels for their own sake. Detail and the DICOM references are in
`references/issue-catalogue.md`, issues 11 and 12; the essentials for this path:

- **It needs no pixel-data codec and no numpy.** A frame is empty when every bit of
  its range is zero, which is an integer mask over a slice of `PixelData`. Native
  encodings are read directly; Deflated Image Frame Compression
  (`1.2.840.10008.1.2.8.1`) is un-deflated here, fragment by fragment, because
  pydicom 3.0.1 does not recognise that UID at all. RLE and the JPEG family are
  reported as `NOT_DECODED` rather than guessed at — read the decode-status counts
  before quoting an empty-frame number as complete.
- **`--workers` parallelises across files**, `--limit N` scans a sample first.
- Pass `--viewer-url` here too, with the same pattern used for the extract.

Four CSVs come out: `encoding_per_object.csv` (the join contract, one row per
object), `issue11_empty_frames.csv` (objects that kept all-zero frames, worst first),
`issue11_empty_segments.csv` (segments with no voxels anywhere) and
`issue12_compression.csv` (one row per transfer syntax, with the **measured** deflated
size beside the delivered one — that number is the argument a producer can act on).

Read the printed decode-status block. An object whose pixel data could not be read is
not an object with no empty frames.

## Colour and algorithm identification

Issues 13 and 14 need nothing extra here: `seg_attributes.py` extracts
`RecommendedDisplayCIELabValue`, `SegmentAlgorithmName` and the whole
`SegmentationAlgorithmIdentificationSequence`, and `seg_checks.py` runs both checks on
every invocation. They run identically on all three access paths.

The one local-files detail: `PhotometricInterpretation` is extracted because issue 13
needs it. PS3.3 C.8.20.2 forbids `RecommendedDisplayCIELabValue` on a LABELMAP object
whose Photometric Interpretation is PALETTE COLOR, and that rule cannot be evaluated
without it.

## What is missing compared to the other paths

- **Collection / project membership.** Not in the object unless `ClinicalTrial*`
  attributes are populated. Supply it with `--collection <name>` if the whole delivery
  is one collection, or join it on afterwards.
- **The referenced image series' `Modality` and `BodyPartExamined`**, unless the
  producer copied them into `ReferencedSeriesSequence`. If the referenced image series
  are in the same directory tree, `--resolve-referenced` reads one instance of each to
  fill them in.
- **A viewer URL**, unless you pass `--viewer-url` with a pattern. Findings without
  clickable examples are markedly less useful to whoever has to act on them — supply
  the pattern for wherever the data will end up, even if it is not ingested yet.

## Sanity checks before trusting the extract

After extraction, confirm against the delivery manifest, and against the objects
themselves:

```bash
# objects found vs files on disk
find /path/to/delivery -type f | wc -l

# rows, objects, series, and how many segments are Background
python - <<'EOF'
import csv, collections
rows = list(csv.DictReader(open("seg_attributes.csv")))
print(len(rows), "segments")
for k in ("SOPInstanceUID", "SeriesInstanceUID", "StudyInstanceUID", "PatientID"):
    print(len({r[k] for r in rows}), "distinct", k)
print(collections.Counter(r["isBackgroundSegment"] for r in rows))
print(collections.Counter(r["multiValuedCodeSequence"] for r in rows))
EOF
```

`multiValuedCodeSequence` must be `False` throughout. If it is not, some segment
carries more than one code per sequence, only the first was extracted, and every check
downstream understates. Handle those segments by hand.
