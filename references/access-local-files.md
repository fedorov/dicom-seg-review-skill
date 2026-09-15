# Access path: local DICOM files

Use when you have the objects on disk and no BigQuery table or DICOMweb endpoint —
a vendor delivery on a drive, a batch pulled from a bucket, output of a segmentation
pipeline before it is ingested anywhere.

`scripts/seg_attributes.py --files <dir>` implements this; `pydicom` is its only
dependency. `scripts/dciodvfy_check.py` adds the IOD validator, which needs
`dicom3tools` as well.

```bash
pip install 'pydicom>=3.0' dicom3tools
python scripts/seg_attributes.py --files /path/to/delivery -o seg_attributes.csv
python scripts/dciodvfy_check.py --files /path/to/delivery -o findings/
python scripts/seg_checks.py seg_attributes.csv \
    --iod findings/issue9_iod_validation.csv --outdir findings/
```

**This is the only path that can run issue 9.** `dciodvfy` validates an object
against the Segmentation IOD, and neither a BigQuery row nor a DICOMweb metadata
response is an object. If the delivery is reachable as files at any point — before
ingestion, or by retrieving a sample afterwards — run it here, because nothing
downstream can.

## Reading only what is needed

Two things make this fast enough to run over a whole delivery:

- **`stop_before_pixels=True`.** A segmentation's metadata is kilobytes; its pixel data
  can be hundreds of megabytes, and the extraction interprets no voxel. (`dciodvfy`
  does read `PixelData`, to check its length — see issue 9. That is a separate pass
  over the files and it cannot be made cheaper.)
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
