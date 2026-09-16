# DICOM SEG Review

A [Claude Code](https://claude.com/claude-code) skill for auditing how DICOM
Segmentation (SEG) objects are **coded and encoded**.

It finds the defects that make a segmentation delivery unusable downstream: a segment
labelled "Large bowel" whose code denotes the liver, one code carrying five different
meanings, a left structure coded as the right one, a lesion type filed under the
"Anatomical Structure" category, an object that does not conform to the Segmentation
IOD, a segmentation whose Frame of Reference is not its source image's, two organs a
viewer will draw in the same colour, a BINARY object that shipped a thousand all-zero
frames uncompressed, and an AI-produced segmentation that does not say which model
produced it.

## Install

```bash
git clone <this repo> ~/github/dicom-seg-review-skill
ln -s ~/github/dicom-seg-review-skill ~/.claude/skills/dicom-seg-review
pip install -r ~/github/dicom-seg-review-skill/requirements.txt   # optional, for the scripts
```

Claude Code loads the skill when a task is about reviewing or QC'ing DICOM
segmentations. The guidance needs nothing installed; the scripts have the optional
dependencies listed below.

## What it produces

1. **A severity-ranked issue report** — what is wrong, how much of the batch, worked
   examples with viewer links, and which findings a machine can reproduce.
2. **A per-series triage CSV** — one row per SEG series, tagged, sorted worst-first, so
   it can be handed to whoever fixes the annotations.

## Three access paths, one table

Whether the metadata is in BigQuery, a DICOMweb store, or a directory of files, every
path produces the **same 61-column per-segment table**, and every check runs on that.

```
BigQuery metadata table ─┐
DICOMweb store ──────────┼─> seg_attributes (one row per segment) ─┬─> report
Directory of .dcm files ─┤                                         │
                         ├─> dciodvfy      (files only) ───────────┤
                         └─> seg_encoding  (files only) ───────────┴─> triage CSV
```

Three checks need the objects: issue 9 runs `dciodvfy`, issue 11 reads the pixel
data, issue 12 reads the transfer syntax. Issue 17 needs the referenced image series
resolved. Where a path cannot run a check, the triage list is *silent* about it, not
clean, and the report says so.

## Usage

The method — workflow, severity scale, traps — is [SKILL.md](SKILL.md), and each
step there carries its commands. The shortest useful run, on local files:

```bash
python scripts/seg_attributes.py --files /path/to/delivery --resolve-referenced -o seg_attributes.csv
python scripts/seg_checks.py seg_attributes.csv --outdir findings/
python scripts/dcmterm.py coverage seg_attributes.csv -o findings/coverage.csv
```

`python scripts/make_fixture.py /tmp/seg-fixture` writes a small synthetic delivery
with known defects, to see every output once before touching real data.

## Layout

```
SKILL.md                             the method: workflow, severity scale, traps
references/
  issue-catalogue.md                 the seventeen checks, with DICOM references
  terminology.md                     which sequence carries the anatomy; judging codes;
                                     the review table; SNOMED flavours; laterality
  reporting.md                       writing the report and the triage list
  access-bigquery.md                 path 1 — metadata table
  access-dicomweb.md                 path 2 — QIDO + WADO
  access-local-files.md              path 3 — pydicom over a directory
scripts/
  seg_attributes.py                  extract the per-segment table (files | DICOMweb)
  seg_checks.py                      run the computed checks + triage roll-up
  dciodvfy_check.py                  validate objects against the IOD with dciodvfy
  seg_encoding.py                    empty frames + compression, from the objects
  dcmterm.py                         codes against DICOM's own code set and context groups
  lookup_codes.py                    resolve codes to fully specified names
  cielab.py                          DICOM's CIELab: parse, compare, draw a swatch
  make_fixture.py                    a synthetic five-file delivery with known defects
  sql/                               the same checks as BigQuery templates, 01–17
templates/
  review.csv                         the curated verdict table's header, with two examples
tests/
  test_seg_review.py                 151 tests over the extraction and check logic
requirements.txt                     every optional dependency
```

## Dependencies

| | |
|---|---|
| `seg_checks.py`, `lookup_codes.py`, `cielab.py` | standard library only |
| `dcmterm.py` | any one of `pyarrow`, `duckdb` or `pandas`, to read Parquet |
| `seg_attributes.py --files`, `seg_encoding.py`, `make_fixture.py`, the tests | `pydicom>=3.0` |
| `dciodvfy_check.py` | `dicom3tools` (for `dciodvfy`) and `pydicom>=3.0` |
| `seg_attributes.py --dicomweb` | `dicomweb-client>=0.59`, plus `[gcp]` and `google-auth` for Healthcare API stores |
| `scripts/sql/` | the `bq` CLI |
| `lookup_codes.py` | network access to `tx.fhir.org` (public, no auth) |
| `dcmterm.py` | one download from [fedorov/dcmterms](https://github.com/fedorov/dcmterms) (public, ~1 MB, cached) |

`seg_encoding.py` needs no pixel-data codec and no numpy: frames are scanned as bytes.

## External authorities

All public, none needing credentials. Cite each by version in the report — a
"not in DICOM's code set" or "does not conform" verdict is a claim about one edition,
or one build, on one day:

| | |
|---|---|
| [**dcmterms**](https://github.com/fedorov/dcmterms) | every coded entry in DICOM PS3.16's context groups, and how the groups include one another, as Parquet — what DICOM *expects* |
| [**tx.fhir.org**](https://tx.fhir.org) | all of SNOMED CT — fully specified names, retired concepts, everything dcmterms does not cover |
| [**dicom3tools**](https://github.com/ImagingDataCommons/dicom3tools-python-distributions) | `dciodvfy`, David Clunie's IOD validator — structure against PS3.3. It checks no context group, so it says nothing about whether the coding is right |

The first two are the terminology pair, and neither alone is enough: a review that
joins only dcmterms **silently passes everything it does not cover**. See "The coverage
trap" in `SKILL.md`. The third is orthogonal to both.

## Verification

```bash
pip install pydicom          # the only thing the suite needs
python tests/test_seg_review.py
```

151 tests over extraction, the check logic, colour, algorithm identification, empty
frames, the terminology comparison, context-group membership, segment numbering,
frame-of-reference integrity, the review-table contract, the `dciodvfy -new`
output parser and the guard against a dciodvfy build that rejects its flags. Terminology tests run against a stub table and validator tests against
captured output, so the suite needs neither network, nor dicom3tools, nor fixture
files.

## Scope

DICOM SEG only. RTSTRUCT has a different structure and different failure modes.

**The boundary is what the voxels mean.** Nothing here interprets what was segmented,
so it will not tell you whether a segmentation is anatomically correct — only whether
the object says what it means. Structure and semantics are checked separately:
`dciodvfy` decides whether the object conforms to the IOD; the terminology work decides
whether the codes mean what the labels say. A batch can pass one comprehensively and
fail the other.

## Licence

Apache-2.0. The DICOM standard references are to
[PS3.3](https://dicom.nema.org/medical/dicom/current/output/chtml/part03/PS3.3.html)
and [PS3.16](https://dicom.nema.org/medical/dicom/current/output/chtml/part16/PS3.16.html).
