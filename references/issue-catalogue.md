# Issue catalogue

The eight checks, in discovery order. Each gives what the defect is, why it matters,
how to detect it, and what was actually found in the two batches this was built from
("manual batch" = 1,316 SEG objects / 1,961 segments from a commercial annotation
vendor; "AI batch" = 3,439 labelmap objects / 51,584 segments from MOOSE and
TotalSegmentator).

Numbering is stable. Add new issues with the next free number; do not renumber, since
reports cite these.

---

## 1. The code denotes different anatomy than the recorded meaning

**Severity: High** where the code names materially different anatomy, **Medium** where
it is only a granularity mismatch. **Layer: curated.**

`AnatomicRegionSequence` (0008,2218) carries a `CodeValue` and a `CodeMeaning`. When
they name different things, one of the two is false and *the object gives no way to
tell which*. This is the defect that makes a delivery unsafe rather than merely untidy.

**Detection.** Not computable. Look up each distinct code's fully specified name
(`scripts/lookup_codes.py`), compare it to the `CodeMeaning` the batch records, and
record a verdict per `(CodeValue, CodeMeaning)` pair:

| Verdict | Meaning |
|---|---|
| `WRONG_ANATOMY` | The code names materially different anatomy. An error. |
| `NARROWER_OR_BROADER` | Right region, wrong granularity. |
| `SPELLING` | Hyphenation or wording only. |

Split them, because they need different responses: `WRONG_ANATOMY` needs a per-segment
human decision, `SPELLING` needs a find-and-replace.

**From the manual batch** — 151 segments across 32 codes; 67 `WRONG_ANATOMY`:

| Code | Denotes | Recorded as |
|---|---|---|
| `128583004` | mesenteric **vein** | "Mesentric" (sic) |
| `10200004` | liver | "Large bowel" |
| `80248007` | left breast | "Left iliac bone" |
| `75397005` | preductal region of aortic arch | "Preaortic lymph node" |
| `91394001` | retroperitoneal lymph node | five different meanings across 23 segments |

Two patterns worth naming in any report:

- **Which field is wrong varies.** In most cases above the code is wrong. In one —
  `21974007` "tongue" recorded as "Submandibular lymph node" — the code was right and
  the meaning wrong. A consumer that systematically trusted either field would get
  some segments wrong.
- **One label scattered across many codes reads like a carry-over bug.** "Peritoneal
  deposit" had a correct home (`94627008`, 26 segments) *and* appeared under six
  unrelated codes for 10 more. Six codes for one label is not a coding choice; it is a
  code left over from the previous segment.

---

## 2. The same code used with conflicting meanings

**Severity: High.** **Layer: computed — run this first.**

A `CodeValue` used elsewhere in the batch with a different `CodeMeaning`. Fully
computed, no curation, highest yield: **46% of segments (897/1,961) in the manual
batch**, across 25 codes.

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

Of 599 series touching an ambiguous code in the manual batch, 497 were in the last
category. Letting those raise a series' severity turns the triage list into a list of
everything. See "not a work queue" in `reporting.md`.

Some ambiguity is cosmetic — the same anatomy written two ways (`Left adrenal gland` /
`Left adrenal`, `Right axillary` / `Right **axilary**`). Six of the 25 codes, 147
segments. Tag these `COSMETIC_VARIANT` and rank them Low; they are a normalisation
task, not a re-coding task.

This check is what produces the shortlist that issues 1 and 3 then judge.

---

## 3. Laterality inverted or uncoded

**Severity: High** for an inversion, **Medium** for uncoded. **Layer: curated.**

| Verdict | Meaning |
|---|---|
| `INVERTED` | The code names the **opposite side** from the `CodeMeaning`. A factual error nothing in the object can detect. |
| `UNCODED` | The code carries no side, but the meaning states one — the laterality exists only as free text. |

