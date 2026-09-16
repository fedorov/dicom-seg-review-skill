# Writing the report

Two deliverables. The report explains; the triage CSV is worked from. Neither
substitutes for the other.

## The report

A single Markdown document. Structure that worked:

```
# Metadata problems in <batch>

<one paragraph: what was reviewed, when, how big, what produced it>

## Severity scale        <- define it before using it
## Summary               <- table, severity order, with a query per row
## <one section per issue, by issue number>
## Suggested follow-up   <- ordered by severity, each item actionable
## Per-series triage list
## Reproducing
```

### Open with what was reviewed, not with findings

> Written <date>, against the SEG population of `<table>`: **<n> SEG objects, <n>
> segments**, all produced by <toolkit> <version>, all `<SegmentationType>`, all
> `SegmentAlgorithmType = <type>`.

Someone who has never seen the data can read the rest after that sentence. Give the
producer, the segmentation type, the algorithm type, and the counts of objects,
segments, series and patients. State the scope explicitly — "SEG only; RTSTRUCT
problems are in <other document>" — so absence of a finding is not read as absence of
a problem.

The same sentence is where any **unjudged** codes belong. A code `codes.csv` marks
`UNRESOLVED` was looked up on OLS4 alone, whose release holds active concepts only, so
neither "this code is retired" nor "this code does not exist" has been decided for it —
say how many there are and that the clean bill does not extend to them, exactly as for
the codes dcmterms does not cover.

### Summary table, in severity order

| # | Severity | Problem | Scale | Query |
|---|---|---|---|---|

Sections by issue number, table sorted by severity, and say so, or a reader will
assume the numbering is meaningful. Follow the table with **one sentence naming the
real problem** — usually some form of "the anatomic region coding is the problem;
issues 1, 2, 3 and 8 are all about it, and nothing else here is more than a nuisance".
That sentence is what a busy reader takes away.

### Per issue

- Severity **and why** it has that severity, in consumer terms.
- Scale: codes, segments, series. Keep it distinct from severity.
- A table of the specific offenders, largest first.
- **At least one worked example with a clickable viewer link.** Give the
  StudyInstanceUID as text and make the SeriesInstanceUID the hyperlink — a reader who
  needs to quote it in a bug report needs the text, and one who needs to see it needs
  the link.
