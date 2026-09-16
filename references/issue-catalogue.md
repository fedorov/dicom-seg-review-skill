# Issue catalogue

The eighteen checks. Each gives what the defect is, why it matters, how to detect
it, and what the finding looks like in practice.

Seventeen of them look for defects. Issue 18 does not: it documents how a
segmentation's grid relates to its source's, where neither answer is a failure.

Numbering is stable. Add new issues with the next free number; do not renumber, since
reports cite these.

**Which code sequences the terminology checks read.** Issues 1, 2, 3 and 8, the
coverage gap and the FSN lookup all judge three sequences of the Segment
Description Macro (PS3.3 Table C.8.20-4): `AnatomicRegionSequence` (0008,2218),
Type 3; `SegmentedPropertyTypeCodeSequence` (0062,000F), Type 1; and
`SegmentedPropertyCategoryCodeSequence` (0062,0003), Type 1. A producer that puts
the organ in the type sequence and omits the region is conformant, and most
organ-segmentation deliveries converted with dcmqi or highdicom look like that. A
review that read the region alone reported such a batch clean. Findings carry a
`codeSequence` column saying which one the code was in, and a segment offending in
two sequences is one segment to recode, not two.

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

**Detection.** Group by `(codeSequence, CodeValue)`, keep those with more than one
distinct `CodeMeaning`. Ambiguity is judged per role: a code used as the region on
one segment and as the type on another is compared only with other uses in the
same sequence. Then classify each segment by `scope` — this is the column that
matters:

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
**`AnatomicRegionModifierSequence` (0008,2220)**, Defined CID 2, laterality from
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
Type 1C. (Issue 13 does report a missing recommended colour, as a Low, for a different
reason: not that it is required, but that every consumer then invents one. Keep the two
apart — this check is about conformance.)

**`SegmentAlgorithmName` (0062,0009) is Type 1C, not Type 1**, so it belongs to
issue 14 rather than here: it is required only where `SegmentAlgorithmType` is not
MANUAL.

These are usually few and badly broken — a segment empty but for a single code value
with no scheme designator breaks four Type 1 attributes and supplies the fifth
incompletely. A NULL `SegmentNumber` also breaks the `(SOPInstanceUID, SegmentNumber)`
key; say so when you claim that key.

**Exclude Background segments first** on labelmap deliveries, or this check fires on
every object.

**Where the files are on disk, issue 9 supersedes this.** `dciodvfy` reads the whole
IOD rather than the five attributes of one macro, and finds the same violations plus
every other Type 1, 1C, 2, VM and enumerated-value breach in the object. This check
stays because it is all the BigQuery and DICOMweb paths can run. Where both ran, report
issue 9 and note that issue 4 is its subset, rather than counting the same segment
twice.

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

- `302508007` "Entire colon (body structure)" — the complete organ only
- `71854001` "Colon structure (body structure)" — the parent concept: the organ or any
  part of it

DICOM's context groups draw on the *structure* flavour. A segmentation of part of the
colon coded "Entire colon" asserts more than was segmented.

**Detection.** Mechanical once you have FSNs: flag every code whose fully specified
name begins `Entire `. `scripts/lookup_codes.py` does this.

**The rule is stated, in PS3.16 §8.1.1 "Use of SNOMED Anatomic Concepts"**, so cite it
rather than reconstructing it:

> In general, DICOM uses the anatomic concepts with the term "structure", rather than
> with the term "entire"... Since imaging typically targets both the anatomic feature
> and the area around it, or sometimes just part of the anatomic feature, DICOM usually
> uses "structure" concepts that are more inclusive than the "entire" concepts.

Two things about how it is phrased decide the severity. It is **"in general" and
"usually", not a *shall*** — so this is a Medium, and a report that calls it a
conformance violation overstates. And it is **not applied uniformly**: CID 4031
includes `38266002` "Entire body", so a code's mere presence in a context group does
not settle it.

That is why the per-batch evidence is still worth producing alongside the citation:
show that each "Entire" code the batch uses is **absent** from DICOM's context groups
while its structure counterpart is **present**. The quotation says what DICOM intends;
the table says what DICOM did.

**DICOM's own Code Meanings will not tell you which flavour a code is.** They never
carry the "structure" or "entire" suffix — CID 4031 lists `71854001` as plain "Colon".
Only the code value distinguishes them, which is exactly why this check needs the FSN
lookup and cannot be run on meanings.

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

## 9. The object does not conform to the Segmentation IOD

**Severity: Medium for an Error, Low for a Warning — never High.** **Layer: computed,
by an external validator.**

