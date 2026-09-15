# Changelog

## 1.2.0 — 2026-09-15

The review now validates the objects, not only their metadata table.

- **Issue 9 — IOD conformance, via `dciodvfy`.** `scripts/dciodvfy_check.py` runs
  David Clunie's validator over a delivery (`pip install dicom3tools`) and normalises
  its output into the per-segment shape everything else uses, so a Type 1 violation
  lands on a segment in the triage list rather than in a log. Three CSVs: one row per
  message, one row per distinct defect (what the report quotes), one row per object.
  `seg_checks.py --iod` folds the verdict in as `IOD_ERROR` / `IOD_WARNING`.
- **`-new`, `-allpffgitems` and `-filename` are always passed**, and the reasons are
  documented because each is a way to miss findings. `-new` supplies the attribute
  path that makes segment attribution possible at all. Without `-allpffgitems`,
  dciodvfy checks only the **first** per-frame functional group item — verified by
  breaking the last frame of a three-frame object and getting a silent pass. It costs
  about 10× (~4 ms per frame item), which is why the runner parallelises across files.
- **The exit status is documented as unusable**: 1 means "IOD errors *or* unreadable
  file", 0 means "clean *or* warnings only", and everything goes to stderr.
- **A boundary statement, in SKILL.md and the catalogue.** dciodvfy validates
  structure, not semantics — confirmed by planting nonexistent codes in
  `AnatomicRegionSequence` and `SegmentedPropertyTypeCodeSequence` and getting silence.
  Issues 1, 2, 3 and 8 are outside its reach, so a clean run is not a statement about
  the coding. Nothing it reports is ever High: a message *is* the detection.
- **The skill is no longer metadata-only, and now says so.** dciodvfy reads `PixelData`
  and checks its length against Rows × Columns × Frames × BitsAllocated, catching a
  truncated object or one claiming more frames than it carries — which every other
  check in the review passes. Class `BAD_VALUE_LENGTH`. The description, the overview
  and the scope section are reworded: the boundary is the **voxel values**, not the
  pixel data. Nothing here still interprets what was segmented.
- **Issue 10 — retired coding scheme designators** (`SRT`, `SNM3`, `SNM`, `99SDM`).
  dciodvfy reports these as a Warning; the review promotes them, because
  `lookup_codes.py` skips any non-`SCT` scheme and `dcmterm.py` does not count `SRT` as
  private. A batch coded in SRT therefore reports near-total coverage gap and zero
  verified codes, which looks like exotic anatomy and is not. Computed from the
  per-segment table, so unlike issue 9 it runs on all three access paths —
  `sql/13_retired_coding_scheme.sql` is the BigQuery form. The repair is a re-coding,
  not a rename: `T-62000` becomes `10200004`.
- **What each access path can and cannot check** is now stated in all three access
  references and in `sql/12`: a triage list built on BigQuery carries no IOD tag, and
  that silence is "not checked", not "conformant".
- Report provenance extends to the validator build alongside the two terminology
  sources.
- 24 more tests, including the `dciodvfy -new` grammar pinned against captured real
  output — so a dicom3tools build that changes the message format fails the suite
  rather than silently parsing to nothing. The suite still needs neither network nor
  dicom3tools.

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
