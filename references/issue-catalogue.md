# Issue catalogue

The eight checks. Each gives what the defect is, why it matters, how to detect it, and
what the finding looks like in practice.

Numbering is stable. Add new issues with the next free number; do not renumber, since
reports cite these.

---

## 1. The code denotes different anatomy than the recorded meaning

**Severity: High** where the code names materially different anatomy, **Medium** where
it is only a granularity mismatch. **Layer: curated.**

`AnatomicRegionSequence` (0008,2218) carries a `CodeValue` and a `CodeMeaning`. When
they name different things, one of the two is false and *the object gives no way to
tell which*. This is the defect that makes a delivery unsafe rather than merely untidy.

**Detection.** Not computable in general. Look up each distinct code's fully specified
name (`scripts/lookup_codes.py`), compare it to the `CodeMeaning` the batch records,
and record a verdict per `(CodeValue, CodeMeaning)` pair:

| Verdict | Meaning |
|---|---|
| `WRONG_ANATOMY` | The code names materially different anatomy. An error. |
| `NARROWER_OR_BROADER` | Right region, wrong granularity. |
| `SPELLING` | Hyphenation or wording only. |

Split them, because they need different responses: `WRONG_ANATOMY` needs a per-segment
human decision, `SPELLING` needs a find-and-replace.

**Part of it *is* computable, and costs one command.** For codes DICOM's own context
groups use, `scripts/dcmterm.py coverage` compares the batch's `CodeMeaning` against
DICOM's and flags disagreement in `meaningAgrees` — `10200004` "Large bowel" against
DICOM's "Liver", with the number of context groups DICOM uses that code in. Run it
before curating anything: it hands you a ranked shortlist with `reviewSource` already
answered as `dcmterm`. It does **not** replace the review — it reaches only the covered
half of the batch, and it tells you the two disagree, not which of them is wrong.

**What the conflicts look like.** Report them as a table of code, what it denotes, and
what the batch recorded — for example `10200004` (liver) recorded as "Large bowel",
`80248007` (left breast) as "Left iliac bone", `75397005` (preductal region of aortic
arch) as "Preaortic lymph node".

Two patterns worth naming in any report:

- **Which field is wrong varies.** Usually the code is the wrong one, but not always:
  `21974007` "tongue" recorded as "Submandibular lymph node" is a case where the code
  is right and the meaning wrong. A consumer that systematically trusted either field
  would get some segments wrong.
- **One label scattered across many codes reads like a carry-over bug.** A label with
  a correct home that also appears under several unrelated codes is not a coding
  choice; it is a code left over from the previous segment. Check for this explicitly
  — group by `CodeMeaning` and count distinct `CodeValue`.

---

## 2. The same code used with conflicting meanings

**Severity: High.** **Layer: computed — run this first.**

A `CodeValue` used elsewhere in the batch with a different `CodeMeaning`. Fully
computed, no curation, and typically the highest-yield check in the set — it can reach
a large fraction of the segments in a batch.

Consequence: **neither field works as a grouping key.** Grouping on `CodeValue` merges
segments labelled as different anatomy; grouping on `CodeMeaning` splits segments that
share a code.

**Detection.** Group by `CodeValue`, keep those with more than one distinct
`CodeMeaning`. Then classify each segment by `scope` — this is the column that matters:

| `scope` | Means |
|---|---|
| `SELF_INCONSISTENT` | This segment's **series** uses one code two ways by itself. Strongest evidence. |
| `MINORITY_MEANING` | This segment uses the code's minority reading. |
| `DOMINANT_MEANING` | This segment uses the usual reading; implicated only because some *other* series differs. **Not evidence about this segment.** |

Expect the last category to dominate: most series using an ambiguous code use it with
its dominant, correct meaning. Letting those raise a series' severity turns the triage
list into a list of everything. See "not a work queue" in `reporting.md`.

Some ambiguity is cosmetic — the same anatomy written two ways (`Left adrenal gland` /
`Left adrenal`, `Right axillary` / `Right **axilary**`). Tag these `COSMETIC_VARIANT`
and rank them Low; they are a normalisation task, not a re-coding task.

