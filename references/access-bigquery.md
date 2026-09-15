# Access path: BigQuery metadata table

The proven path, and the right one whenever it is available: the checks are set-based,
so a whole delivery is one query rather than one request per series.

## What counts as a source table

Two flavours, both of which the templates in `scripts/sql/` handle:

| Source | Example | Notes |
|---|---|---|
| **Google Healthcare API metadata export** of a DICOM store | `idc-external-040.apollo5.dicom_metadata` | Column exists only if some instance populates the attribute — see below |
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

Attributes that were absent in real batches, and what their absence means:

| Attribute | Absent from | Meaning |
|---|---|---|
| `TrackingID` / `TrackingUID` (0062,0020/0021) | the AI batch | No instance tracks findings — issue 7 does not apply |
| `AnatomicRegionModifierSequence` (0008,2220) | the manual batch | Laterality is pre-coordinated — issue 3's root cause |
| `SegmentAlgorithmName` (0062,0009) | the manual batch | No algorithm identification recorded |

**An absent attribute is itself a finding.** Report it rather than working around it
silently.

Re-run this check after every new ingest: a later delivery can add a column, and a
re-delivery can remove one.

## Build the per-segment view

`scripts/sql/01_seg_attributes.sql` is the template. Substitute the source table and
the viewer URL pattern, then deploy it as a view so every check reads one definition:

```bash
sed -e 's|@@SOURCE_TABLE@@|idc-external-040.apollo5.dicom_metadata|' \
    -e 's|@@IMAGE_TABLE@@|idc-external-040.apollo5.dicom_metadata|' \
    -e 's|@@VIEWER_BASE@@|https://viewer.example.org/...|' \
    scripts/sql/01_seg_attributes.sql > /tmp/seg_attributes.sql

bq mk --project_id=<project> --use_legacy_sql=false \
  --view "$(cat /tmp/seg_attributes.sql)" <dataset>.seg_attributes
# replace `mk` with `update` once it exists
```

`@@SOURCE_TABLE@@` and `@@IMAGE_TABLE@@` are separate on purpose — see "When the
segmented images are elsewhere" below. Both templates were dry-run against a real
store of each shape.

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

A segmentation-only store needs one more edit. Its `ReferencedSeriesSequence` carries
nothing but `SeriesInstanceUID`, where a store written by highdicom also copies
`Modality` and `BodyPartExamined` into it. Verified on two real stores:

```
apollo5.dicom_metadata            IDC segmentation store
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
  sed 's|@@SEG_ATTRIBUTES@@|<project>.<dataset>.seg_attributes|' "$q" \
    | bq query --use_legacy_sql=false --project_id=<project>
done
```

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

Deploy `04_code_review_template.sql` as a **view of its own** and have `05`, `06` and
`12` join it. Editing a verdict then means editing one file, not three. The alternative
— inline code lists in each query — was tried first and drifted immediately.

## Cost control while developing

Add a collection or patient restriction to the source CTE:

```sql
AND collection_id = 'some_collection'   # IDC
AND PatientID = 'AP-1234'               # a single-patient smoke test
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