`dciodvfy` (David Clunie's [dicom3tools](https://github.com/ImagingDataCommons/dicom3tools-python-distributions),
`pip install dicom3tools`) validates an object against the IOD definition in PS3.3:
which modules a Segmentation must carry, which of their attributes are Type 1, 1C, 2 or
3, their value multiplicity, their enumerated values, and whether each frame's
`ReferencedSegmentNumber` names a segment that exists. It is the authority on
structure, and it supersedes issue 4 wherever the files are on disk — issue 4 hand-rolls
five Type 1 attributes from one macro, this reads the whole IOD.

**It also reads the pixel data — this is the one check in the review that does.**
dciodvfy compares `PixelData`'s length against Rows × Columns × Frames ×
BitsAllocated, so it catches a truncated object, or one whose `NumberOfFrames` claims
more frames than it carries:

```
Error - </PixelData(7fe0,0010)> - PixelData has incorrect value length = <98304> \
- expected 131072 dec
```

Class `BAD_VALUE_LENGTH` in `issue9_iod_validation.csv`, with `attributeName` =
`PixelData`. It is a Medium like any other Error — detectable, since any reader
computes the same expected length — but it is the Medium a consumer feels hardest,
because the object will not decode. Nothing else in this review would notice it: every
other check reads metadata only. Call it out separately from the attribute findings.

**Know what it does not do.** dciodvfy validates *structure*, not *semantics*, and it
interprets no voxel values. It does not check `SegmentedPropertyTypeCodeSequence` or
`AnatomicRegionSequence` against any context group — confirmed by planting a
nonexistent code in each and getting silence. Issues 1, 2, 3 and 8 are outside its
reach entirely, and a clean dciodvfy run says nothing whatever about whether the
anatomy is coded correctly. Say that in the report, or "passes the DICOM validator"
will be read as "the coding was checked".

**Severity never reaches High, by construction.** High is reserved for an assertion
that is false and that the object gives no way to detect. A dciodvfy message *is* the
detection. Map its own split straight onto the scale: Error → Medium, Warning → Low,
with the one promotion noted in issue 10.

### Running it

`scripts/dciodvfy_check.py` wraps it, parses the output into the per-segment shape the
rest of the review uses, and rolls it up:

```bash
pip install dicom3tools
python scripts/dciodvfy_check.py --files /path/to/delivery -o findings/
python scripts/seg_checks.py seg_attributes.csv \
    --iod findings/issue9_iod_validation.csv --outdir findings/
```

Three flags matter, and the script passes all three:

- **`-new`.** Without it a message names only the attribute; with it the message carries
  the full path including sequence item indices —
  `</SegmentSequence(0062,0002)[1]/SegmentLabel(0062,0005)>`. That index is what lets a
  message be attributed to a **segment** rather than to a file, which is the whole
  reason the finding can join the per-series triage list. Never run without it.
- **`-allpffgitems`.** By default dciodvfy checks only the **first** item of
  `PerFrameFunctionalGroupsSequence`. A SEG's per-frame defects are rarely in frame 1.
  Verified: delete `SegmentIdentificationSequence` from the last frame of a three-frame
  object and the default run reports nothing at all.
- **`-filename`**, so a batch run's output can be attributed to a file.

### Traps

- **The exit status is not a pass/fail signal.** 1 means "IOD errors **or** the file
  could not be read"; 0 means "clean **or** warnings only". Parse the output. A batch
  triaged on exit status alone silently passes every warning and conflates a corrupt
  file with a non-conformant one.

- **Everything goes to stderr**, including the IOD name and the clean-run output.
  Redirecting only stdout captures nothing.

- **One file per invocation.** dciodvfy takes a single input; there is no batch mode.
  `dciodvfy_check.py` parallelises across files for this reason.

- **`-allpffgitems` costs about 10×.** Measured on a 1000-frame object: 0.39 s default,
  3.9 s with the flag — roughly 4 ms per per-frame item. On a delivery of large
  multi-frame objects that is hours single-threaded. Budget for it; do not drop the flag
  to save the time, because dropping it is what hides the findings.

- **Check which IOD it chose.** dciodvfy prints the Information Object it validated
  against as a bare line — `Segmentation`. If it picked the wrong one, every message
  below it is about the wrong rules. `dciodvfy_check.py` records it per object in
  `iod` and prints the distribution; a batch that is not uniformly `Segmentation` needs
  explaining before anything else in the output is quoted.

- **Labelmap objects (`...66.7`) need a build that knows them.** This one does: it
  requires `SegmentationType` to be `LABELMAP` for that SOP class and reports `BINARY`
  there as an unrecognized enumerated value. An older build predating Labelmap
  Segmentation Storage will not, and will quietly validate against the wrong rules.
  Test yours against a known-good labelmap object before trusting a labelmap delivery.

- **The sequence item index is not the SegmentNumber.** `SegmentSequence[1]` is the
  first *item*, which on a labelmap SEG is the Background segment at `SegmentNumber` 0,
  putting every later index one out. `dciodvfy_check.py` reads the actual
  `SegmentNumber` out of the item rather than assuming.

