---
name: dicom-seg-review
description: Review a delivery of DICOM Segmentation (SEG) objects for metadata defects — anatomic codes that contradict their own CodeMeaning, one code used with conflicting meanings, inverted or uncoded laterality, non-conformant segments, ambiguous TrackingUIDs, wrong SNOMED code flavour — and produce a severity-ranked report plus a per-series triage list. Use when asked to audit, QC, review or sanity-check DICOM segmentations or their anatomic/SNOMED coding, whether the metadata is reachable through BigQuery, a DICOMweb store, or local DICOM files.
license: Apache-2.0
metadata:
  version: 1.0.0
  skill-author: Andrey Fedorov, @fedorov
---

# DICOM SEG Review

## Overview

Audits the **metadata** of DICOM Segmentation objects — not the pixels. It finds the
defects that make a segmentation delivery unusable downstream: a segment that says
"Large bowel" while its code denotes the liver, one code carrying five different
meanings, a left structure coded as the right one, a segment missing Type 1
attributes.

Distilled from two applied reviews: a 1,316-object / 1,961-segment manual annotation
delivery, and a 3,439-object / 51,584-segment AI-generated store. Every check here
caught something real in at least one of them.

**Two deliverables**, both described in `references/reporting.md`:
1. A severity-ranked issue report — what is wrong, how much of the batch, worked
   examples with viewer links.
2. A per-series triage CSV — one row per SEG series, tagged, sorted worst-first, so
   it can be handed to whoever fixes the annotations.

## Core rule: separate what is computed from what is judged

Every check falls into exactly one of two layers, and they must not be mixed:

| Layer | Needs | Follows a new delivery? |
|---|---|---|
| **Computed** — ambiguity, conformance, TrackingUID, coverage gap | Nothing but the data | **Yes**, automatically |
| **Curated** — "this code denotes different anatomy than its meaning" | A human comparing a code to its SNOMED FSN | **No**, must be re-derived |

Keep the curated verdicts in **one** place (a review table keyed on
`(CodeValue, CodeMeaning)`), not repeated inside each check. When a new batch
arrives, the computed checks run as-is; the curated table is *stale until re-derived*
and saying so is part of the report.

Never claim a batch is clean on the strength of a terminology join alone. See
"The coverage trap" below.

## Choose an access path

All three paths produce the **same per-segment table** — one row per
`(SOPInstanceUID, SegmentNumber)`, column names identical — and every check runs on
that. Pick by what access you have:

| You have | Read | Cost |
|---|---|---|
| A BigQuery metadata table (Healthcare API export, or `idc_current.dicom_all`) | `references/access-bigquery.md` | Best for whole-archive scale; the proven path |
| A DICOMweb endpoint (Healthcare API store, IDC proxy) | `references/access-dicomweb.md` | One metadata request per series; no BigQuery needed |
| A directory of `.dcm` files | `references/access-local-files.md` | Fully offline; `pydicom` only |

If more than one is available, prefer BigQuery — the checks are set-based and a
whole delivery is one query.

## Workflow

**Order matters.** Steps 1–3 need no human input and surface the candidates that
steps 4–5 then judge. Running the curated checks first means curating codes that the
data never had a problem with.

1. **Build the per-segment table.** Per the access reference you chose. Confirm its
   row count and that `multiValuedCodeSequence` is FALSE everywhere — if it is not,
   the single code reported per sequence is only the first, and the checks understate.

2. **Characterise the batch before checking it.** Producer (`Manufacturer`,
   `SoftwareVersions`), `SegmentationType`, `SegmentAlgorithmType`, how many objects,
   segments, series, patients. A report that opens with this is readable by someone
   who has never seen the data; one that opens with findings is not.

3. **Run the computed checks**, in this order:
   - **Ambiguous codes first** (`sql/02_ambiguous_code.sql`, or `seg_checks.py`). One
     code used with more than one `CodeMeaning` is fully computed and needs no
     curation, and it is the single highest-yield check — it found 46% of segments in
     one batch. It also hands you the shortlist for step 4.
   - Coverage gap, conformance, type-repeats-category, SegmentsOverlap, TrackingUID.

4. **Look up every distinct anatomic code's fully specified name** —
   `scripts/lookup_codes.py`. This is what turns "these codes are suspicious" into
   "this code denotes a vein and the segment calls it a bowel". It also detects the
   "Entire X" flavour problem mechanically. See `references/terminology.md`.

5. **Judge the conflicts** and record each verdict once, in the review table:
   `WRONG_ANATOMY`, `NARROWER_OR_BROADER`, `SPELLING`, `INVERTED`, `UNCODED`. Record
   **where each verdict came from** (`dcmterm`, `tx.fhir.org`, a human) — a reader
   needs to know which findings a machine can reproduce.

6. **Roll up per series and write the report.** `references/reporting.md`.

## What to check for

Full detail, DICOM references and detection logic in `references/issue-catalogue.md`.