- Where relevant, the **root cause**, separated from the symptom (issue 3's
  pre-coordination; issue 1's carried-over codes).
- For issue 9, **aggregate by message, not by object**. A list of 40 000 validator
  messages is not a finding; "every object omits `ContentLabel`" is.
  `issue9_iod_validation_summary.csv` is already one row per distinct defect with the
  instances, series and segments it touches. Pair it with the boundary statement:
  dciodvfy checks structure, so a clean run is not evidence about the coding.
- For issue 11, lead with the **measurement, not the preference**: what fraction of
  frames are empty and what they cost in bytes. Omitting them is conformant and so is
  keeping them, so the number is the whole argument. For issue 12, quote the
  **measured** deflated size beside the delivered one — `seg_encoding.py` compresses
  each object's pixel data to get it — and name the transfer syntax you are
  recommending by UID, with what can read it today.
- For issue 13, **show the colours**. See "Colour, in a Markdown report" below.
- For issue 14, separate the **Type 1C violation** (a missing
  `SegmentAlgorithmName` on a non-MANUAL segment — non-conformant) from the
  **provenance gap** (no `SegmentationAlgorithmIdentificationSequence`, so no
  version — conformant, and still the reason nobody can say which model version
  produced the delivery). A reader who cannot tell which is which will either
  over- or under-react to both.
- For issues 1, 2, 3 and 8, say **which sequence** each finding sits in
  (`codeSequence`), and open the terminology section with which sequence carries
  the anatomy in this batch. A reader sent to `AnatomicRegionSequence` on a batch
  that codes organs as the property type finds an empty column.
- For issue 15, report **pairings**, not segments: one row per distinct
  `(category, type)` is what the producer changes. Keep the Medium
  (`TYPE_OUTSIDE_CATEGORY`, the two Type 1 attributes contradict each other) apart
  from the two Lows (baseline membership, which is not conformance).
- For issue 17, state whether the referenced series were **resolved at all**. An
  empty `referencedFrameOfReferenceUID` column means the check did not run, and
  "no Frame of Reference findings" on such a batch is a false reassurance.
- Any **reassuring negative** the check establishes. "No TrackingUID is shared across
  patients, and none is repeated within a series" bounds the problem and lets a reader
  respond proportionately. A report that lists only what is broken cannot be acted on.

### Follow-up section

Ordered by severity, each item naming the specific thing to change and, where it
matters, **who has to change it**:

> Items 1–4 and 7 need the annotation producer, not a change on our side: the codes and
> identifiers are what was delivered.

That distinction is usually the most consequential sentence in the document.

### Reproducing

The exact commands, and — importantly — **which parts will not follow a new delivery**:

> The review view will **not** follow a new delivery: it encodes judgements about
> specific codes. Re-derive it by running `02_ambiguous_code.sql` and
> `dcmterm.py coverage` (or `03_codes_not_in_dcmterm.sql`) against the new data and
> re-checking what they surface. Every other query is fully computed and will follow
> the data.

**Name every external authority by version**, in the same section — the terminology
sources, each SNOMED server that answered, and the IOD validator:

> Codes were checked against DICOM 2026c (dcmterms, extracted 2026-07-02) and, for
> those DICOM does not carry, against SNOMED CT via `tx.fhir.org` on 2026-09-15 —
> and, where `codes.csv` says `source=ols4`, against SNOMED CT International
> `http://snomed.info/sct/900000000000207008/version/20251017` as EBI OLS4 served it.
> Objects were validated against the IOD with dciodvfy (dicom3tools 20260901).
> Colours within an object were compared in CIELab, calling two confusable below
> 10 dE*ab (`--color-delta-e`).

The colour threshold belongs in that list for the same reason the others do: it is
a choice, not a fact, and a reader who disagrees with it needs to know which value
produced the counts.

All three move. "This code is not in DICOM's code set", "this code is retired" and
"this object does not conform" are claims about a particular edition, or a particular
build, on a particular day, and a reader re-running the review a year later needs to
know which one you saw. `dcmterm.py` and `dciodvfy_check.py` each print the line to
copy.

Without that paragraph the next person runs the whole set against a new batch and
trusts stale verdicts.

## Colour, in a Markdown report

A colour finding a reader cannot see is half a finding. "Segments 3 and 7 share
`43620/34952/32896`" is unarguable and unreadable; a swatch beside it is the
evidence.

**No Markdown renderer agrees on how to show a colour**, and the three obvious
approaches each fail somewhere that matters:

| Approach | Fails because |
|---|---|
| `<span style="background:#c00">` | GitHub strips the `style` attribute. The cell renders as blank text. |
| `<img src="data:image/png;base64,...">` | GitHub refuses `data:` image sources. Broken image. |
| `` `#RRGGBB` `` chip syntax | Works in issues, pull requests and discussions — **not** in a committed `.md` file. |

What survives all of them is a **small SVG file referenced relatively**.
`seg_checks.py` writes one per distinct colour into `findings/swatches/` and
`findings/issue13_color_palette.md` as a paste-ready table:

```markdown
| Colour | Hex | L\*a\*b\* | Segments | Series | Structures |
|---|---|---|---|---|---|
| <img src="swatches/ff0000.svg" width="16" height="16" alt="#ff0000"> | `#ff0000` **shared** | 54.3 / 80.8 / 69.9 | 3 | 2 | Mass / Liver; Neoplasm / Lung |
```

Three rules for using it:

- **Keep the swatches with the report.** The `<img>` paths are relative to the
  Markdown file. Moving the report without `swatches/` leaves broken images.
- **Always print the hex as text in the same row**, as the table above does. A
  swatch that fails to load must still leave the reader with the value, and a
  reader quoting the colour in a bug report needs text, not a picture. This is
  the same rule as "the UID as text, the link on the UID".
- **Say what the swatch is.** It is an sRGB rendering of the stored CIELab
  assuming the D50 white point of the ICC PCS, and writers differ at the margins,
  so it shows the producer's intent rather than proving anything. Every finding
  in issue 13 is decided in CIELab; the script prints that sentence under the
  table for you.

Publish the palette even when nothing is wrong. "What colours does this delivery
assign?" is a question a reader has before any finding, and the answer is one
table.

## The per-series triage list

One row per SEG series, sorted worst-first, CSV in version control.

| Column | |
|---|---|
| `PatientID`, `StudyInstanceUID`, `SeriesInstanceUID` | First three, so the row is identifiable |
| `issues` | Semicolon-separated tags, most severe first. A segment offending in two code sequences counts once |
| `worstSeverity` | High / Medium / Low / None |
| `segmentCount` | |
| `segmentsNeedingRecode` | **The work queue** — segments needing a per-segment human decision |
| `offendingCodes` | The code and meaning for each such segment |
| `SeriesDescription`, `viewer_url` | |

Sort by `worstSeverity`, then `segmentsNeedingRecode` descending. The series needing
most work are then the first rows, which is the only property that makes a CSV of a
few thousand rows usable.

Tags, each traceable to one issue query:

```
LATERALITY_INVERTED        High      issue 3
ANATOMY_CONFLICT           High      issue 1
CODE_SELF_INCONSISTENT     High      issue 2
CODE_MEANING_MINORITY      High      issue 2
TRACKINGUID_CROSS_PATIENT  High      issue 7
LOSSY_COMPRESSED           High      issue 12 - local files only
ENTIRE_CODE_FLAVOUR        Medium    issue 8
LATERALITY_UNCODED         Medium    issue 3
MALFORMED_SEGMENT          Medium    issue 4
RETIRED_CODING_SCHEME      Medium    issue 10
IOD_ERROR                  Medium    issue 9 - local files only
TRACKINGUID_AMBIGUOUS      Medium    issue 7
EMPTY_SEGMENT              Medium    issue 11 - local files only
COLOR_DUPLICATE            Medium    issue 13
COLOR_NOT_PERMITTED        Medium    issue 13
COLOR_MALFORMED            Medium    issue 13
ALGORITHM_NAME_MISSING     Medium    issue 14
TYPE_OUTSIDE_CATEGORY      Medium    issue 15 - needs dcmterm.py property
SEGMENT_NUMBER_DUPLICATE   Medium    issue 16
SEGMENT_NUMBER_NOT_SEQUENTIAL Medium issue 16
FRAME_OF_REFERENCE_MISMATCH Medium   issue 17 - needs the references resolved
REFERENCED_SERIES_MISSING  Medium    issue 17 - needs the references resolved
COSMETIC_VARIANT           Low       issue 2
IOD_WARNING                Low       issue 9 - local files only
CODE_MEANING_SPELLING      Low       issue 1
TYPE_REPEATS_CATEGORY      Low       issue 5
NO_SEGMENTS_OVERLAP        Low       issue 6
EMPTY_FRAMES_RETAINED      Low       issue 11 - local files only
UNCOMPRESSED               Low       issue 12 - local files only
COLOR_CONFUSABLE           Low       issue 13
COLOR_INCONSISTENT         Low       issue 13
COLOR_ABSENT               Low       issue 13
ALGORITHM_UNIDENTIFIED     Low       issue 14
TYPE_NOT_IN_CID            Low       issue 15 - needs dcmterm.py property
CATEGORY_NOT_IN_CID        Low       issue 15 - needs dcmterm.py property
GEOMETRY_DIFFERS           Low       issue 18 - needs seg_geometry.py
SOURCE_NOT_IN_IDC          Low       issue 18 - needs seg_geometry.py
NO_REFERENCED_SERIES       Low       issue 18 - needs seg_geometry.py
CODE_AMBIGUOUS_ELSEWHERE   context   issue 2 — does NOT raise worstSeverity
```

`scripts/seg_checks.py` and `scripts/sql/12_series_triage.sql` emit the same tags,
with three classes of exception, each a check that a given path may not have run:

- The six tags marked *local files only* come from the objects. `IOD_ERROR` /
  `IOD_WARNING` need `dciodvfy` (issue 9); `EMPTY_SEGMENT` / `EMPTY_FRAMES_RETAINED`
  need the pixel data (issue 11); `UNCOMPRESSED` / `LOSSY_COMPRESSED` need the file
  meta group, which a BigQuery export drops and a DICOMweb metadata response does
  not return (issue 12).
- The three issue 15 tags need `dcmterm.py property` and `--property`; the SQL
  roll-up never emits them.
- The two issue 17 tags need the referenced series resolved: always on BigQuery,
  only with `--resolve-referenced` on the other two paths.
- The three issue 18 tags need `seg_geometry.py` and `--geometry`. They are the only
  tags in this list that are **not** defects: see below.

A triage list built without one of these is **silent** about it, not clean.
`seg_checks.py` prints which checks ran; repeat that in the report, because a tag
absent because the check did not run looks identical, in a CSV, to a tag absent
because nothing was wrong.

`COSMETIC_VARIANT` differs in how it is decided: the SQL reads a curated code list,
while the script offers near-match candidates (`cosmeticCandidate` in
`issue2_ambiguous_code.csv`) for you to confirm. Confirm them before quoting the Low
count as final.

### The three issue 18 tags are not defects

`GEOMETRY_DIFFERS`, `SOURCE_NOT_IN_IDC` and `NO_REFERENCED_SERIES` are Low and stay
Low. A segmentation sampled on a different grid from its source is conformant — PS3.3
A.51.1 constrains the Frame of Reference, not the sampling — and whether IDC holds the
segmented series is a fact about the delivery's provenance, not about the object. They
are in the triage list so that whoever picks up a series knows, not so that the list
ranks on them.

Give issue 18 **its own section** in the report, structured as what was compared rather
than what is wrong:

- how the segmented series was resolved, for how many series, and by which resolver —
  `--files`, the IDC index, or `--probe`;
- the proportion of referenced series IDC holds. State this **before** any list: "3 of
  412 are not in IDC" is a finding, "412 of 412 are not" says these are not IDC images;
- how many objects name no segmented series at all, split by `sourceImageReferenceLevel`
  — `INSTANCE_ONLY` is conformant and is not the same as `NONE`;
- a table of the grid mismatches, both grids side by side;
- **which dimensions were never compared.** Without `--probe` or `--files` the
  orientation is one of them, on every row.

### `CODE_AMBIGUOUS_ELSEWHERE` is not a work queue

Issue 2 is a statement about *codes* measured across the whole population, so attaching
it to a series needs care. Split the series touching an ambiguous code three ways:

- those using one code two ways inside themselves — `CODE_SELF_INCONSISTENT`
- those using a code's minority reading — `CODE_MEANING_MINORITY`
- those using only dominant meanings, unremarkable in themselves, implicated solely
  because some other series, often a different patient, used the same code differently

Only the first two are evidence about the series, and only they raise `worstSeverity`.
The third group is normally much the largest; let it raise severity and a large part
of the batch lands in the work queue for no reason.

Report the breakdown by worst severity — "<n> High, <n> Medium, <n> Low, <n> series
with no issue" — so the size of the job is legible.

### Reconcile the counts

The triage list and the report are generated separately and will disagree if a tag is
defined differently in each. Check the arithmetic and explain it where it looks wrong:

> That total is the `ANATOMY_CONFLICT` series plus the one `LATERALITY_INVERTED`
> series, which carries no anatomy conflict of its own — the two tags do not overlap.

A count in a report that a reader cannot reproduce from the CSV costs more trust than
the finding gains.

## Publishing

- The **CSV in version control is the version of record**. If you also import it into
  a spreadsheet for people who will not run a query, say in the report that the sheet
  is a snapshot and must be re-imported when the query is re-run.
- Keep the report, the queries and the CSV in one repository, cross-linked: report →
  query file per issue, query header → report section.
- One issue per query file, and the definitions in the query header rather than in the
  report, so there is a single place to change a definition.