- **A dangling `ReferencedSegmentNumber` must not be echoed into the SegmentNumber
  column.** The reference not resolving *is* the finding; writing 99 into
  `SegmentNumber` invents a segment 99 that the triage list will then carry.

- **Unrecognised messages.** The parser classifies known message text and files the rest
  as `OTHER` with the full message kept. Read those before quoting any count as
  complete — a new dicom3tools build can add message text the classifier has not seen.

### Reporting it

Aggregate by message, not by object: `issue9_iod_validation_summary.csv` gives one row
per distinct defect with the instances, series and segments it touches, worst first.
A list of 40 000 messages is not a finding; "every object omits `ContentLabel`" is.

Quote the validator version alongside the terminology versions —
`dciodvfy_check.py` prints the line. An IOD definition is a claim about one edition of
PS3.3 as one build of dicom3tools implements it.

---

## 10. Codes recorded under a retired coding scheme designator

**Severity: Medium.** **Layer: computed.**

`SRT`, `SNM3`, `SNM` and `99SDM` are retired designators for the SNOMED family, all
superseded by `SCT`. dciodvfy reports them ("CodingSchemeDesignator is deprecated"), but
only as a **Warning**, and only where the files are on disk. This check is separate from
issue 9, and promoted above dciodvfy's own severity, for two reasons.

**It breaks the terminology work silently.** Everything in this review keyed on
`(CodingSchemeDesignator, CodeValue)` resolves `SCT` and nothing else.
`lookup_codes.py` skips a non-`SCT` designator outright, so no FSN is ever fetched and
issues 1, 3 and 8 have nothing to judge. `dcmterm.py coverage` counts the code as one
DICOM's context groups do not carry, and since `SRT` is not a `99…` designator it is not
separated out as private either. **A batch coded entirely in SRT therefore reports
near-total coverage gap and zero verified codes** — which reads exactly like a batch of
exotic codes and is nothing of the kind. Check the designator distribution before
believing any coverage number.

**It is computable from the per-segment table**, so unlike issue 9 it runs on all three
access paths: `seg_checks.py` always runs it, and `sql/13_retired_coding_scheme.sql` is
the BigQuery form.

**The fix is a re-coding, not a rename.** An SRT code and its SCT equivalent have
different `CodeValue`s — `T-62000` "Liver" becomes `10200004`. Swapping the designator
and keeping the value produces a code that does not exist. Say so explicitly in the
follow-up section, because "replace SRT with SCT" is the obvious wrong reading and it
is the annotation producer who has to do the mapping.

Report the designator distribution across the batch, not just the offending count — a
producer that emits `SRT` in one code sequence normally emits it in all of them, and
that is one change at the source rather than a per-segment repair.

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

---

## 11. Empty frames kept, and segments with no voxels

**Severity: Low** for retained empty frames, **Medium** for a segment that holds
nothing at all. **Layer: computed, but only where the files are on disk.**

A BINARY segmentation carries one frame per segment per slice, and on a typical
lesion most of those slices contain nothing. Those all-zero frames are pure
overhead: for a 512 × 512 object each costs 32 KiB uncompressed, and a
whole-body series can be 90% empty. The preference — near-universal in the
tooling, and what `dcmqi` and `highdicom` (`omit_empty_frames`) do by default —
is to **omit them**, since a frame's position comes from its own Plane Position
(Patient) and its segment from its own `SegmentIdentificationSequence`. Nothing
requires a frame per source image.

**Say plainly that keeping them is conformant.** No text in PS3.3 C.8.20
requires omission, so this is an efficiency argument, not a violation — which is
exactly why it is Low and why the *scale* is the part worth reporting. Give the
fraction of frames that are empty and the bytes they occupy, and the case makes
itself.

**A segment with no voxels anywhere is a different finding**, and Medium: the
object declares a segment, names its anatomy, and then segments nothing. A
consumer counting findings counts one that does not exist. PS3.3 C.8.20.2.3.3
permits it explicitly for LABELMAP — "the Segment Sequence can describe Segments
that are not actually present in the pixel data, e.g., to allow for re-use of a
common Segment description across multiple instances, despite the inefficiency
of encoding unused information" — so report it as what it is: permitted, and
almost never what was meant.

**Detection.** `scripts/seg_encoding.py --files <dir>`. Frames are scanned as
bytes, not decoded into an array, so a whole delivery is affordable.

### The trap that makes a hand-rolled version wrong

**BINARY frames are not byte-aligned.** PS3.5 §8.1: "In a Multi-frame Image with
a Bits Allocated (0028,0100) of 1 that is transmitted in Native Format, the
individual Frames are not padded, therefore successive bits are packed into
bytes or words... I.e., a frame other than the first frame may start in the
middle of a byte or word."