This check is what produces the shortlist that issues 1 and 3 then judge.

---

## 3. Laterality inverted or uncoded

**Severity: High** for an inversion, **Medium** for uncoded. **Layer: curated.**

| Verdict | Meaning |
|---|---|
| `INVERTED` | The code names the **opposite side** from the `CodeMeaning`. A factual error nothing in the object can detect. |
| `UNCODED` | The code carries no side, but the meaning states one — the laterality exists only as free text. |

An inversion looks like `110634007` — *right* uterine adnexa — recorded as "Left
adnexa". Expect inversions to be rare and uncoded laterality to be common.

**An inversion can easily sit outside DICOM's code set**, where no automated sweep
reaches it and only a human comparing codes to FSNs finds it. A single such segment is
the best argument for doing step 4 of the workflow by hand.

### The root cause is pre-coordination

Both failure modes come from one choice: expressing laterality by **pre-coordination**,
picking a code that already names the side. DICOM's post-coordinated alternative is
**`AnatomicRegionModifierSequence` (0008,2220)**, baseline CID 2, laterality from
**CID 244** — `7771000` Left, `24028007` Right, `66459002` Unilateral, `51440002`
Bilateral. Where a batch pre-coordinates, this attribute is typically absent from the
export schema entirely, which is itself the finding.

Where no pre-coordinated code exists for a side, the laterality falls into free text.
Where two similar codes exist, picking the wrong one flips the side silently.

**State this as a practical argument, not a conformance violation.** PS3.3 §10.5
defines the modifier mechanism and the laterality context groups but does not require
post- over pre-coordination.

**Check *which* modifier sequence a batch uses before reporting anything.** A batch
that does post-coordinate may carry Left / Right / "Right and left" in
`SegmentedPropertyTypeModifierCodeSequence` (0062,0011) rather than in the
anatomic-region modifier. They are different attributes and either may be the one in
play.

---

## 4. Structurally malformed segments

**Severity: Medium** — badly non-conformant, but impossible to mistake for valid data.
**Layer: computed.**

The Segment Description Macro (**PS3.3 Table C.8.20-4**) makes these **Type 1**:

- `SegmentNumber` (0062,0004)
- `SegmentLabel` (0062,0005)
- `SegmentedPropertyCategoryCodeSequence` (0062,0003)
- `SegmentedPropertyTypeCodeSequence` (0062,000F)
- `SegmentAlgorithmType` (0062,0008)

Also flag a code sequence present **without its `CodingSchemeDesignator`** — a code
value alone does not identify a concept.

`AnatomicRegionSequence` and `RecommendedDisplayCIELabValue` are optional; their
absence is conformant and must **not** be reported here. `TrackingID`/`TrackingUID` are
Type 1C.

These are usually few and badly broken — a segment empty but for a single code value
with no scheme designator breaks four Type 1 attributes and supplies the fifth
incompletely. A NULL `SegmentNumber` also breaks the `(SOPInstanceUID, SegmentNumber)`
key; say so when you claim that key.

**Exclude Background segments first** on labelmap deliveries, or this check fires on
every object.

---

## 5. SegmentedPropertyType repeats the category

**Severity: Low.** **Layer: computed.**

`SegmentedPropertyTypeCodeSequence` (0062,000F) set to the same code as
`SegmentedPropertyCategoryCodeSequence` (0062,0003), so the type adds nothing beyond
the category. Conformant, just uninformative.

Report the correctly coded majority alongside the offenders — the distribution of
`SegmentedPropertyType` across the batch — or the finding reads as bigger than it is.

---

## 6. SegmentsOverlap absent

**Severity: Low, and arguably not a defect.** **Layer: computed.**

`SegmentsOverlap` (0062,0013) is **Type 3** in the Segmentation Image Module
(**PS3.3 C.8.20.2**), so omitting it is fully conformant.

Report it so its absence is not later mistaken for a defect, and so a consumer that
needs to know whether segments share voxels can see which objects it must compute that
from instead. **Only multi-segment objects lose anything** — give the segment count per
object and sort by it.

