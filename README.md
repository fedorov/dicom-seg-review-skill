# DICOM SEG Review

A [Claude Code](https://claude.com/claude-code) skill for auditing the **metadata** of
DICOM Segmentation (SEG) objects — not the pixels.

It finds the defects that make a segmentation delivery unusable downstream: a segment
labelled "Large bowel" whose code denotes the liver, one code carrying five different
meanings, a left structure coded as the right one, a segment missing attributes DICOM
makes Type 1.

## Install

```bash
git clone <this repo> ~/github/dicom-seg-review-skill
ln -s ~/github/dicom-seg-review-skill ~/.claude/skills/dicom-seg-review
```

Claude Code then loads it automatically when a task is about reviewing or QC'ing DICOM
segmentations. Nothing else is needed for the guidance; the scripts have optional
dependencies (below).

## What it produces

1. **A severity-ranked issue report** — what is wrong, how much of the batch, worked
   examples with viewer links, and which findings a machine can reproduce.
2. **A per-series triage CSV** — one row per SEG series, tagged, sorted worst-first, so
   it can be handed to whoever fixes the annotations.

## Three access paths, one table

Whether the metadata is in BigQuery, a DICOMweb store, or a directory of files, every
path produces the **same per-segment table**, and every check runs on that.

```
BigQuery metadata table ─┐
DICOMweb store ──────────┼──> seg_attributes (one row per segment) ──> 8 checks ──> report
Directory of .dcm files ─┘                                                      └─> triage CSV
```

## Layout

```
SKILL.md                             the method: workflow, severity scale, traps
references/
  issue-catalogue.md                 the eight checks, with DICOM references
  terminology.md                     judging codes; SNOMED flavours; laterality
  reporting.md                       writing the report and the triage list
  access-bigquery.md                 path 1 — metadata table
  access-dicomweb.md                 path 2 — QIDO + WADO
  access-local-files.md              path 3 — pydicom over a directory
scripts/
  seg_attributes.py                  extract the per-segment table (files | DICOMweb)
  seg_checks.py                      run the computed checks + triage roll-up
  dcmterm.py                         check codes against the code set DICOM uses
  lookup_codes.py                    resolve codes to fully specified names
  sql/                               the same checks as BigQuery templates, 01–12
tests/
  test_seg_review.py                 43 tests over the extraction and check logic
```

## Usage

```bash
# 1. Extract (pick a source)
python scripts/seg_attributes.py --files /path/to/delivery -o seg_attributes.csv
python scripts/seg_attributes.py --dicomweb <url-or-store> --gcp -o seg_attributes.csv

# 2. Computed checks + per-series triage
python scripts/seg_checks.py seg_attributes.csv --outdir findings/

# 3. Coverage gap + meaning disagreements against DICOM's own code set
python scripts/dcmterm.py coverage seg_attributes.csv -o findings/coverage.csv

# 4. Fully specified names for every anatomic code, and "Entire X" detection
python scripts/lookup_codes.py seg_attributes.csv -o findings/codes.csv
python scripts/dcmterm.py suggest findings/codes.csv -o findings/entire_flavour.csv

# 5. Re-run with the curated verdicts folded in
python scripts/seg_checks.py seg_attributes.csv --codes findings/codes.csv \
    --review review.csv --outdir findings/
```

For BigQuery, deploy `scripts/sql/01_seg_attributes.sql` as a view and run `02`–`12`
against it. See `references/access-bigquery.md`.

## Dependencies

| | |
|---|---|
| `seg_checks.py`, `lookup_codes.py` | standard library only |
| `dcmterm.py` | any one of `pyarrow`, `duckdb` or `pandas`, to read Parquet |
| `seg_attributes.py --files` | `pydicom>=3.0` |
| `seg_attributes.py --dicomweb` | `dicomweb-client>=0.59`, plus `[gcp]` and `google-auth` for Healthcare API stores |
| `scripts/sql/` | the `bq` CLI |
| `lookup_codes.py` | network access to `tx.fhir.org` (public, no auth) |
| `dcmterm.py` | one download from [fedorov/dcmterms](https://github.com/fedorov/dcmterms) (public, ~0.5 MB, cached) |

## Terminology sources

Both are public and need no credentials:

| | |
|---|---|
| [**dcmterms**](https://github.com/fedorov/dcmterms) | every coded entry in DICOM PS3.16's context groups, as Parquet — what DICOM *expects*, and the evidence behind the "Entire X" finding |
| [**tx.fhir.org**](https://tx.fhir.org) | all of SNOMED CT — fully specified names, retired concepts, everything dcmterms does not cover |

Neither one alone is enough, and a review that joins only the first **silently passes
everything it does not cover**, which can be most of a batch. See "The coverage trap"
in `SKILL.md`.

## Verification

`python tests/test_seg_review.py` — 43 tests covering extraction (Background segments,
multi-valued code sequences, both laterality modifier sequences, absent attributes),
the check logic (ambiguity scope classification, Type 1 conformance, TrackingUID
sharing patterns, triage severity and ordering) and the terminology comparison
(meaning agreement, the private-scheme and coverage-gap split, "Entire X" replacement
matching). The terminology tests run against a stub table, so the suite needs no
network.

## Scope

DICOM SEG only. RTSTRUCT has a different structure and different failure modes.

Metadata only — it does not look at a voxel, so it will not tell you whether a
segmentation is anatomically correct, only whether it says what it means.

## Licence

Apache-2.0. The DICOM standard references are to
[PS3.3](https://dicom.nema.org/medical/dicom/current/output/chtml/part03/PS3.3.html)
and [PS3.16](https://dicom.nema.org/medical/dicom/current/output/chtml/part16/PS3.16.html).