So for `Rows × Columns` not a multiple of 8, slicing `PixelData` at
`frame * rows * columns / 8` reads a neighbour's pixels, and an empty frame
whose predecessor ended mid-byte reads as non-empty. The check must mask a bit
range, not slice a byte range. `frame_is_empty` does; `payload_is_empty` handles
the opposite case, since an **encapsulated** frame is its own fragment and *does*
start on a byte boundary (PS3.5 A.4.13).

Two more:

- **`NumberOfFrames` is a claim, not a measurement.** Scan what the pixel data
  actually holds and report the disagreement (`SHORT_PIXEL_DATA`) rather than
  iterating to a frame that is not there. dciodvfy reports the same thing from
  the other direction, as `BAD_VALUE_LENGTH` — issue 9.
- **An empty *segment* on a LABELMAP object is a question about values, not
  frames.** One frame carries every segment, so "segment 3 is empty" means the
  value 3 appears nowhere in the pixel data.

---

## 12. Compression: none, the wrong kind, or lossy

**Severity: Low** where an object is uncompressed, **High** where it is lossy
compressed. **Layer: computed, from the file meta group — files on disk only.**

Segmentation pixel data is the most compressible data in DICOM: long runs of
zeros, one bit per pixel. Deflate routinely takes 90% or more off it, and the
Standard offers two ways to apply it, which are **not** the same thing:

| Transfer Syntax | UID | What it compresses |
|---|---|---|
| Deflated Explicit VR Little Endian | `1.2.840.10008.1.2.1.99` | the **entire Data Set**, as one stream (PS3.5 A.5) |
| Deflated Image Frame Compression | `1.2.840.10008.1.2.8.1` | **each frame**, into its own encapsulated fragment (PS3.5 A.4.13) |

The second is the one the Standard designed for these objects. PS3.5 §8.2.16:
"One application of Deflated Image Frame Compression is the lossless compression
of single bit (bilevel, Bits Allocated (0028,0100) == 1) Segmentation images."
It also keeps the metadata readable without inflating anything, and lets a
consumer fetch one frame.

**Report the transfer syntax distribution and the measured saving**, not a
recommendation. `seg_encoding.py` deflates each object's pixel data and records
what it would have cost — an argument a producer can act on, where "you should
compress" is not. Quote it as the optimistic end of the range: compressing the
whole stream at once beats compressing frames separately.

**The cost of recommending `1.2.840.10008.1.2.8.1` is real, so state it.**
pydicom 3.0.x (checked through 3.0.2) does not know the UID at all —
`UID.is_encapsulated` raises "UID is not a transfer syntax" and `dcmwrite` refuses
it — though `dcmread` still parses such a file by falling back to Explicit VR
Little Endian. Check what the
consumers of this delivery can actually read before telling a producer to switch;
`1.2.840.10008.1.2.1.99` is older and more widely supported, and for a
segmentation its pixel data compresses nearly as well.

### Lossy compression is the High finding

PS3.3 C.8.20.2.2: "It is not advisable to lossy compress a Segmentation
Instance. In particular, BINARY or LABELMAP Segmentation Instances should not be
lossy compressed." A lossy-compressed segmentation has voxel values that are not
the ones that were segmented, the object does not say which moved, and no
consumer can recover them. That is the definition of High in this skill.

**Do not read `LossyImageCompression` (0028,2110) as "this object was lossy
compressed".** The same section requires it to be `01` when any of the **source
images** was lossy compressed — so a perfectly intact segmentation of a lossy
JPEG CT carries `01`. Take the verdict from the **transfer syntax**, and keep
(0028,2110) beside it as context. Reading it the other way manufactures a High
finding out of a correct object.

### Why this cannot run on the other access paths

The Transfer Syntax UID lives in the file meta group (0002,0010), which a Google
Healthcare API BigQuery export does not carry and a DICOMweb metadata response
does not return. `sql/01` keeps the column and fills it with NULL so the
per-segment table is the same shape everywhere — which means a BigQuery-based
review is **silent** about issues 11 and 12, exactly as it is about issue 9. Say
so.

---

## 13. Recommended display colour: absent, duplicated, or indistinguishable

**Severity: Medium** where two structures in one object share a colour,
**Low** otherwise. **Layer: computed.**

`RecommendedDisplayCIELabValue` (0062,000D) is how a segmentation tells a viewer
what colour to draw each segment. It is Type 3, three unsigned shorts, scaled per
**PS3.3 C.10.7.1.1**:

```
L*     0x0000 -> 0.0     0xFFFF -> 100.0
a*, b* 0x0000 -> -128.0  0x8080 -> 0.0    0xFFFF -> 127.0
```

Six things are worth deciding, and only the first two need a human to care:

| `colorIssue` | Severity | Meaning |
|---|---|---|
| `DUPLICATE_IN_OBJECT` | Medium | Two segments of **different** structures in one object, same colour. The viewer draws both overlays identically. |
| `CONFUSABLE_IN_OBJECT` | Low | ... or within the ΔE\*ab threshold of each other. |
| `NOT_PERMITTED` | Medium | Present on a LABELMAP object whose Photometric Interpretation is PALETTE COLOR. PS3.3 C.8.20.2: it "shall not be present". |
| `MALFORMED` | Medium | Present, but not three unsigned shorts. A VM violation. |
| `INCONSISTENT_ACROSS_BATCH` | Low | One structure drawn in several colours across the delivery. Not wrong anywhere in particular; two series cannot be read side by side. |
| `ABSENT` | Low | No colour. Type 3, so conformant — but every consumer then invents one, and two consumers invent different ones. |

**Two segments of the same structure sharing a colour is the point of a colour
convention, not a defect.** The check compares structures — the type code and the
anatomic region code, falling back to the label only where neither exists — so
consistency is never reported as duplication. Get this wrong and the finding is
every multi-object delivery in existence.

**Decide in CIELab; convert to RGB only to show a human.** ΔE\*ab (CIE76) is a
plain Euclidean distance once the components are unscaled, and it depends on
nothing but C.10.7.1.1, so "these two are the same colour" is exact. Rendering
one as sRGB needs a white point the Standard only implies: the note says the
encoding is "the same form ... as used for the PCS in ICC Profiles", the ICC PCS
is D50, and PixelMed's `ColorUtilities` — which `dcmqi` follows — converts sRGB
(D65) → XYZ → D50 → Lab on the way in. `scripts/cielab.py` inverts exactly that.
Writers differ at the margins, so a swatch is the producer's intent, not
evidence.

**The threshold is a judgement; `--color-delta-e` exists so it can be argued
with.** 2.3 ΔE\*ab is the classic just-noticeable difference for two large flat
patches side by side. Segment overlays are small, scattered, and drawn at partial
opacity over grey, so the threshold that matters in a viewer is well above that;
the default is 10. Say which value you used.

**Publish the palette even when nothing is wrong.** `issue13_color_palette.csv`
is one row per distinct colour with the structures using it — it answers "what
colours does this delivery assign", which is a question a reader has before any
finding. `seg_checks.py` also writes it as Markdown with a swatch per row; see
`references/reporting.md`.

---

## 14. An automatic segmentation that does not say what made it

**Severity: Medium** for the conformance violation, **Low** for the rest.
**Layer: computed.**

`SegmentAlgorithmName` (0062,0009) is **Type 1C** in the Segmentation Image
Module: *"Required if Segment Algorithm Type (0062,0008) is not MANUAL"*
(PS3.3 C.8.20.2). An `AUTOMATIC` or `SEMIAUTOMATIC` segment with no algorithm
name is non-conformant, full stop — that is `NAME_MISSING`, and it is the only
one of the four reasons below that is a violation.

The other three are about whether the name is worth anything:

| Reason | What it means |
|---|---|
| `NAME_MISSING` | (0062,0009) absent on a non-MANUAL segment. **Type 1C violation.** |
| `NAME_UNINFORMATIVE` | A name that identifies nothing: "unknown", "AI", "segmentation" — or the name of the toolkit that *wrote* the object rather than the model that segmented it. |
| `NO_IDENTIFICATION` | No `SegmentationAlgorithmIdentificationSequence` (0062,0007), so no version, no source, no coded model identity. |
| `NO_VERSION` | (0062,0007) present, but `AlgorithmVersion` (0066,0031) — Type 1 *within* it — is empty. |

### Where the model belongs

(0062,0009) is a bare string with nowhere to put a version. The structured home
is **`SegmentationAlgorithmIdentificationSequence` (0062,0007)**, Type 3, which
includes the Algorithm Identification Macro (**PS3.3 Table 10-19**):

| Attribute | Tag | Type | Carries |
|---|---|---|---|
| `AlgorithmFamilyCodeSequence` | (0066,002F) | 1 | what kind of algorithm, baseline CID 7162 |
| `AlgorithmName` | (0066,0036) | 1 | the model's name |
| `AlgorithmVersion` | (0066,0031) | 1 | **which version produced these annotations** |
| `AlgorithmNameCodeSequence` | (0066,0030) | 3 | a manufacturer's code for a specific algorithm — the closest DICOM has to a model identifier |
| `AlgorithmSource` | (0024,0202) | 3 | who produced it |
| `AlgorithmParameters` | (0066,0033) | 3 | how it was configured |