This attribute is **object-level, not per-segment**: report one row per SOP instance.

Quote both numbers — objects missing it, and how many of those hold more than one
segment. The second is the one that matters.

---

## 7. TrackingUID does not reliably identify one finding

**Severity: Medium.** **Layer: computed.**

`TrackingUID` (0062,0021) exists to identify a **finding** across observations. A
shared UID can mean several different things, so "same UID" cannot be read as "same
finding, different observation" without inspecting the surrounding structure.

**Detection.** Group by `TrackingUID`, keep the shared ones, and classify:

| Pattern | Reading |
|---|---|
| Across studies, one patient | One lesion followed over time. **The intended use.** |
| Within one study, two segmentation series | **Unclear** — one finding annotated twice, or a UID reused. |
| Within one study, a seed-point series and a lesion series | The seed point and the segmentation of one finding. |

Give the breakdown by pattern — how many shared UIDs are longitudinal, how many are
unclear within-study, how many are seed-and-lesion — rather than a single count of
shared UIDs.

Also check and **report the reassuring negatives**: no UID shared across patients, none
repeated within a single series. They bound the problem, and a report that only lists
what is broken cannot be acted on proportionately.

The within-study pairs are the ones to escalate; until they are resolved, grouping on
`TrackingUID` mixes "the same lesion three months later" with "the same lesion recorded
twice today".

---

## 8. SNOMED "Entire X" flavour where DICOM uses "X structure"

**Severity: Medium.** **Layer: computed, once FSNs are looked up.**

SNOMED carries two body-structure concepts for most anatomy:

- `302508007` "Entire colon (body structure)" — the whole organ, **exclusively**
- `71854001` "Colon structure (body structure)" — the organ **or any part of it**

DICOM's context groups draw on the *structure* flavour. A segmentation of part of the
colon coded "Entire colon" asserts more than was segmented.

**Detection.** Mechanical once you have FSNs: flag every code whose fully specified
name begins `Entire `. `scripts/lookup_codes.py` does this.

**PS3.16 §6 does not state the rule, so build the evidence from DICOM's own code set**,
per batch: show that each "Entire" code the batch uses is **absent** from DICOM's
context groups while its structure counterpart is **present**. That is an argument;
"DICOM prefers it" is not.

`scripts/dcmterm.py suggest findings/codes.csv` produces both halves from the public
dcmterms Parquet: whether DICOM carries the "Entire" code, and what the
structure-flavour candidate is. Its columns match the `entireFlavourCodes` struct in
`sql/07`. The candidates are matched on the FSN's stem and need confirming.

**Separate the substitutable from the undecidable.** Codes with a drop-in structure
equivalent are a find-and-replace. Codes without one — typically the more specific
lymph-node and region concepts, for which SNOMED has no "structure" sibling — need a
decision, and those segments may also be at the wrong granularity.

Watch for codes with **two problems at once**: `181616008` "Entire peritoneal cavity"
used for "Peritoneal deposit" is both the entire flavour *and* an issue 1 conflict.

---

## Reporting the codes nothing could verify

Not an issue, but a deliverable that must accompany the others: the anatomic codes
DICOM's own code set does not carry, with how many segments depend on each.
`scripts/dcmterm.py coverage` writes it — the rows where `inDcmterm` is `False` and
`isPrivateScheme` is `False`.

This can be most of the batch. No automated check verifies any of those codes, so the
clean bill from the computed checks does not extend to them, and the report must say
so explicitly. `dcmterm.py coverage` prints the code and segment counts to quote.

Two things belong in that paragraph beside the counts:

- **The DICOM edition** the code set came from (`dcmterm.py` prints it: `2026c`,
  extracted `2026-07-02`). "Not in DICOM's code set" is a claim about one edition.
- **Private-scheme codes counted separately.** A `99…` designator is *expected* to be
  absent — it is a placeholder for a structure with no standard code, not a gap in the
  review. Lumping the two together overstates the problem.

See "The coverage trap" in `SKILL.md` and `references/terminology.md`.
