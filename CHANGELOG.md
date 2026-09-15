# Changelog

## 1.1.0 — 2026-09-15

Checking a batch against the code set DICOM itself uses now needs nothing but a
download — no BigQuery table, no credentials.

- `scripts/dcmterm.py` — the DICOM code set from
  [fedorov/dcmterms](https://github.com/fedorov/dcmterms), published as Parquet from
  PS3.16's context groups, downloaded and cached locally. Four commands: `coverage`
  (the coverage-gap deliverable), `lookup` (what DICOM records for a code, and in which
  CIDs), `search` (find a standard code by meaning), `suggest` (structure-flavour
  replacements for the "Entire X" codes).
- **A computed shortlist for issue 1.** Comparing the batch's `CodeMeaning` against
  DICOM's own catches `10200004` "Large bowel" — DICOM's liver code — with no human in
  the loop. It reaches only the covered half of a batch and says that the two disagree,
  not which is wrong, so it shortens the review rather than replacing it.
- **Issue 8's evidence is reproducible.** `suggest` reports both halves of the argument
  — the "Entire" code absent from DICOM's set, the structure flavour present — in the
  column names `sql/07` expects.
- **Provenance in the report.** Every run prints the DICOM edition and extraction date;
  `references/reporting.md` now requires both terminology sources to be cited by
  version, since "not in DICOM's code set" is a claim about one edition.
- `sql/03` and `references/access-bigquery.md` keep the BigQuery path, giving the
  `bq load` command for the public Parquet.
- 13 more tests, run against a stub table so the suite still needs no network.

## 1.0.0 — 2026-09-15

First release.

- `SKILL.md` — the review method: the computed/curated split, the workflow and its
  ordering, the severity scale, and the traps.
- Eight checks catalogued with DICOM references and severity rationale
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
