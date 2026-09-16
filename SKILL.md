---
name: dicom-seg-review
description: Audit DICOM Segmentation (SEG) objects for defects in how they are coded and encoded - anatomic, category and type codes whose meaning contradicts the code, ambiguous or retired codes, laterality, IOD conformance, empty frames, compression, display colours, algorithm provenance, segment numbering and frame-of-reference integrity - plus the geometry of each segmentation against the source series it references, resolved through IDC. Produces a severity-ranked report and a per-series triage CSV. Use when asked to review, QC, audit, validate or sanity-check DICOM segmentations reachable through BigQuery, a DICOMweb store or local files. SEG only, not RTSTRUCT.
license: Apache-2.0
metadata:
  version: 1.5.0
  skill-author: Andrey Fedorov, @fedorov
---

# DICOM SEG Review

Audits how DICOM Segmentation objects are **coded and encoded**: a segment that
says "Large bowel" while its code denotes the liver, one code carrying five
meanings, a left structure coded as the right one, an object whose `PixelData` is
the wrong length, two organs a viewer will draw in one colour, a segmentation
whose Frame of Reference is not its source image's.

One check is not about defects at all. **Issue 18** compares each segmentation's
grid with the grid of the series it segments, resolving that series through
IDC, and records whether the source is available and whether the two are
sampled the same way. Neither answer is a failure; both belong in the report.

**The boundary is what the voxels mean, not the voxels themselves.** Nothing here
judges whether the liver segmentation covers the liver. Two checks read pixel
data without knowing anatomy: `dciodvfy` checks its length (issue 9) and
`seg_encoding.py` asks whether each frame is all zero (issue 11). Everything else
is metadata. RTSTRUCT is out of scope.

**Two deliverables**, described in `references/reporting.md`: a severity-ranked
issue report with clickable examples, and a per-series triage CSV sorted
worst-first for whoever fixes the annotations. Write both, and every
intermediate CSV, into the batch's own working directory, never into this skill.

## Core rule: separate what is computed from what is judged

| Layer | Needs | Follows a new delivery? |
|---|---|---|
| **Computed** - ambiguity, conformance, retired schemes, context groups, numbering, colours, provenance | the data, plus `dciodvfy` and the public `dcmterms` tables | yes |
| **Curated** - "this code denotes different anatomy than its meaning" | a human comparing a code to its SNOMED fully specified name | no, must be re-derived |

