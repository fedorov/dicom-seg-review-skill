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
## <one section per issue, numbered by discovery>
## Suggested follow-up   <- ordered by severity, each item actionable
## Per-series triage list
## Reproducing
```

### Open with what was reviewed, not with findings

> Written 2026-09-02, against the SEG population of `<table>`: **1,316 SEG objects,
> 1,961 segments**, all produced by highdicom 0.22.0, all `BINARY`, all
> `SegmentAlgorithmType = MANUAL`.

Someone who has never seen the data can read the rest after that sentence. Give the
producer, the segmentation type, the algorithm type, and the counts of objects,
segments, series and patients. State the scope explicitly — "SEG only; RTSTRUCT
problems are in <other document>" — so absence of a finding is not read as absence of
a problem.

### Summary table, in severity order

| # | Severity | Problem | Scale | Query |
|---|---|---|---|---|

Sections numbered by discovery, table sorted by severity, and say so, or a reader
will assume the numbering is meaningful. Follow the table with **one sentence naming
the real problem** — in both batches it was "the anatomic region coding is the problem;
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
> `03_codes_not_in_dcmterm.sql` against the new data and re-checking what they surface.
> Every other query is fully computed and will follow the data.

Without that paragraph the next person runs the whole set against a new batch and
trusts stale verdicts.

## The per-series triage list

One row per SEG series, sorted worst-first, CSV in version control.

| Column | |
|---|---|
| `PatientID`, `StudyInstanceUID`, `SeriesInstanceUID` | First three, so the row is identifiable |
| `issues` | Semicolon-separated tags, most severe first |
| `worstSeverity` | High / Medium / Low / None |
| `segmentCount` | |
| `segmentsNeedingRecode` | **The work queue** — segments needing a per-segment human decision |
| `offendingCodes` | The code and meaning for each such segment |
| `SeriesDescription`, `viewer_url` | |

Sort by `worstSeverity`, then `segmentsNeedingRecode` descending. The series needing
most work are then the first rows, which is the only property that makes a
1,316-row CSV usable.

Tags, each traceable to one issue query:

```
LATERALITY_INVERTED        High      issue 3
ANATOMY_CONFLICT           High      issue 1
CODE_SELF_INCONSISTENT     High      issue 2
CODE_MEANING_MINORITY      High      issue 2
TRACKINGUID_CROSS_PATIENT  High      issue 7
ENTIRE_CODE_FLAVOUR        Medium    issue 8
LATERALITY_UNCODED         Medium    issue 3
MALFORMED_SEGMENT          Medium    issue 4
TRACKINGUID_AMBIGUOUS      Medium    issue 7
COSMETIC_VARIANT           Low       issue 2
CODE_MEANING_SPELLING      Low       issue 1
TYPE_REPEATS_CATEGORY      Low       issue 5
NO_SEGMENTS_OVERLAP        Low       issue 6
CODE_AMBIGUOUS_ELSEWHERE   context   issue 2 — does NOT raise worstSeverity
```

`scripts/seg_checks.py` and `scripts/sql/12_series_triage.sql` emit the same tags.
`COSMETIC_VARIANT` differs in how it is decided: the SQL reads a curated code list,
while the script offers near-match candidates (`cosmeticCandidate` in
`issue2_ambiguous_code.csv`) for you to confirm. Confirm them before quoting the Low
count as final.

### `CODE_AMBIGUOUS_ELSEWHERE` is not a work queue

Issue 2 is a statement about *codes* measured across the whole population, so attaching
it to a series needs care. Of 599 series using a code that is ambiguous somewhere in
the manual batch:

- **8** used one code two ways inside themselves — `CODE_SELF_INCONSISTENT`
- **102** used a code's minority reading — `CODE_MEANING_MINORITY`
- **497** used only dominant meanings and were unremarkable, implicated solely because
  some other series, often a different patient, used the same code differently

Only the first two are evidence about the series, and only they raise `worstSeverity`.
Let the third raise it and 45% of the batch lands in the work queue for no reason.

Report the breakdown by worst severity — "170 High, 216 Medium, 514 Low, 416 series
with no issue" — so the size of the job is legible.

### Reconcile the counts

The triage list and the report are generated separately and will disagree if a tag is
defined differently in each. Check the arithmetic and explain it where it looks wrong:

> That 119 is 118 `ANATOMY_CONFLICT` series plus the one `LATERALITY_INVERTED` series,
> which carries no anatomy conflict of its own — the two tags do not overlap.

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
