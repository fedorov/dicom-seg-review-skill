---
name: dicom-seg-review
description: Review a delivery of DICOM Segmentation (SEG) objects for defects in how they are coded and encoded — anatomic codes that contradict their own CodeMeaning, one code used with conflicting meanings, inverted or uncoded laterality, IOD non-conformance and malformed pixel data found by dciodvfy, retired coding scheme designators, ambiguous TrackingUIDs, wrong SNOMED code flavour, all-zero frames a BINARY object should have omitted, missing or lossy compression, recommended display colours that collide between structures, and automatic segments that do not identify the algorithm or model that produced them — and produce a severity-ranked report plus a per-series triage list. Use when asked to audit, QC, review, validate or sanity-check DICOM segmentations, their anatomic/SNOMED coding, their display colours, their provenance, their size and compression, or their conformance to the Segmentation IOD, whether the objects are reachable through BigQuery, a DICOMweb store, or local DICOM files.
license: Apache-2.0
metadata:
  version: 1.3.0
  skill-author: Andrey Fedorov, @fedorov
---

# DICOM SEG Review

## Overview

Audits how DICOM Segmentation objects are **coded and encoded**. It finds the defects
that make a segmentation delivery unusable downstream: a segment that says "Large
bowel" while its code denotes the liver, one code carrying five different meanings, a
left structure coded as the right one, a segment missing Type 1 attributes, an object
whose `PixelData` is the wrong length for the frames it claims.

**The boundary is what the voxels mean, not the voxels themselves.** Nothing here
interprets what was segmented, so it cannot tell you the liver segmentation covers
the liver. Two checks do read the pixel data, and neither needs to know anatomy to
do it: `dciodvfy` compares `PixelData`'s length against Rows × Columns × Frames ×
BitsAllocated, catching a truncated or mis-framed object (issue 9), and
`seg_encoding.py` asks of each frame only "is every bit zero", which is what finds
the empty frames a BINARY object should have omitted and the segments that hold
nothing at all (issue 11). Everything except issues 9, 11 and 12 works from
metadata alone.

**Two deliverables**, both described in `references/reporting.md`:
1. A severity-ranked issue report — what is wrong, how much of the batch, worked
   examples with viewer links.
2. A per-series triage CSV — one row per SEG series, tagged, sorted worst-first, so
   it can be handed to whoever fixes the annotations.

## Core rule: separate what is computed from what is judged

Every check falls into exactly one of two layers, and they must not be mixed:

| Layer | Needs | Follows a new delivery? |
|---|---|---|
| **Computed** — ambiguity, IOD conformance, retired schemes, TrackingUID, coverage gap | Nothing but the data (plus `dciodvfy` for the IOD) | **Yes**, automatically |
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
| A BigQuery metadata table (Healthcare API export, or `idc_current.dicom_all`) | `references/access-bigquery.md` | Best for whole-archive scale |
| A DICOMweb endpoint (Healthcare API store, IDC proxy) | `references/access-dicomweb.md` | One metadata request per series; no BigQuery needed |
| A directory of `.dcm` files | `references/access-local-files.md` | Fully offline; `pydicom` only |

If more than one is available, prefer BigQuery — the checks are set-based and a
whole delivery is one query.

**Three checks need the files themselves**, and for three different reasons:

| Issue | Needs | Because |
|---|---|---|
| 9 — IOD conformance | the object | `dciodvfy` validates an object; a metadata row is not one |
| 11 — empty frames | the pixel data | no export carries voxels |
| 12 — compression | the file meta group | (0002,0010) is dropped by a BigQuery export and not returned by DICOMweb metadata |

