# Access path: BigQuery metadata table

The right path whenever it is available: the checks are set-based, so a whole delivery
is one query rather than one request per series.

## What counts as a source table

Two flavours, both of which the templates in `scripts/sql/` handle:

| Source | Example | Notes |
|---|---|---|
| **Google Healthcare API metadata export** of a DICOM store | `<project>.<dataset>.dicom_metadata` | Column exists only if some instance populates the attribute — see below |
| **IDC public metadata** | `bigquery-public-data.idc_current.dicom_all` | Carries `collection_id`; clustered on it |

The export schema is the DICOM keyword tree: sequences are `ARRAY<STRUCT>`, so
`SegmentSequence` is unnested and code sequences are indexed.

## Check the schema before writing a single query

**A Healthcare API export creates a column only for attributes some instance in the
store populates.** Referencing an absent attribute is a **compile error**, not a NULL
column — so a query that works against one store fails against the next.

```bash
bq query --use_legacy_sql=false --project_id=<project> "
SELECT field_path, data_type
FROM \`<project>.<dataset>.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS\`
WHERE table_name = '<table>'
  AND (field_path LIKE '%Segment%' OR field_path LIKE '%AnatomicRegion%'
       OR field_path LIKE '%Tracking%')
ORDER BY field_path"
```

Attributes commonly absent, and what their absence means:

| Attribute | Meaning if absent |
|---|---|
| `TrackingID` / `TrackingUID` (0062,0020/0021) | No instance tracks findings — issue 7 does not apply |
| `AnatomicRegionModifierSequence` (0008,2220) | Laterality is pre-coordinated — issue 3's root cause |
| `SegmentAlgorithmName` (0062,0009) | No algorithm identification recorded |

**An absent attribute is itself a finding.** Report it rather than working around it
silently.

Re-run this check after every new ingest: a later delivery can add a column, and a
re-delivery can remove one.

## Build the per-segment view

`scripts/sql/01_seg_attributes.sql` is the template. Substitute the source table and
the viewer URL pattern, then deploy it as a view so every check reads one definition:

```bash
sed -e 's|@@SOURCE_TABLE@@|<project>.<dataset>.dicom_metadata|' \
    -e 's|@@IMAGE_TABLE@@|<project>.<dataset>.dicom_metadata|' \
    -e 's|@@VIEWER_BASE@@|https://viewer.example.org/...|' \
    scripts/sql/01_seg_attributes.sql > /tmp/seg_attributes.sql

bq mk --project_id=<project> --use_legacy_sql=false \
  --view "$(cat /tmp/seg_attributes.sql)" <dataset>.seg_attributes
# replace `mk` with `update` once it exists
```

`@@SOURCE_TABLE@@` and `@@IMAGE_TABLE@@` are separate on purpose — see "When the
segmented images are elsewhere" below.

Deploy **from files in version control, never by editing a view in the console** — a
console edit leaves no history, and a view that exists nowhere else has to be recovered
from whatever backup happens to hold it.

### Things the template already handles, and why

- **`SOPClassUID` filter, not `Modality = 'SEG'`** — accepts `1.2.840.10008.5.1.4.1.1.66.4`
  (Segmentation Storage) and `...66.7` (Labelmap Segmentation Storage).
- **`SAFE_OFFSET(0)` on every code sequence**, plus a `multiValuedCodeSequence` boolean
  that is TRUE when any sequence holds more than one item. That column is the guard on
  the assumption every other check makes — verify it is FALSE everywhere before
  trusting any result.
- **`isBackgroundSegment`** — `SegmentNumber = 0`. Labelmap objects all carry one.
- **Backfilling the referenced series' `Modality` / `BodyPartExamined`** — only some
  producers copy them into `ReferencedSeriesSequence`, so join the image series and
  `COALESCE`.
- **`referencedFrameOfReferenceUID` and `referencedSeriesFound`** from the same
  join, which is what decides issue 17. This path always resolves the references;
  the file and DICOMweb extractors need `--resolve-referenced`.
