# Changelog

## 1.0.0 — 2026-09-15

First release. Distilled from two applied DICOM SEG metadata reviews: a 1,316-object
manual annotation delivery (APOLLO5) and a 3,439-object AI-generated segmentation store
(IDC).

- `SKILL.md` — the review method: the computed/curated split, the workflow and its
  ordering, the severity scale, and the traps.
- Eight checks catalogued with DICOM references, severity rationale and observed scale
  (`references/issue-catalogue.md`).
- Three access paths, all converging on one per-segment table: BigQuery metadata table,
  DICOMweb, local files.
- `scripts/seg_attributes.py` — extraction from local files or DICOMweb.
- `scripts/seg_checks.py` — the computed checks and the per-series triage roll-up.
  Standard library only.
- `scripts/lookup_codes.py` — fully specified names from `tx.fhir.org`, with mechanical
  detection of the SNOMED "Entire X" flavour.
- `scripts/sql/01`–`12` — the same checks as BigQuery templates, numbered in run order.
- 30 tests over the extraction and check logic.

Verified against real data: the scripts on SEG objects from both deliveries, the SQL
templates dry-run against a store holding images and segmentations and one holding only
segmentations.