**From the manual batch:** one inversion (`110634007`, *right* uterine adnexa, recorded
as "Left adnexa") and 24 uncoded. The inversion was **absent from the DICOM-derived
terminology table**, so no automated sweep found it — a human comparing codes to FSNs
did. That single segment is the best argument for doing step 4 of the workflow by hand.

### The root cause is pre-coordination

Both failure modes come from one choice: expressing laterality by **pre-coordination**,
picking a code that already names the side. DICOM's post-coordinated alternative is
**`AnatomicRegionModifierSequence` (0008,2220)**, baseline CID 2, laterality from
**CID 244** — `7771000` Left, `24028007` Right, `66459002` Unilateral, `51440002`
Bilateral. No object in the manual batch populated it; the attribute was absent from
the export schema entirely.

Where no pre-coordinated code exists for a side, the laterality falls into free text.
Where two similar codes exist, picking the wrong one flips the side silently.

**State this as a practical argument, not a conformance violation.** PS3.3 §10.5
defines the modifier mechanism and the laterality context groups but does not require
post- over pre-coordination.

**Contrast — the AI batch got this right**: 26,428 of its segments carried Left, Right
or "Right and left" in `SegmentedPropertyTypeModifierCodeSequence` (0062,0011). A
reviewer should check *which* modifier sequence a batch uses: the type modifier and the
anatomic-region modifier are different attributes and either may be the one in play.

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

**From the manual batch:** one segment, empty but for a single code value with no
scheme designator — breaking four Type 1 attributes and supplying the fifth
incompletely. Its NULL `SegmentNumber` was also the one row for which
`(SOPInstanceUID, SegmentNumber)` was not a key; say so when you claim that key.

**Exclude Background segments first** on labelmap deliveries, or this check fires on
every object.

---

## 5. SegmentedPropertyType repeats the category

**Severity: Low.** **Layer: computed.**

`SegmentedPropertyTypeCodeSequence` (0062,000F) set to the same code as
`SegmentedPropertyCategoryCodeSequence` (0062,0003), so the type adds nothing beyond
the category. Conformant, just uninformative.

**From the manual batch:** three segments, both set to `49755003` "Morphologically
abnormal structure". The rest were coded properly — `52988006` "Lesion" (1,516) and
`59441001` "Structure of lymph node" (442).

Report the correct majority alongside the three, or the finding reads as bigger than
it is.

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

**From the manual batch:** absent from 730 of 1,316 objects, `NO` on the rest; only 320
of the 730 had more than one segment.

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

**From the manual batch:** 564 of 1,256 distinct UIDs shared, covering 1,268 segments —
442 longitudinal, 84 unclear within-study, 38 seed-and-lesion.

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

**The evidence, since PS3.16 §6 does not state the rule**: all 11 "Entire" codes in the
manual batch were **absent** from the DICOM-derived terminology table, while the
structure counterpart was **present** for every one that had a findable equivalent.
Argue it that way — from DICOM's own code set — rather than asserting a rule.

**Separate the substitutable from the undecidable.** In that batch, 111 of 142 segments
had a drop-in structure equivalent; 31 across five codes ("Entire cervical lymph node",
"Entire iliac lymph node", "Entire internal mammary lymph node", "Entire bone of
spine", "Entire abdomen") had none, and for those the segment may also be at the wrong
granularity. Only the first group is a find-and-replace.

Watch for codes with **two problems at once**: `181616008` "Entire peritoneal cavity"
was both the entire flavour *and* used for "Peritoneal deposit" under issue 1.

---

## Reporting the codes nothing could verify

Not an issue, but a deliverable that must accompany the others: the anatomic codes your
terminology table does not carry, with how many segments depend on each.

In the manual batch that was **77 codes over 1,080 segments** — more than half. No
automated check verified any of them, so the clean bill from the computed checks did
not extend to them, and the report had to say so explicitly.

See "The coverage trap" in `SKILL.md` and `references/terminology.md`.
