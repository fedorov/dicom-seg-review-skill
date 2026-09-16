# Judging the codes

Step 4–5 of the workflow: turning "these codes are suspicious" into a verdict per
`(CodeValue, CodeMeaning)` pair. This is the part no query does for you, and it is
where the review earns its keep.

## Two sources, and why you need both

| Source | Holds | Use for |
|---|---|---|
| **dcmterms** — every coded entry in PS3.16's context groups, 14,765 unique codes | Codes DICOM's own context groups use, with the meanings DICOM gives them | Deciding what DICOM *expects*; the "Entire X" argument |
| **A FHIR terminology server** — `tx.fhir.org`, public, no auth | All of SNOMED CT, with fully specified names and active/inactive status | Everything the first does not cover |

**Record which source produced each verdict.** A reader needs to know which findings a
machine can reproduce and which rest on a human's reading. One column, `reviewSource`,
values `dcmterm` / `tx.fhir.org` / `manual`.

### Getting the DICOM code set

[fedorov/dcmterms](https://github.com/fedorov/dcmterms) publishes the extraction as
Parquet at `docs/data/` — public, no auth, no BigQuery. `scripts/dcmterm.py` downloads
and caches it:

```bash
python scripts/dcmterm.py fetch                 # ~0.5 MB, cached in ~/.cache/dcmterms
python scripts/dcmterm.py coverage seg_attributes.csv -o findings/coverage.csv
```

| File | |
|---|---|
| `codes_unique.parquet` | one row per (scheme, code, **meaning**) with `num_cids` |
| `coded_entries.parquet` | the same codes with the CID each appears in |
| `extraction_metadata.json` | DICOM edition and extraction date — **cite these** |

**Quote the edition in the report** (`DICOM 2026c, extracted 2026-07-02`). "Not in
DICOM's code set" is a claim about a specific edition, and a later one may add the code.
`dcmterm.py` prints the provenance line on every run for that reason.

Two properties of the table that change how you join it:

- It is deduplicated on the **meaning as well as the code**, so one code appears once
  per spelling DICOM uses — `21974007` is there as both "Tongue" and "tongue". A join
  on the code alone multiplies rows; collapse to one entry per code first.
- Its `code_meaning` is **DICOM's** meaning, not the SNOMED fully specified name. It
  answers "does DICOM use this code for what the batch says", which is a different
  question from "what does SNOMED call it" — and the reason you still need the server.

### The coverage trap

dcmterms holds codes DICOM uses, **not all of SNOMED**. The fraction of a batch it
reaches can be well under half — `dcmterm.py coverage` prints the codes and segments
it covered, and that number belongs in the report.

A check that joins it and treats non-matches as clean therefore passes everything in
the gap without looking at it. Worse, the most serious error in a batch can sit in
that gap: a code like `110634007`, a right-side code recorded as "Left adnexa", is
exactly the kind of inversion DICOM's context groups never carry.

So:

1. Never let a terminology join stand in for the review.
2. **Publish the uncovered codes as their own deliverable**, with segment counts, and
   say in the report that the clean bill does not extend to them. The `inDcmterm`
   column of `coverage.csv` is that list; `dcmterm.py coverage` prints its size and
   segment count so the report can state it.
3. Check every uncovered code against the terminology server.

Private designators (`99…`) are an expected miss, not a gap — they are placeholders for
structures with no standard code. `coverage.csv` separates them as `isPrivateScheme`.

### What the covered half does tell you

For codes dcmterms *does* carry, comparing its `code_meaning` against the batch's is a
**computed** issue-1 signal — no human needed, and it follows a new delivery. The
`meaningAgrees` column is `True`, `False` or `PARTIAL`, and a `False` on a code DICOM
uses widely is as strong as this review gets:

```
code        batch says     DICOM uses it for
10200004    Large bowel    Liver          (in 9 context groups)
```

Comparison is on case and whitespace only. Hyphenation and wording differences survive
it on purpose — "Paraaortic" against "para-aortic" is the `SPELLING` verdict, a finding
rather than noise to normalise away.

## Looking up fully specified names

```bash
python scripts/lookup_codes.py seg_attributes.csv -o findings/codes.csv
```

One row per distinct `(CodingSchemeDesignator, CodeValue)` with:

| Column | |
|---|---|
| `fsn` | Fully specified name from the server |
| `found` | FALSE means the code does not exist — a finding in itself |
| `active` | FALSE means a **retired** concept; DICOM objects should not use one |
| `isEntireFlavour` | `fsn` begins `Entire ` — issue 8 candidate, computed |
| `meaningsInBatch` | Every distinct `CodeMeaning` the batch records, pipe-separated |
| `distinctMeanings`, `segments`, `series` | Scale |

Sort by `segments` descending and work down — a handful of codes usually accounts for
most of the damage.

The raw request, if you need one by hand:

```
https://tx.fhir.org/r4/CodeSystem/$lookup?system=http://snomed.info/sct&code=110634007
```

A 404 means the code is not in SNOMED. Private scheme designators (anything that is
not `SCT`, e.g. `DCM`, `99LOCAL`) are skipped by the script — they cannot be looked up
there and must be judged against whatever defines them.

### `SRT` is skipped too, and that is the trap

`SRT` (and `SNM3`, `SNM`, `99SDM`) is the retired designator for the SNOMED family,
and its code values are the old style — `T-62000` for the liver, not `10200004`. It is
not `SCT`, so `lookup_codes.py` skips it and no FSN is ever fetched. It is not a `99…`
designator either, so `dcmterm.py coverage` counts it as a code DICOM's context groups
do not carry rather than separating it out as private.

**A batch coded in `SRT` therefore reports near-total coverage gap and zero verified
codes**, which reads exactly like a batch of exotic anatomy and is nothing of the kind.
Before quoting any coverage number, check the designator distribution:

```bash
python - <<'EOF'
import csv, collections
rows = list(csv.DictReader(open("seg_attributes.csv")))
for column in ("AnatomicRegion", "SegmentedPropertyCategory", "SegmentedPropertyType"):
    print(column, collections.Counter(
        r[f"{column}CodingSchemeDesignator"] for r in rows).most_common())
EOF
```

`seg_checks.py` reports it as issue 10 and `sql/13_retired_coding_scheme.sql` is the
BigQuery form. The remedy is a per-code mapping from the SNOMED-RT style identifier to
the SCT concept id, which belongs to the annotation producer — **swapping the
designator and keeping the value invents codes that do not exist.** Until that mapping
is done, the terminology half of this review cannot run on those segments, and the
report must say so rather than presenting the empty result as a clean one.

**Rate-limit**: the script sleeps between requests. A few hundred codes takes a couple
of minutes; do not parallelise it into a public server.

## Deciding a verdict

For each pair where the FSN and the batch's `CodeMeaning` disagree:

| Verdict | Test | Severity |
|---|---|---|
| `WRONG_ANATOMY` | Materially different anatomy — a different organ, a different structure class, a vein where the label says bowel | High |
| `NARROWER_OR_BROADER` | Same region, wrong granularity — "left lobe of thyroid" vs "left thyroid gland", "nodule of lung" vs "lung" | Medium |
| `SPELLING` | Hyphenation, case or wording only — "Paraaortic" vs "para-aortic" | Low |
| `INVERTED` | The code names the **opposite side** | High |
| `UNCODED` | The code carries no side, the meaning states one | Medium |

Judgement notes:

- **Decide which field is wrong, per pair, and say so.** Usually the code; sometimes
  the meaning. `21974007` "tongue" recorded as "Submandibular lymph node" was the
  meaning; `10200004` "liver" recorded as "Large bowel" was the code. Reporting only
  "they disagree" leaves the producer with the same problem you had.
- **Watch for anatomical synonyms before calling something wrong.** SNOMED often uses
  older or more formal terms than clinical usage — "left auricular appendage" is the
  left atrial appendage. Try variant terms before concluding a code is absent or
  mismatched.
- **A lymph-node code is not its region, and a region is not its node.** "Left inguinal
  region" for "Left inguinal lymph node" is `NARROWER_OR_BROADER`, not a spelling
  variant. Expect most granularity findings to be this pattern.
- **One label under many codes is a process bug**, not a coding choice. When you see
  it, say so in the report: it points at the producer's workflow, not at individual
  segments.

## "Entire X" versus "X structure"

SNOMED carries two body-structure concepts for most anatomy:

- `302508007` "Entire colon (body structure)" — the complete organ only
- `71854001` "Colon structure (body structure)" — the parent concept: the organ
  or any part of it

DICOM's context groups draw on the *structure* flavour (e.g. CID 4031 lists
`71854001` with Code Meaning "Colon"), so a segmentation of part of an organ
coded "Entire …" asserts more than was segmented. Note DICOM code meanings
never carry the "structure"/"entire" suffix — only the code value tells you.