PS3.3 notes that (0062,0007) replaced the older
`SegmentSurfaceGenerationAlgorithmIdentificationSequence` (0066,002D) in this
module, "since not all segmentation algorithms involve surface generation" — so a
producer emitting the retired one is aiming at the wrong attribute.

Being Type 3, its absence is conformant, which is why `NO_IDENTIFICATION` is Low.
Say in the report what it costs anyway: **a delivery without it cannot be
attributed to a model version**, so "which model produced these annotations, and
were they re-run after the fix" has no answer inside the data.

### Manufacturer is not a substitute — it is a cross-check

`Manufacturer` (0008,0070), `ManufacturerModelName` (0008,1090) and
`SoftwareVersions` (0018,1020) describe the equipment that **wrote the object**,
which on a converted segmentation is `dcmqi` or `highdicom`, not the model. The
per-segment table carries them beside the algorithm columns for one reason: the
disagreement is informative. A batch whose `ManufacturerModelName` is an
inference toolkit while every `SegmentAlgorithmType` says `MANUAL` is
misdescribing how it was made, and that belongs in the report even though no
query returns it.

---

## 15. Category or type code outside its context group

**Severity: Medium** where the type contradicts its own category, **Low** where a
code is merely outside the baseline group. **Layer: computed**, from dcmterms'
context-group tables; runs on every access path.

PS3.3 Table C.8.20-4 gives `SegmentedPropertyCategoryCodeSequence` (0062,0003)
**BCID 7150** "Segmentation Property Category" and
`SegmentedPropertyTypeCodeSequence` (0062,000F) **BCID 7151** "Segmentation
Property Type". Both are **Baseline**, so a code outside them is permitted, and
membership alone is Low. CID 7150 does one more thing: its "Segmentation Property
Type Context Group" column names, per category, the CID its types come from —
`91723000` "Anatomical Structure" → CID 7192, `49755003` "Morphologically Abnormal
Structure" → CID 7194, and so on. A type that is a segmentation property type but
not one of the CID its own category names contradicts the category: "Anatomical
Structure" over a lesion type says the segment is an organ and a neoplasm at once.
A consumer filtering on category then gets the wrong answer, so that one is Medium.

| `propertyIssue` | Severity | Meaning |
|---|---|---|
| `TYPE_OUTSIDE_CATEGORY` | Medium | The type is in CID 7151 but not in the CID its category names |
| `TYPE_NOT_IN_CID` | Low | The type is in no segmentation property type context group |
| `CATEGORY_NOT_IN_CID` | Low | The category is not one of CID 7150's nine |

**Detection.** `scripts/dcmterm.py property seg_attributes.csv`, then
`seg_checks.py --property`. CID 7151 is nothing but Include-CID lines — nine of
them, each including more (CID 7192 alone includes 25) — so membership is the
transitive closure over dcmterms' `context_groups.parquet`, and the category → type
link comes from `coded_entries.parquet`. Private (`99…`) schemes are not judged;
retired ones are issue 10's and are noted rather than tagged. There is no SQL form:
export the per-segment view and run the script.

**Report the pairings, not the segments.** One row per distinct `(category, type)`
is what the producer fixes, and the CSV is shaped that way; the triage list carries
the tags per series.

---

## 16. Segment Numbers not unique, or not 1..n

**Severity: Medium.** **Layer: computed.**

PS3.3 C.8.20.2.4: "Segment Number (0062,0004) shall be unique within each
Instance", and where Segmentation Type is BINARY or FRACTIONAL it "shall start at a
Value of 1, and increase monotonically by 1". LABELMAP numbers are pixel values and
need not be consecutive; a LABELMAP's 0 is the Background.

| `segmentNumberProblem` | Meaning |
|---|---|
| `DUPLICATE` | A number appears twice in one object. The `(SOPInstanceUID, SegmentNumber)` key every check relies on is broken for that object. |
| `NOT_SEQUENTIAL` | A BINARY or FRACTIONAL object whose distinct numbers are not exactly 1..n — a gap, or a 0. |

**Detection.** `seg_checks.py` runs it on every invocation, on the **unfiltered**
rows: this is the one check that must see the Background segment, since a 0 on a
BINARY object is the defect. `sql/16_segment_numbers.sql` is the BigQuery form. The
order of items in the Segment Sequence is not recoverable from a table, so
"increase by 1" is checked as "the numbers are 1..n"; an object numbered 2, 1
passes here and is dciodvfy's to report.

Detectable by any reader, hence Medium — but a consumer that indexes frames by
segment number will silently merge two segments on a `DUPLICATE`, so call that one
out separately.

---

## 17. Frame of Reference differs from the referenced series

**Severity: Medium.** **Layer: computed, where the referenced series is resolved.**