Keep curated verdicts in **one** review table keyed on `(CodeValue, CodeMeaning)`;
its header is a contract, given in `references/terminology.md` and
`templates/review.csv`. When a new batch arrives, the computed checks run as-is;
the review is *stale until re-derived* and the report must say so. Never call a
batch clean on the strength of a terminology join alone (see "The coverage
trap").

## Access paths

All three produce the **same per-segment table**, one row per
`(SOPInstanceUID, SegmentNumber)` with identical columns, and every check runs
on it. Prefer BigQuery where available: the checks are set-based.

| You have | Read | Extract with |
|---|---|---|
| BigQuery metadata table (Healthcare API export, `idc_current.dicom_all`) | `references/access-bigquery.md` | `scripts/sql/01_seg_attributes.sql` as a view, export to CSV |
| DICOMweb endpoint | `references/access-dicomweb.md` | `seg_attributes.py --dicomweb <url> [--gcp] --resolve-referenced` |
| Directory of DICOM files | `references/access-local-files.md` | `seg_attributes.py --files <dir> --resolve-referenced` |

**What a path cannot check must be reported as unchecked, not clean.** Issues 9,
11 and 12 need the objects, so they run on files only; where the delivery is
reachable as files anywhere, retrieve at least a sample and run them. Issue 17
needs the referenced series resolved: BigQuery does it through `@@IMAGE_TABLE@@`,
the other two through `--resolve-referenced`. Issue 18 needs the segmented
series' own geometry, which is a fourth lookup - IDC, or a directory of the
source images - and reaches the orientation only with `--probe` or `--files`.
In a triage CSV, a tag absent because a check never ran looks exactly like a
tag absent because nothing was wrong.

## Workflow

Steps 1-3 need no human and surface the candidates that step 4 judges. Run them
in this order; curating first means judging codes the data never had a problem
with. Install once with `pip install -r requirements.txt`; `seg_checks.py`,
`lookup_codes.py` and `cielab.py` need nothing beyond the standard library.

1. **Extract, and say which sequence carries the anatomy.**
   ```bash
   python scripts/seg_attributes.py --files <dir> --resolve-referenced -o seg_attributes.csv
   ```
   Confirm the row count, that `multiValuedCodeSequence` is false everywhere, and
   read the "Code sequences populated" block. `AnatomicRegionSequence` is Type 3;
   category and type are Type 1. On most organ-segmentation deliveries the organ
   is the **type** code and the region is absent, and the report has to say so.

2. **Characterise the batch**: producer (`Manufacturer`, `SoftwareVersions`),
   `SegmentationType`, `SegmentAlgorithmType`, counts of objects, segments,
   series, patients. A report that opens with this is readable by someone who
   has never seen the data.

3. **Run the computed checks.**
   ```bash
   python scripts/seg_checks.py seg_attributes.csv --outdir findings/          # 2 4 5 6 7 10 13 14 16 17
   python scripts/dcmterm.py coverage  seg_attributes.csv -o findings/coverage.csv   # coverage gap + issue-1 shortlist
   python scripts/dcmterm.py property  seg_attributes.csv -o findings/issue15_property_context_group.csv
   python scripts/dciodvfy_check.py --files <dir> -o findings/                # 9, files only
   python scripts/seg_encoding.py   --files <dir> -o findings/                # 11 12, files only
   python scripts/seg_geometry.py seg_attributes.csv --probe orientation \
       -o findings/issue18_geometry.csv                                       # 18, needs IDC
   ```
   Ambiguity (issue 2) first: fully computed, highest yield, and it hands you the
   shortlist for step 4. `dcmterm.py` checks codes against the code set DICOM's
   own context groups use, downloaded from
   [dcmterms](https://github.com/fedorov/dcmterms) and cached; `coverage` prints
   how much of the batch it reached, and `property` decides issue 15.
   `seg_geometry.py` answers issue 18: whether IDC holds each segmented series,
   and whether its grid is the segmentation's. `--probe orientation` costs one
   ~16 KB ranged HTTPS GET per series and is what supplies the orientation - no
   IDC index carries `ImageOrientationPatient`. Point it at `--files <the
   source images>` instead wherever you have them.
   `dciodvfy` validates **structure, not semantics**: a clean run says nothing
   about the coding. All terminology checks judge the anatomic region, the
   property type and the property category.

4. **Look up every distinct code's fully specified name, and judge.**
   ```bash
   python scripts/lookup_codes.py seg_attributes.csv -o findings/codes.csv          # tx.fhir.org, all three sequences
   python scripts/dcmterm.py suggest findings/codes.csv -o findings/entire_flavour.csv
   ```
   This turns "these codes are suspicious" into "this code denotes a vein and
   the segment calls it bowel", and detects the "Entire X" flavour (issue 8)
   mechanically. Record each verdict once in the review table - `WRONG_ANATOMY`,
   `NARROWER_OR_BROADER`, `SPELLING`, `INVERTED`, `UNCODED` - with its
   `reviewSource` (`dcmterm`, `tx.fhir.org`, `manual`). See
   `references/terminology.md`.

5. **Fold everything in and write the report.**
   ```bash
   python scripts/seg_checks.py seg_attributes.csv --outdir findings/ \
       --codes findings/codes.csv --review review.csv \
       --property findings/issue15_property_context_group.csv \
       --iod findings/issue9_iod_validation.csv --encoding findings/encoding_per_object.csv \
       --geometry findings/issue18_geometry.csv
   ```
   `seg_checks.py` prints which checks ran and which were skipped; the report
   repeats that. `references/reporting.md` gives the report structure and the
   triage tag vocabulary. `scripts/sql/` holds the same checks as BigQuery
   templates, numbered in the order they were added; `scripts/make_fixture.py`
   writes a small synthetic delivery for verifying an installation end to end.

## Checks

Detail, DICOM citations and detection logic are in `references/issue-catalogue.md`.
Numbers are stable so reports can cite them; the table is in severity order.

| # | Severity | Check | Layer |
|---|---|---|---|
| 1 | High / Medium | Code denotes different anatomy than its own `CodeMeaning` | Curated |
| 2 | High | Same code used with conflicting meanings across the batch | Computed |
| 3 | High / Medium | Laterality inverted, or stated in text with no code to carry it | Curated |
| 12 | High / Low | Lossy compressed; or uncompressed where deflate would pay | Computed, files only |
| 8 | Medium | SNOMED "Entire X" flavour where DICOM uses "X structure" | Computed + lookup |
| 9 | Medium / Low | IOD non-conformance reported by `dciodvfy` | Computed, files only |
| 10 | Medium | Codes under a retired scheme designator (`SRT`, `SNM3`) | Computed |
| 4 | Medium | Segment missing a Type 1 attribute, or a code without its scheme | Computed |
| 7 | Medium | `TrackingUID` shared in a way that does not mean "same finding" | Computed |
| 13 | Medium / Low | Two structures given one display colour; colour absent, malformed or not permitted | Computed |
| 14 | Medium / Low | Non-MANUAL segment that does not identify its algorithm or model | Computed |
| 15 | Medium / Low | Type code outside the context group its category names; category or type outside CID 7150 / 7151 | Computed |
| 16 | Medium | Segment Numbers not unique, or not 1..n on a BINARY or FRACTIONAL object | Computed |
| 17 | Medium | Frame of Reference differs from the referenced series; referenced series missing | Computed, needs the references resolved |
| 11 | Medium / Low | Segment with no voxels; all-zero frames a BINARY object kept | Computed, files only |
| 5 | Low | `SegmentedPropertyType` merely repeats the category | Computed |
| 6 | Low | `SegmentsOverlap` absent (Type 3; conformant, but costly on multi-segment objects) | Computed |
| 18 | Low | Segmented series absent, or sampled on a different grid from the segmentation. **Not a defect** | Computed, needs the segmented series |

**Anatomic coding is almost always where the damage is** (issues 1, 2, 3, 8, 15).
Budget the review accordingly. Issues 11 to 17 are cheap and mostly Medium or
Low, but they are what a producer can fix in an afternoon; say so in the
follow-up section.

## Severity is about consumer harm, not scale

- **High**: the metadata asserts something **false** and the object gives no way to
  detect it. One inverted laterality outranks 700 missing optional attributes.
- **Medium**: not false, but **non-conformant or unusable**; detectable, costs effort.
- **Low**: **cosmetic, or conformant but unhelpful**. Nobody is misled.

State scale separately from severity, and never let scale promote a finding.

## Traps

Cross-cutting ones only; each per-issue trap lives with its issue in the
catalogue. Read the catalogue before concluding anything about a batch.

- **The anatomy may not be in `AnatomicRegionSequence`.** It is Type 3. A batch that
  carries the organ as the property type and no region at all is conformant and
  common, and a region-only review reports it clean. Every terminology check here
  judges all three sequences; the report must say which one carries the anatomy.
- **The coverage trap.** `dcmterms` covers only the codes DICOM's context groups use,
  often well under half a batch. A join that treats misses as clean passes
  everything it did not cover. Publish the uncovered codes as their own
  deliverable and resolve them against the terminology server.
- **The SRT trap.** A batch coded under the retired `SRT` designator resolves
  nowhere: `lookup_codes.py` skips non-`SCT` schemes and `dcmterm.py` does not
  count `SRT` as private, so it reports near-total coverage gap and zero verified
  codes and looks like exotic anatomy. Check the designator distribution first.
  The repair is a re-coding: `T-62000` becomes `10200004`.
- **A clean `dciodvfy` run says nothing about the coding.** It consults no context
  group. Report structure and semantics separately.
- **Neither field is trustworthy.** Where `CodeValue` and `CodeMeaning` disagree,
  sometimes the code is wrong and sometimes the meaning. Say which, per case.
- **`SegmentLabel` is rarely independent evidence**: usually the `CodeMeaning` plus an
  enumerator. Measure how often the two differ before leaning on it.
- **Ambiguity is a property of a code, not a segment.** Tag series
  `SELF_INCONSISTENT` / `MINORITY_MEANING` / `DOMINANT_MEANING` and let only the
  first two raise severity, or the triage list becomes a list of everything.
- **Labelmap SEGs carry a Background segment** (`SegmentNumber` 0). An artefact of the
  encoding: exclude it from every count of what was segmented, except issue 16,
  which needs it.
- **Booleans in a CSV vary in case.** The extractor writes `True`; a BigQuery
  export writes `true`. The scripts accept both; a hand-rolled join may not.
- **A Healthcare API export creates a column only for attributes some instance
  populates.** Referencing an absent one is a compile error. Check
  `INFORMATION_SCHEMA.COLUMN_FIELD_PATHS` first; the absence is itself a finding.
- **Filter on `SOPClassUID`, not `Modality = 'SEG'`**, accepting both
  `1.2.840.10008.5.1.4.1.1.66.4` and `...66.7` (Labelmap).
- **A different grid is not a defect, and "not compared" is not a match.** Issue
  18 is Low throughout: PS3.3 A.51.1 constrains the Frame of Reference, not the
  sampling, so a resampled or cropped segmentation is conformant. Report it as a
  property of the delivery. And without `--probe` or `--files` the orientation -
  the dimension most worth having - is never looked at, because no IDC index
  carries `ImageOrientationPatient`.
- **`ReferencedSeriesSequence` is the only series-level link a SEG has**, and a
  batch that records its derivation per SOP instance has none: issues 17 and 18
  are then both silent, and `sourceImageReferenceLevel` is what separates "the
  link is there, per instance" from "there is no link at all". Neither is a
  producer error; both are worth saying.
- **Every finding needs a clickable example.** Build a viewer URL into the
  per-segment table from the start (`--viewer-url`, `@@VIEWER_BASE@@`).
- **Cite every external authority by version**: the DICOM edition `dcmterm.py`
  prints, the `tx.fhir.org` date, the dicom3tools build, the colour threshold.
  Each verdict is a claim about one edition on one day.