| # | Severity | Check | Layer |
|---|---|---|---|
| 1 | High / Medium | Code denotes different anatomy than its own `CodeMeaning` | Curated |
| 2 | High | Same code used with conflicting meanings across the batch | Computed |
| 3 | High / Medium | Laterality inverted, or stated in text with no code to carry it | Curated |
| 8 | Medium | SNOMED "Entire X" flavour where DICOM uses "X structure" | Computed + lookup |
| 4 | Medium | Segment missing a Type 1 attribute, or a code without its scheme | Computed |
| 7 | Medium | `TrackingUID` shared in a way that does not mean "same finding" | Computed |
| 5 | Low | `SegmentedPropertyType` merely repeats the category | Computed |
| 6 | Low | `SegmentsOverlap` absent (Type 3 — conformant, but costly on multi-segment objects) | Computed |

Numbering is by discovery order and is kept stable so reports cross-reference; the
table is in severity order. Add new issues with the next free number rather than
renumbering.

**Anatomic coding is almost always where the damage is.** Issues 1, 2, 3 and 8 are
all about it. Budget the review accordingly.

## Severity is about consumer harm, not scale

- **High** — the metadata asserts something **false**, and the object gives no way to
  detect it. One inverted laterality outranks 700 missing optional attributes.
- **Medium** — not false, but **non-conformant or unusable**; detectable, costs effort
  to work around.
- **Low** — **cosmetic, or conformant but unhelpful**. Nobody is misled.

State the scale separately from the severity, and never let scale promote a finding.

## Traps

These each cost real time to learn. Read `references/issue-catalogue.md` before
concluding anything about a batch.

- **The coverage trap.** A terminology table built from DICOM's context groups (e.g.
  `dcmterm`) covers only the codes DICOM itself uses — 57 of 134 codes in one batch.
  A check that joins it and reports the misses as clean **silently passes everything
  it does not cover**, and the single worst error in that batch was in the gap. Always
  publish the uncovered-code list as a deliverable of its own, and check those against
  a terminology server.

- **Neither field is trustworthy.** Where `CodeValue` and `CodeMeaning` disagree,
  sometimes the code is wrong and sometimes the meaning is. A consumer that
  systematically trusts either one gets some segments wrong. Report both, say which
  is wrong per case, and do not "normalise" your way out of it.

- **`SegmentLabel` is usually not independent evidence.** In one batch it was the
  `CodeMeaning` plus an enumerator in 91% of segments. It is a tiebreaker only in the
  minority where it differs.

- **Ambiguity is a property of a code, not of a segment.** Most series using an
  ambiguous code use it with its dominant, correct meaning. Tag them
  `SELF_INCONSISTENT` / `MINORITY_MEANING` / `DOMINANT_MEANING` and let only the first
  two raise a series' severity, or the triage list becomes a list of everything.

- **Labelmap SEGs carry a Background segment.** `SegmentNumber` 0, "Background",
  algorithm MANUAL — an artefact of the encoding, not a finding. It makes
  `SegmentNumber` start at 0 rather than 1, and it will trip the conformance check.
  Flag it (`isBackgroundSegment`) and exclude it from every count of what was
  segmented.

- **A Healthcare API export creates a column only for attributes some instance
  populates.** Referencing `TrackingUID` or `AnatomicRegionModifierSequence` against a
  store where nothing populates them is a *compile* error, not a NULL column. Check
  `INFORMATION_SCHEMA.COLUMN_FIELD_PATHS` first. An attribute being absent from the
  schema is itself a finding: it means no instance in the batch uses it.

- **Filter on `SOPClassUID`, not `Modality = 'SEG'`.** Accept both
  `1.2.840.10008.5.1.4.1.1.66.4` (Segmentation Storage) and
  `...66.7` (Labelmap Segmentation Storage) so a future labelmap delivery is picked up
  rather than silently dropped.

- **Every finding needs a clickable example.** A study/series UID a reader cannot open
  is not evidence. Build a viewer URL into the per-segment table from the start.

## Scripts

Run from the skill root. `seg_checks.py` is stdlib-only; the extractors need
`pydicom` or `dicomweb-client` respectively.

```bash
# 1. Extract the per-segment table (pick one source)
python scripts/seg_attributes.py --files /path/to/seg/dir      -o seg_attributes.csv
python scripts/seg_attributes.py --dicomweb <base-url> --gcp   -o seg_attributes.csv
#    ...or run scripts/sql/01_seg_attributes.sql as a BigQuery view

# 2. Computed checks + per-series triage
python scripts/seg_checks.py seg_attributes.csv --outdir findings/

# 3. Fully specified names for every anatomic code, and "Entire X" detection
python scripts/lookup_codes.py seg_attributes.csv -o findings/codes.csv

# 4. Re-run the checks with the FSNs and your curated verdicts folded in
python scripts/seg_checks.py seg_attributes.csv \
    --codes findings/codes.csv --review review.csv --outdir findings/
```

`scripts/sql/` holds the same checks as BigQuery templates, numbered in run order;
substitute the table name with `--parameter` or `sed`. See
`references/access-bigquery.md`.