- **`Collection`**, NULL by default so the table has the same shape as the other
  paths; on IDC, select `collection_id` into it.
- **`viewer_url`** on every row, so examples in the report are clickable.
- **`# description:` comment blocks** above each output column, per the
  [idc-index-data](https://github.com/ImagingDataCommons/idc-index-data) convention,
  and `#` comments rather than `--` throughout (BigQuery standardSQL house style).

### When the segmented images are elsewhere

Two store shapes, and they need different substitutions:

| Store holds | `@@SOURCE_TABLE@@` | `@@IMAGE_TABLE@@` |
|---|---|---|
| Images **and** segmentations | the table | the same table |
| **Only** segmentations | the table | wherever the images live, e.g. `bigquery-public-data.idc_current.dicom_all` |

A segmentation-only store needs one more edit. Its `ReferencedSeriesSequence` often
carries nothing but `SeriesInstanceUID`, where a store holding the images alongside
also copies `Modality` and `BodyPartExamined` into it:

```
images + segmentations            segmentations only
  ReferencedSeriesSequence          ReferencedSeriesSequence
    .SeriesInstanceUID                .SeriesInstanceUID
    .Modality                         .ReferencedInstanceSequence...
    .BodyPartExamined
    .SeriesDescription
    .Manufacturer ...
```

So on a segmentation-only store, drop the `COALESCE` fallback on `referencedModality`
and `referencedBodyPartExamined` and keep `imageSeries.<attribute>` alone — the
template says so at each column. Leaving it in is a compile error naming `Modality`,
which is confusing because the table *does* have a top-level `Modality` column; the
one that does not exist is the field inside the sequence.

Cost: joining `idc_current.dicom_all` scans ~7 GB because it is clustered on
`collection_id` and so cannot be pruned by `SeriesInstanceUID` — an `IN` list restricts
the *result*, not the scan. If that matters, materialise the `imageSeries` CTE into a
small table once and point the view at it.

## Run the checks

`scripts/sql/` is numbered in run order. Each reads the `seg_attributes` view:

```bash
for q in scripts/sql/0[2-9]*.sql scripts/sql/1*.sql; do
  sed -e 's|@@SEG_ATTRIBUTES@@|<project>.<dataset>.seg_attributes|' \
      -e 's|@@DELTA_E@@|10|' "$q" \
    | bq query --use_legacy_sql=false --project_id=<project>
done
```

`@@DELTA_E@@` is issue 13's confusability threshold in ΔE\*ab, and `14` and `12` must
be given the **same** value or the two will disagree about which series are tagged. 10
matches `seg_checks.py`'s default; the reasoning is in `references/issue-catalogue.md`,
issue 13. Whichever you use, name it in the report — it is a choice, not a fact.

| File | Check | Layer |
|---|---|---|
| `01_seg_attributes.sql` | the per-segment view | — |
| `02_ambiguous_code.sql` | issue 2 — **run first** | computed |
| `03_codes_not_in_dcmterm.sql` | terminology coverage gap | computed |
| `04_code_review_template.sql` | the curated verdict table | curated |
| `05_anatomy_conflict.sql` | issue 1 | curated |
| `06_laterality.sql` | issue 3 | curated |
| `07_entire_code_flavour.sql` | issue 8 | computed + lookup |
| `08_malformed_segment.sql` | issue 4 | computed |
| `09_type_repeats_category.sql` | issue 5 | computed |
| `10_segments_overlap_absent.sql` | issue 6 | computed |
| `11_tracking_uid.sql` | issue 7 | computed |
| `12_series_triage.sql` | per-series roll-up | both |
| `13_retired_coding_scheme.sql` | issue 10 | computed |
| `14_recommended_color.sql` | issue 13 | computed |
| `15_algorithm_identification.sql` | issue 14 | computed |
| `16_segment_numbers.sql` | issue 16 | computed |
| `17_frame_of_reference.sql` | issue 17 | computed |
| `18_geometry.sql` | issue 18 | computed |

File numbers are **not** issue numbers — they are the order the files were added, and
issues are numbered separately so that a report can cite an issue for good. The
numbering after `12` looks odd for that reason: the roll-up was written before issues
10, 13, 14, 16 and 17 existed. It does not read the other files' output, so their
position relative to it does not matter; `12` recomputes each tag from the per-segment
view, and each numbered file is the place the definition is *documented*.

**`02`, `03`, `05`, `06`, `07` and `12` judge three code sequences**, not the anatomic
region alone. Each opens with the same `codes` CTE — one row per (segment, sequence)
over the region, the property type and the property category — and their outputs
carry a `codeSequence` column. Keep that CTE identical across the six files. The
review table has a `codeSequence` column too; NULL applies the verdict wherever the
pairing appears.

**Issue 15 has no SQL form.** Membership in CID 7151 is a transitive closure over
dcmterms' Include-CID edges, and the category → type link is a column of CID 7150;
both live in Parquet tables `dcmterm.py` reads. Export the per-segment view to CSV
and run `scripts/dcmterm.py property` on it, then fold the result in with
`seg_checks.py --property`.

**Exporting to CSV changes booleans to `true` / `false`.** The scripts accept
either case; a hand-written join against `isBackgroundSegment = 'True'` will not.

Deploy `04_code_review_template.sql` as a **view of its own** and have `05`, `06` and
`12` join it. Editing a verdict then means editing one file, not three. The alternative
— inline code lists in each query — drifts out of sync almost immediately.

### The one table that is not yours: DICOM's code set

`03` joins `@@DCMTERM_TABLE@@`, the codes DICOM's own context groups use. It is public
— [fedorov/dcmterms](https://github.com/fedorov/dcmterms) publishes it as Parquet — so
load a copy rather than hunting for someone's private table:

```bash
curl -LO https://raw.githubusercontent.com/fedorov/dcmterms/main/docs/data/codes_unique.parquet
bq load --project_id=<project> --source_format=PARQUET \
  <dataset>.dcmterm_codes_unique codes_unique.parquet
```

Join on **`SELECT DISTINCT`** pairs, as the template does: the table is deduplicated on
the meaning as well as the code, so `21974007` appears as both "Tongue" and "tongue".

Reload it when a new DICOM edition lands, and record the edition from
`extraction_metadata.json` in the report — "not in DICOM's code set" is a claim about
one edition.

The alternative is to skip `03` in BigQuery entirely and run `scripts/dcmterm.py
coverage` over the exported per-segment CSV. It reads the Parquet directly, needs no
load step, and also reports codes that *are* in the set but carry a meaning DICOM does
not use for them — a computed issue-1 signal the SQL does not produce.

## Cost control while developing

Add a collection or patient restriction to the source CTE:

```sql
AND collection_id = 'some_collection'   # IDC
AND PatientID = '<one-patient>'         # a single-patient smoke test
```

Remove it before producing numbers for the report, and dry-run anything unfamiliar:

```bash
bq query --use_legacy_sql=false --dry_run < query.sql
```

## BigQuery notes that bite in these queries

- `ARRAY_AGG` needs an explicit `ORDER BY` to guarantee element order.
- `NOT IN` with a subquery is not NULL-safe; prefer `NOT EXISTS` where the subquery
  column can be NULL.
- `MAX`/`MIN` already ignore NULLs — do not add `IGNORE NULLS`.
- Table names cannot be parameterised with `DECLARE`; substitute the text, which is
  why these templates use `@@PLACEHOLDER@@` markers.
- Single quotes inside SQL passed inline through a shell break; pipe from a file
  (`bq query < file.sql`) instead.

## Exporting for the report

```bash
bq query --use_legacy_sql=false --format=csv --max_rows=100000 \
  < 12_series_triage.sql > seg-issues-by-series.csv
```

The CSV in version control is the version of record. If you also publish a spreadsheet
copy for people who will not run a query, say in the report that it is a snapshot and
has to be re-imported when the query is re-run.

## Issues 9, 11 and 12 are not available here

`dciodvfy` validates a Part 10 object against the Segmentation IOD. A metadata table
is not an object, so there is no SQL form of issue 9 and `sql/12_series_triage.sql`
emits no `IOD_ERROR` / `IOD_WARNING` tag. A triage list built entirely on BigQuery is
**silent** about IOD conformance, not clean.

`sql/08_malformed_segment.sql` still runs and covers the five Type 1 attributes of the
Segment Description Macro. That is a small subset of what the validator checks, and
the report should say so where it quotes issue 4.

Issues 11 and 12 are unavailable for related reasons, and both are worth stating
explicitly rather than leaving as a gap in the summary table:

- **Issue 11** needs the voxels. No metadata export carries pixel data, so nothing
  here can tell an object that omitted its empty frames from one that kept several
  hundred. `numberOfFrames` in the view is a claim, not a measurement — comparing it
  against `referencedInstanceCount` hints at coverage, but an object with a frame per
  source slice may still be almost entirely zeros.
- **Issue 12** needs the file meta group. `TransferSyntaxUID` (0002,0010) is not part
  of the data set a Healthcare API export flattens, so `sql/01` fills the column with
  NULL to keep the per-segment table the same shape on all three paths. Do not read
  that NULL as "uncompressed". If your source happens to carry the transfer syntax,
  substitute it in `sql/01` and say so.

If the objects are reachable as files anywhere — the bucket the store was loaded from,
or a WADO-RS retrieval of a sample — run `scripts/dciodvfy_check.py` and
`scripts/seg_encoding.py` there and fold the results in with `seg_checks.py --iod`
and `--encoding`. Issue 10, the retired coding scheme designators dciodvfy also
reports, *is* computable here: `sql/13_retired_coding_scheme.sql`.

**Issue 18 is at its strongest here.** `sql/18_geometry.sql` reads the per-segment
view and joins `@@IMAGE_TABLE@@` for the segmented series' grid; point that at
`bigquery-public-data.idc_current.dicom_all` and "is the segmented series available in
IDC" *is* the join. `dicom_all` carries `ImageOrientationPatient` per instance, so the
orientation comparison — the one `seg_geometry.py` needs `--probe` or `--files` for —
comes for free. Substitute the same `@@SPACING_TOLERANCE@@` the script was given, or
the two will disagree about which series are tagged.

The one comparison the SQL cannot make is the **measured** slice spacing: that means
projecting every instance's `ImagePositionPatient` onto the slice normal and taking the
median gap, which is trigonometry over arrays for little gain here. `sql/18` uses the
source's declared `SpacingBetweenSlices` where there is one and is silent otherwise.
Run `seg_geometry.py --probe full` on a sample if the spacing matters.

`sql/01` gains two things for issue 18, and both are worth checking before deploying.
The geometry columns read the Pixel Measures and Plane Orientation macros from the
**Shared** Functional Groups, falling back to a `frameGeometry` CTE that unnests
`PerFrameFunctionalGroupsSequence` — delete that CTE if the scan cost is not worth it,
and `geometrySource` will say which objects went unexamined. And
`sourceImageReferenceLevel` reads the two *top-level* instance-reference sequences
only, so an object whose only link is the per-frame Derivation Image group reads `NONE`
here and `INSTANCE_ONLY` from `seg_attributes.py`. Extract with the script where that
distinction matters.

Issues 13 and 14 are fully available: `sql/14_recommended_color.sql` and
`sql/15_algorithm_identification.sql`, both reading the per-segment view. The one
part of issue 13 that is script-only is the **swatch** — rendering CIELab as sRGB for
the report — which `seg_checks.py` writes from the same numbers.