**PS3.16 §8.1.1 "Use of SNOMED Anatomic Concepts" states this**, with two
rationales: imaging usually covers the feature *plus surrounding area*, and
sometimes only part of it. Phrased as "in general"/"usually", not a *shall* —
and not applied uniformly (CID 4031 includes `38266002` "Entire body").

Both halves of that argument are now reproducible in one command. `lookup_codes.py`
writes `isEntireFlavour` from the FSN; `dcmterm.py suggest` takes that file and, for
each flagged code, reports whether DICOM carries it and what the structure-flavour
candidate is:

```bash
python scripts/dcmterm.py suggest findings/codes.csv -o findings/entire_flavour.csv

302508007  Entire colon          ->  71854001  Colon                 (4 CIDs)
181279003  Entire spleen         ->  78961009  Spleen                (6 CIDs)
```

Its columns match the `entireFlavourCodes` struct in `sql/07`, so they paste straight
in. They are **candidates, not verdicts** — the match is on the FSN's stem, and SNOMED's
naming is not regular enough for that to stand unreviewed. Confirm each one.

Split the finding: codes with a drop-in structure equivalent are a substitution; codes
without one need a decision and may also be at the wrong granularity.

## Laterality: pre- versus post-coordinated

Two ways to say "left":

- **Pre-coordinated** — one code that already names the side (`80248007` left breast).
- **Post-coordinated** — a general anatomic code plus a laterality modifier:
  - `AnatomicRegionModifierSequence` (0008,2220), baseline CID 2
  - `SegmentedPropertyTypeModifierCodeSequence` (0062,0011)
  - laterality from **CID 244**: `7771000` Left, `24028007` Right, `66459002`
    Unilateral, `51440002` Bilateral

Pre-coordination alone causes both laterality failure modes: where no side-specific
code exists the laterality falls into free text (`UNCODED`), and where two similar
codes exist, picking the wrong one flips the side silently (`INVERTED`).

**Check which mechanism the batch uses before reporting anything.** A pre-coordinating
batch populates neither modifier sequence — often the attributes are absent from the
export schema entirely. A post-coordinating one may carry Left / Right / "Right and
left" in either sequence. They are different attributes and a batch may use either.

State the preference as a practical argument. **PS3.3 §10.5** defines the modifier
mechanism and the laterality context groups but does not require post- over
pre-coordination, and a report that claims a conformance violation here will be
correctly rejected.

## Codes with no standard equivalent

Where a structure genuinely has no standard code, a private scheme is better than
either skipping the segment or blocking on a perfect code. A workable convention:

- `CodingSchemeDesignator`: a private designator, e.g. `99LOCAL`
- `CodeValue`: short alphanumeric, **≤16 characters** (SH VR) — never the raw label
  name, which routinely exceeds it
- `CodeMeaning`: the human-readable label

Private codes are placeholders; mapping them to standard codes is a separate activity.
Validators should skip them rather than report them as unresolvable.