PS3.3 A.51.1 (Segmentation IOD Description): "If the referenced images have a
defined Frame of Reference, the Segmentation Instance shall have the same Frame of
Reference." A viewer decides whether to overlay a segmentation on an image by
comparing exactly these two UIDs. A mismatch is a segmentation nobody can display
on its source: non-conformant and unusable, but detectable, so Medium.

| `frameOfReferenceProblem` | Meaning |
|---|---|
| `FRAME_OF_REFERENCE_MISMATCH` | The referenced series was found and its `FrameOfReferenceUID` differs |
| `REFERENCED_SERIES_MISSING` | `ReferencedSeriesSequence` names a series the store or directory does not hold — the images were never delivered, or the UID is wrong |

**Detection needs the referenced series.** The per-segment table carries
`referencedFrameOfReferenceUID` and `referencedSeriesFound`. BigQuery fills them
from `@@IMAGE_TABLE@@` on every run; the file and DICOMweb extractors fill them
only with `--resolve-referenced`, which reads one instance of each referenced
series. Where they are empty the check is **silent**, and the report must say
"not checked" rather than let the silence read as a match. `seg_checks.py` prints
which it was; `sql/17_frame_of_reference.sql` is the BigQuery form.

Only the first item of `ReferencedSeriesSequence` is compared;
`referencedSeriesCount` says when there were more.

**This check is silent on an object that names no referenced series at all**, which is
commoner than it sounds: `ReferencedSeriesSequence` is the only series-level link a SEG
has, and a batch that records its derivation per SOP instance instead has none. Issue
18 reports that case as `NO_REFERENCED_SERIES`, with `sourceImageReferenceLevel`
separating "the link is recorded per instance" from "there is no link at all".

---

## 18. The segmentation's grid against the segmented series'

**Severity: Low throughout, and not a conformance verdict at all.**
**Layer: computed, where the segmented series is resolved.**

Two questions about the relationship between a segmentation and the images it was
drawn on, neither of which has a pass or a fail:

1. **Is the segmented series there at all?** — both whether the object names one, and
   whether whoever holds the images has it.
2. **Where it is there, do the two grids agree?** — `Rows` and `Columns`, in-plane
   `PixelSpacing`, `ImageOrientationPatient`, `SpacingBetweenSlices`, `SliceThickness`.

**A different grid is not a defect.** PS3.3 A.51.1 constrains the Frame of Reference —
that is issue 17, and it *is* a rule — but it says nothing about the sampling. A
segmentation resampled to an isotropic grid, or cropped to a bounding box around the
organ, is conformant, and is often what the producer intended. What a mismatch costs
is that a consumer cannot overlay or compare the two voxel-wise without resampling
first, and resampling a label map means choosing an interpolator, which is a decision
somebody has to make knowingly. So the finding is a **documented property of the
delivery**, and the report should present it as a table of what was compared, never in
the defect list. Do not let it raise a series' severity above Low.

```bash
python scripts/seg_geometry.py seg_attributes.csv -o findings/issue18_geometry.csv \
    --probe orientation
python scripts/seg_checks.py seg_attributes.csv --outdir findings/ \
    --geometry findings/issue18_geometry.csv
```

### The two absences, which matter more than any mismatch

| `geometryObservation` | Meaning |
|---|---|
| `NO_REFERENCED_SERIES` | The object names no segmented **series**. Read `sourceImageReferenceLevel` next to it. |
| `SOURCE_NOT_IN_IDC` | It names one, and IDC does not hold it. |

`ReferencedSeriesSequence` (0008,1115) is the **only series-level link a SEG has**.
Where it is absent, `sourceImageReferenceLevel` separates two very different
situations, and conflating them is the trap here:

| `sourceImageReferenceLevel` | Meaning |
|---|---|
| `SERIES` | `ReferencedSeriesSequence` names a `SeriesInstanceUID`. The normal case, and the only one issues 17 and 18 can follow. |
| `INSTANCE_ONLY` | No such sequence, but the object *does* name its source SOP instances — Source Image (0008,2112), Referenced Image (0008,1140), or the per-frame Derivation Image group. **The derivation is recorded**; recovering the series from it needs an instance-level index, which neither idc-index nor a series-level DICOMweb query provides. Common in older converted batches. |
| `NONE` | Nothing in the object connects it to any image. |

`INSTANCE_ONLY` is conformant and is not a producer error — say so, rather than
reporting "no referenced series" as though the link were missing. What it costs is
concrete and worth stating: issues 17 and 18 are both **silent** on those objects, and
a viewer that resolves the source by series will not find it.

**`SOURCE_NOT_IN_IDC` has to be interpreted before it is reported.** For a delivery
derived from IDC it means the images cannot be re-fetched and the segmentation cannot
be reproduced. For a delivery over private images it means nothing at all. Report the
*proportion* first — "3 of 412 referenced series are not in IDC" is a finding; "412 of
412 are not" is a statement that these are not IDC images — and only then the list.

