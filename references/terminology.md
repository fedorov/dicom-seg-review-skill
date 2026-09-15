# Judging the codes

Step 4–5 of the workflow: turning "these codes are suspicious" into a verdict per
`(CodeValue, CodeMeaning)` pair. This is the part no query does for you, and it is
where the review earns its keep.

## Two sources, and why you need both

| Source | Holds | Use for |
|---|---|---|
| **A DICOM-derived code table** (e.g. `idc-sandbox-000.dcmterm.codes_unique`, 14,765 rows) | Codes DICOM's own context groups use, with the meanings DICOM gives them | Deciding what DICOM *expects*; the "Entire X" argument |
| **A FHIR terminology server** — `tx.fhir.org`, public, no auth | All of SNOMED CT, with fully specified names and active/inactive status | Everything the first does not cover |

**Record which source produced each verdict.** A reader needs to know which findings a
machine can reproduce and which rest on a human's reading. One column, `reviewSource`,
values `dcmterm` / `tx.fhir.org` / `manual`.

### The coverage trap

The DICOM-derived table holds codes DICOM uses, **not all of SNOMED**. In the manual
batch it covered **57 of 134 anatomic codes — 880 of 1,961 segments**.

A check that joins it and treats non-matches as clean therefore passes more than half
the batch without looking at it. Worse, the single worst error in that batch —
`110634007`, a right-side code recorded as "Left adnexa" — was in the uncovered half.

So:

1. Never let a terminology join stand in for the review.
2. **Publish the uncovered codes as their own deliverable**, with segment counts, and
   say in the report that the clean bill does not extend to them.
3. Check every uncovered code against the terminology server.

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

Sort by `segments` descending and work down: in both batches a handful of codes
accounted for most of the damage.

The raw request, if you need one by hand:

```
https://tx.fhir.org/r4/CodeSystem/$lookup?system=http://snomed.info/sct&code=110634007
```

A 404 means the code is not in SNOMED. Private scheme designators (anything that is
not `SCT`, e.g. `DCM`, `99LOCAL`) are skipped by the script — they cannot be looked up
there and must be judged against whatever defines them.

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

Judgement notes from doing this twice:

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
  variant — most of the granularity findings in the manual batch were this pattern.
- **One label under many codes is a process bug**, not a coding choice. When you see
  it, say so in the report: it points at the producer's workflow, not at individual
  segments.

## "Entire X" versus "X structure"

SNOMED carries two body-structure concepts for most anatomy:

- `302508007` "Entire colon (body structure)" — the whole organ, **exclusively**
- `71854001` "Colon structure (body structure)" — the organ **or any part of it**

DICOM's context groups draw on the *structure* flavour, so a segmentation of part of an
organ coded "Entire …" asserts more than was segmented.

**PS3.16 §6 does not state this rule**, so argue it from DICOM's own code set: all 11
"Entire" codes in the manual batch were **absent** from the DICOM-derived table, while
the structure counterpart was **present** for each one that had a findable equivalent.
That is evidence; "DICOM prefers it" is not.

Split the finding: codes with a drop-in structure equivalent are a substitution (111 of
142 segments), codes without one need a decision and may also be at the wrong
granularity (31 segments, five codes).

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

**Check which mechanism the batch uses before reporting anything.** The manual batch
populated neither modifier sequence; the AI batch carried Left / Right / "Right and
left" in `SegmentedPropertyTypeModifierCodeSequence` on 26,428 segments. The two
sequences are different attributes and a batch may use either.

State the preference as a practical argument. **PS3.3 §10.5** defines the modifier
mechanism and the laterality context groups but does not require post- over
pre-coordination, and a report that claims a conformance violation here will be
correctly rejected.

## Codes with no standard equivalent

Where a structure genuinely has no standard code, a private scheme is better than
either skipping the segment or blocking on a perfect code. The convention used in the
projects this came from:

- `CodingSchemeDesignator`: a private designator, e.g. `99APOLLO`
- `CodeValue`: short alphanumeric, **≤16 characters** (SH VR) — never the raw label
  name, which routinely exceeds it
- `CodeMeaning`: the human-readable label

Private codes are placeholders; mapping them to standard codes is a separate activity.
Validators should skip them rather than report them as unresolvable.