Where the delivery is reachable as files, run all three; where it is not, retrieve a
sample and say in the report that they were checked on a sample, or not at all.
**Silence on issues 9, 11 and 12 must not read as a pass** — in a triage CSV, a tag
absent because the check never ran is indistinguishable from a tag absent because
nothing was wrong.

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
     code used with more than one `CodeMeaning` is fully computed, needs no curation,
     and is typically the highest-yield check in the set. It also hands you the
     shortlist for step 4.
   - **Against DICOM's own code set** (`scripts/dcmterm.py coverage`). Which codes
     DICOM's context groups use, and whether it uses them for what the batch says.
     Public Parquet from [dcmterms](https://github.com/fedorov/dcmterms), cached
     locally — no BigQuery, no credentials. Produces both the coverage-gap deliverable
     and a computed shortlist of meaning disagreements.
   - **Against the IOD** (`scripts/dciodvfy_check.py`), if the files are on disk.
     `dciodvfy` reads the whole Segmentation IOD, so it supersedes the hand-rolled
     Type 1 check. It validates **structure, not semantics** — it will never find a
     miscoded anatomic region, and a clean run is not a statement about the coding.
   - **Against the encoding** (`scripts/seg_encoding.py`), if the files are on
     disk. Empty frames a BINARY object kept, segments with no voxels at all, the
     transfer syntax, and what deflate would actually have saved — measured on the
     delivery's own pixel data rather than asserted.
   - Retired coding scheme designators (`SRT`, `SNM3`). Cheap, and it decides
     whether the terminology steps below can work at all — see the trap below.
   - Conformance, type-repeats-category, SegmentsOverlap, TrackingUID, display
     colour, algorithm identification.

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
| 9 | Medium / Low | IOD non-conformance reported by `dciodvfy` | Computed (needs the files) |
| 10 | Medium | Codes under a retired scheme designator (`SRT`, `SNM3`) | Computed |
| 4 | Medium | Segment missing a Type 1 attribute, or a code without its scheme | Computed |
| 7 | Medium | `TrackingUID` shared in a way that does not mean "same finding" | Computed |
| 13 | Medium / Low | Two structures given the same display colour; colour absent, malformed, or not permitted | Computed |
| 14 | Medium / Low | Non-MANUAL segment that does not identify its algorithm or model | Computed |
| 12 | High / Low | Lossy compressed; or uncompressed where deflate would pay | Computed (needs the files) |
| 11 | Medium / Low | Segment with no voxels; all-zero frames a BINARY object kept | Computed (needs the files) |
| 5 | Low | `SegmentedPropertyType` merely repeats the category | Computed |
| 6 | Low | `SegmentsOverlap` absent (Type 3 — conformant, but costly on multi-segment objects) | Computed |

Numbering is stable so reports cross-reference; the table is in severity order. Add
new issues with the next free number rather than renumbering.

**Anatomic coding is almost always where the damage is.** Issues 1, 2, 3 and 8 are
all about it. Budget the review accordingly. Issues 11 to 14 are cheap to run and
mostly Low — but they are the ones a producer can fix in an afternoon, and the
follow-up section should say so.

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

- **The coverage trap.** A terminology table built from DICOM's context groups
  (`dcmterms`) covers only the codes DICOM itself uses, which can be well under half
  the codes in a batch. A check that joins it and reports the misses as clean
  **silently passes everything it does not cover**. Measure the gap on your own batch
  — `dcmterm.py coverage` prints how many codes and segments it reached — then publish
  the uncovered-code list as a deliverable of its own and check those against a
  terminology server.

- **The SRT trap.** A batch coded under the retired `SRT` designator resolves
  nowhere: `lookup_codes.py` skips any non-`SCT` scheme, and `dcmterm.py coverage`
  counts the code as absent from DICOM's context groups without marking it private,
  because `SRT` is not a `99…` designator. The result is a batch that reports
  **near-total coverage gap and zero verified codes** and looks like a pile of exotic
  anatomy. It is not. Check the designator distribution before believing any coverage
  number, and remember the repair is a re-coding — `T-62000` becomes `10200004`, so
  swapping the designator alone invents codes that do not exist. Issue 10.

- **A clean `dciodvfy` run says nothing about the coding.** It validates structure
  against the IOD, not semantics: no context group is consulted for
  `AnatomicRegionSequence` or `SegmentedPropertyTypeCodeSequence`, so issues 1, 2, 3
  and 8 are entirely outside its reach. Report the two separately, or "passes the
  DICOM validator" will be read as "the anatomy was checked".

- **`dciodvfy` needs three flags and an ignored exit status.** `-new` for the
  attribute path that lets a message be attributed to a segment; `-allpffgitems`
  because by default only the **first** per-frame functional group item is checked;
  `-filename` so a batch run can be attributed. Its exit status is 1 for "IOD errors
  **or** unreadable file" and 0 for "clean **or** warnings only" — parse the output
  instead, and read it from **stderr**, where all of it goes. Issue 9.

- **Neither field is trustworthy.** Where `CodeValue` and `CodeMeaning` disagree,
  sometimes the code is wrong and sometimes the meaning is. A consumer that
  systematically trusts either one gets some segments wrong. Report both, say which
  is wrong per case, and do not "normalise" your way out of it.

- **`SegmentLabel` is usually not independent evidence.** It is commonly the
  `CodeMeaning` plus an enumerator. Measure how often the two differ before leaning on
  it; it is a tiebreaker only in the minority where it does.

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

- **A BINARY segmentation's frames are not byte-aligned.** PS3.5 §8.1: with
  `BitsAllocated` 1 in Native Format "the individual Frames are not padded... a frame
  other than the first frame may start in the middle of a byte". So where
  Rows × Columns is not a multiple of 8, slicing `PixelData` at byte boundaries reads
  a neighbour's pixels, and an empty frame whose predecessor ended mid-byte reads as
  non-empty. Mask a bit range. The opposite holds for an **encapsulated** object,
  where each frame is its own fragment and does start on a byte. Issue 11.

- **`LossyImageCompression` = 01 does not mean this object was lossy compressed.**
  PS3.3 C.8.20.2.2 requires the value to be `01` when any of the **source images**
  was, so an intact segmentation of a lossy-compressed CT carries it. Take the
  verdict from the transfer syntax; reading (0028,2110) the other way manufactures a
  High finding out of a correct object. Issue 12.

- **Two segments of the same structure sharing a colour is the point, not a defect.**
  A colour finding is about two *different* structures a viewer will draw
  identically. Compare structures — the type and anatomic-region codes — not
  segments, or the check fires on every consistent delivery in existence. Issue 13.

- **Decide colour in CIELab, render it only to show a human.** ΔE\*ab follows from
  PS3.3 C.10.7.1.1 alone, so "these two are the same colour" is exact; converting to
  sRGB needs a white point the Standard only implies (the ICC PCS D50, which is what
  PixelMed and dcmqi assume). A swatch is the producer's intent, not evidence. Issue
  13.

- **`SegmentAlgorithmName` is Type 1C, not optional.** "Required if Segment Algorithm
  Type is not MANUAL" — so its absence on an AUTOMATIC segment is a conformance
  violation, while the missing *version* beside it is not: that lives in
  `SegmentationAlgorithmIdentificationSequence`, which is Type 3. Report the two
  differently or a reader will over- or under-react to both. Issue 14.

- **`ManufacturerModelName` is not the model.** On a converted segmentation it names
  the converter — dcmqi, highdicom — not what did the segmenting. Use it as a
  cross-check instead: an inference toolkit in that attribute beside
  `SegmentAlgorithmType = MANUAL` means the batch is misdescribing how it was made.
  Issue 14.

- **Every finding needs a clickable example.** A study/series UID a reader cannot open
  is not evidence. Build a viewer URL into the per-segment table from the start.

## Scripts

Run from the skill root. `seg_checks.py`, `lookup_codes.py` and `cielab.py` are
stdlib-only; the extractors need `pydicom` or `dicomweb-client`, `seg_encoding.py`
needs `pydicom` (but no pixel-data codec — it reads bytes), `dcmterm.py` needs any
one of `pyarrow`, `duckdb` or `pandas` to read Parquet, and `dciodvfy_check.py`
needs `pip install dicom3tools` (plus `pydicom`, to resolve segment numbers).

```bash
# 1. Extract the per-segment table (pick one source)
python scripts/seg_attributes.py --files /path/to/seg/dir      -o seg_attributes.csv
python scripts/seg_attributes.py --dicomweb <base-url> --gcp   -o seg_attributes.csv
#    ...or run scripts/sql/01_seg_attributes.sql as a BigQuery view

# 2. Computed checks (issues 2, 4, 5, 6, 7, 10) + per-series triage
python scripts/seg_checks.py seg_attributes.csv --outdir findings/

#    IOD conformance - local files only. -new, -allpffgitems and -filename are
#    passed for you; dropping any of them hides findings.
pip install dicom3tools
python scripts/dciodvfy_check.py --files /path/to/seg/dir -o findings/

#    Empty frames and compression (issues 11, 12) - local files only. Reads the
#    pixel data as bytes; no codec, no numpy. Measures what deflate would save.
python scripts/seg_encoding.py --files /path/to/seg/dir -o findings/

# 3. Against DICOM's own code set: coverage gap + meaning disagreements
python scripts/dcmterm.py coverage seg_attributes.csv -o findings/coverage.csv

# 4. Fully specified names for every anatomic code, and "Entire X" detection
python scripts/lookup_codes.py seg_attributes.csv -o findings/codes.csv
python scripts/dcmterm.py suggest findings/codes.csv -o findings/entire_flavour.csv

# 5. Re-run the checks with the FSNs, the IOD and encoding verdicts and your
#    curated verdicts folded into the triage list
python scripts/seg_checks.py seg_attributes.csv \
    --codes findings/codes.csv --review review.csv \
    --iod findings/issue9_iod_validation.csv \
    --encoding findings/encoding_per_object.csv --outdir findings/
```

Issues 13 and 14 need no extra step — `seg_checks.py` runs them on every invocation,
and the colour palette comes out as `issue13_color_palette.csv` plus a paste-ready
`issue13_color_palette.md` with a swatch per row. `scripts/cielab.py` is also a CLI,
for reading one stored triplet by hand:

```bash
python scripts/cielab.py 35580/53665/50856
# 35580/53665/50856  ->  L*=54.3 a*=80.8 b*=69.9  ->  rgb(255,0,0)  #ff0000
```

`scripts/sql/` holds the same checks as BigQuery templates, numbered in run order;
substitute the table name with `--parameter` or `sed`. See
`references/access-bigquery.md`. Issues 9, 11 and 12 have no SQL form — the
validator needs the objects, the empty-frame check needs the voxels, and the
transfer syntax is in the file meta group, which no export carries.