### The comparison, and what each dimension needs

`seg_geometry.py` resolves the segmented series three ways, in this order per series.
They differ in what they can reach, and the CSV says which was used in
`sourceResolvedBy`:

| Resolver | Reaches | Cost |
|---|---|---|
| `--files DIR` | everything, exactly, including the measured slice spacing | free |
| IDC index (default) | existence, collection, and — for CT, MR and PT only — `Rows`, `Columns`, `PixelSpacing`, `SliceThickness` | one parquet download, cached |
| `--probe orientation` | **`ImageOrientationPatient`**, plus the rest from the object itself | one ~16 KB ranged HTTPS GET per series |
| `--probe full` | the **measured** slice spacing | one per instance |

**No IDC index carries `ImageOrientationPatient`.** Without `--probe` or `--files`, the
orientation is reported `ORIENTATION_NOT_COMPARED` — which is the dimension most worth
having, because an orientation mismatch means the two arrays are not even indexed the
same way. Budget one `--probe orientation` pass.

**A sample cannot measure a slice spacing.** The spacing is the median gap between
consecutive `ImagePositionPatient` projections onto the slice normal, and the smallest
gap in a random half of the slices is about twice the true spacing. `--probe full`
therefore reads every instance or none; a series above `--probe-limit` falls back to
one instance and its spacing stays `NOT_COMPARED`. `SliceThickness` is **not** a
substitute: it is nominal, and says nothing about gaps or overlap between slices.

### Reading the result

| `geometryObservation` | |
|---|---|
| `GRID_SIZE_DIFFERS` | `Rows` / `Columns` differ |
| `PIXEL_SPACING_DIFFERS` | in-plane spacing differs beyond the relative tolerance |
| `ORIENTATION_DIFFERS` | the two planes are more than `--orientation-tolerance` apart |
| `SLICE_SPACING_DIFFERS` | `SpacingBetweenSlices` differs from the source's |
| `SLICE_THICKNESS_DIFFERS` | `SliceThickness` differs from the source's |
| `SEG_GEOMETRY_PER_FRAME` | the segmentation's own frames disagree, so it has no one grid |
| `SEG_GEOMETRY_ABSENT` | it carries no Pixel Measures or Plane Orientation at all |
| `*_NOT_COMPARED`, `SOURCE_GEOMETRY_UNAVAILABLE` | **silence, not a match** |
| `GEOMETRY_MATCHES` | every dimension agreed |
| `GEOMETRY_MATCHES_PARTIAL` | every dimension that *could* be compared agreed |

The two `MATCHES` values are split for the reason the rest of this skill splits
"unchecked" from "clean": a row saying `GEOMETRY_MATCHES` beside
`ORIENTATION_NOT_COMPARED` reads as "the grids agree" when the orientation was never
looked at.

### Traps

- **Compare spacings with a *relative* tolerance, never exactly.** Producers write the
  same number differently: `7.031003e-01` against a source's `0.7031` is one spacing
  written twice, and an exact comparison reports a batch of false mismatches. The
  default is 0.001 relative, which accepts that and still separates 1.0 mm from 1.25 mm.
- **Compare orientations as an angle, not as text.** `1\0\0\0\1\0` and
  `1.000000e+000\-2.038648e-010\...` are the same plane. `seg_geometry.py` reports the
  larger of the row and column rotations in `orientationAngleDegrees`; a slice normal
  alone would miss an in-plane rotation, which changes which voxel is which.
- **An attribute whose instances disagree is dropped, not averaged.** A source series
  with two in-plane spacings has no spacing to compare against, and reporting the first
  instance's would compare the segmentation against a grid that does not exist. IDC's
  `volume_geometry_index` says the same thing at series level, and
  `sourceVolumeIsRegular` carries it through.
- **The segmentation's own geometry may be per-frame.** Pixel Measures and Plane
  Orientation are legal in either functional groups macro. `geometrySource` says which
  it was; `PER_FRAME_VARYING` means the object itself has no single grid, which is a
  more interesting finding than any mismatch.
- **Only the first referenced series is compared.** `MULTIPLE_REFERENCED_SERIES` says
  when there were more; the per-segment table carries only the first.

### On BigQuery

`sql/18_geometry.sql`, with `@@IMAGE_TABLE@@` pointed at
`bigquery-public-data.idc_current.dicom_all` — on that path "is the segmented series
available in IDC" *is* the join, and the orientation comes for free because
`dicom_all` carries `ImageOrientationPatient` per instance. The one comparison the SQL
cannot make is the **measured** slice spacing; it uses the source's declared
`SpacingBetweenSlices` where there is one, and is silent otherwise. Give it the same
`@@SPACING_TOLERANCE@@` the script was given, or the two will disagree about which
series are tagged.
