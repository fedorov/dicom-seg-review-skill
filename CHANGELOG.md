# Changelog

## 1.5.0 — 2026-09-16

One new check, and it is the first one here that does not look for a defect.

- **Issue 18 - the segmentation's grid against the segmented series'.** For every
  `(segmentation series, referenced series)` pair, `scripts/seg_geometry.py` asks
  whether the segmented series is available - in IDC, or in a directory of the source
  images - and, where it is, whether the two are sampled the same way: `Rows` and
  `Columns`, `PixelSpacing`, `ImageOrientationPatient`, `SpacingBetweenSlices` and
  `SliceThickness`. **Low throughout, and never a conformance verdict.** PS3.3 A.51.1
  constrains the Frame of Reference (that is issue 17), not the sampling, so a
  segmentation resampled to an isotropic grid or cropped to a bounding box is
  conformant - the finding is a documented property of the delivery, and the report
  gives it its own section rather than a place in the defect list. Folded into the
  triage list with `seg_checks.py --geometry` as `GEOMETRY_DIFFERS`,
  `SOURCE_NOT_IN_IDC` and `NO_REFERENCED_SERIES`, all Low.
- **IDC is the fourth external authority**, through
  [idc-index](https://github.com/ImagingDataCommons/idc-index), public and
  unauthenticated. Three resolvers, tried in order per series, and the output records
  which one answered: `--files` (exact, and the only free one); the IDC series-level
  indices (existence, collection, and for CT, MR and PT the grid); and `--probe`,
  which reads instance headers from IDC's open bucket by ranged HTTPS GET, ~16 KB
  each. **`--probe orientation` is worth its cost**: no IDC index carries
  `ImageOrientationPatient`, and without it the dimension that decides whether two
  arrays are even indexed alike is never compared. `--probe full` reads every
  instance and *measures* the slice spacing from the positions - a sample cannot,
  since the smallest gap in half the slices is about twice the true spacing, and
  `SliceThickness` is nominal and is not a substitute.
- **Two absences are reported, and they matter more than any mismatch.** A referenced
  series IDC does not hold; and an object that names no segmented *series* at all.
  The second is commoner than it sounds - `ReferencedSeriesSequence` (0008,1115) is
  the only series-level link a SEG has, and a batch recording its derivation per SOP
  instance has none, which leaves issues 17 and 18 both silent. The new
  `sourceImageReferenceLevel` column separates `SERIES` from `INSTANCE_ONLY` (the
  link is there, per instance, and is conformant) and `NONE` (there is no link at
  all). Reporting `INSTANCE_ONLY` as a missing reference would blame the producer for
  a conformant object.
- **Eight new columns in the per-segment table**, 61 to 69: `Rows`, `Columns`,
  `PixelSpacing`, `SliceThickness`, `SpacingBetweenSlices`, `ImageOrientationPatient`,
  `geometrySource` and `sourceImageReferenceLevel`. The four functional-group
  attributes are read from the Shared groups where the macro is there and from the
  Per-frame groups otherwise; an attribute whose frames **disagree** is left empty
  with `geometrySource = PER_FRAME_VARYING`, because such an object has no one grid
  and reporting the first frame's value would invent a comparison. `sql/01` gains the
  same columns, with a `frameGeometry` CTE for the per-frame half.
- **`sql/18_geometry.sql`.** With `@@IMAGE_TABLE@@` pointed at
  `idc_current.dicom_all`, "is the segmented series available in IDC" *is* the join,
  and the orientation comparison comes for free - `dicom_all` carries
  `ImageOrientationPatient` per instance. The one thing it cannot do is *measure* the
  slice spacing; it uses the source's declared `SpacingBetweenSlices` and is silent
  otherwise.
- **"Not compared" is kept distinct from "matches".** `GEOMETRY_MATCHES` means every
  dimension agreed; `GEOMETRY_MATCHES_PARTIAL` means every dimension that *could* be
  compared agreed. Only the `*_DIFFERS` observations become triage tags - a geometry
  nobody looked at raises nothing, the same rule issue 17 follows for an unresolved
  reference.
- **Spacings are compared with a relative tolerance and orientations as an angle.**
  `7.031003e-01` against a source's `0.7031` is one spacing written twice, and
  `1\0\0\0\1\0` against `1.000000e+000\-2.038648e-010\...` is one plane; exact
  comparisons would report a batch of false mismatches. Defaults: 0.001 relative,
  0.1 degrees, both settable and both to be quoted in the report.
- `make_fixture.py` gives `seg_bowel.dcm` a 2 mm grid over a 1 mm CT, so the fixture
  exercises issue 18. 177 tests, up from 151.

## 1.4.0 — 2026-09-16

The terminology checks now read the sequences that actually carry the anatomy,
three checks are added, and the review table has a contract.

- **Issues 1, 2, 3 and 8 judge three code sequences**, not `AnatomicRegionSequence`
  alone: the anatomic region (Type 3), the segmented property type (Type 1) and the
  segmented property category (Type 1). A producer that puts the organ in the type
  sequence and omits the region is conformant and common - most organ
  segmentations converted with dcmqi or highdicom look like that - and every
  earlier version of this skill reported such a batch clean. Ambiguity is judged
  per `(codeSequence, CodeValue)`; findings carry a `codeSequence` column; a segment
  offending in two sequences is one segment to recode. `lookup_codes.py` and
  `dcmterm.py coverage` resolve all three by default and record `codeSequences` per
  code; `seg_checks.py --sequences` and `--column` narrow them. `sql/02`, `03`, `05`,
  `06`, `07` and `12` open with the same `codes` CTE. Step 1 of the workflow now
  says which sequence carries the anatomy, and both the extractor and
  `seg_checks.py` print it.
- **Issue 15 - category and type against their context groups.**
  `dcmterm.py property` decides, per distinct `(category, type)` pairing, whether
  the category is in CID 7150, whether the type is in CID 7151, and whether the
  type is in the CID that its own category names in CID 7150's "Segmentation
  Property Type Context Group" column. The first two are Baseline (PS3.3 Table
  C.8.20-4), so Low; the third contradicts the category - "Anatomical Structure"
  over a lesion type - and is Medium. CID 7151 is nothing but Include-CID lines,
  so membership is the transitive closure over dcmterms' `context_groups.parquet`,
  which the script now downloads too. `seg_checks.py --property` folds it in.
  Script only; the BigQuery path exports the view and runs it.
- **Issue 16 - Segment Numbers.** PS3.3 C.8.20.2.4: unique within the instance,
  and 1..n by 1 on BINARY and FRACTIONAL objects. Medium. Runs on the unfiltered
  rows, since a 0 on a BINARY object is the defect and a LABELMAP's 0 is not.
  `sql/16_segment_numbers.sql`.
- **Issue 17 - Frame of Reference.** PS3.3 A.51.1: "If the referenced images have
  a defined Frame of Reference, the Segmentation Instance shall have the same
  Frame of Reference." Medium, with `REFERENCED_SERIES_MISSING` for a dangling
  reference. Needs the referenced series resolved: always on BigQuery through
  `@@IMAGE_TABLE@@`, with `--resolve-referenced` on the file and DICOMweb paths -
  which is now implemented for DICOMweb, where 1.3.0 documented it but did not
  do it. Silent where unresolved, and the report must say so.
  `sql/17_frame_of_reference.sql`.
- **The review table has a documented header**, in `references/terminology.md` and
  `templates/review.csv`, with an optional `codeSequence` column. `seg_checks.py`
  refuses a table missing a required column and names the expected header, and
  reports how many review rows matched no segment. Before this a wrong header
  joined nothing and reported nothing.
- **The per-segment table is the same 61 columns on all three paths.** It was not:
  the SQL view lacked `Collection`, the script lacked `referencedInstanceCount` and
  `referencedSeriesCount`, and the two spelled `SoftwareVersions` differently.
  Added `referencedFrameOfReferenceUID` and `referencedSeriesFound`.
- **CSV booleans are read case-insensitively.** A BigQuery export writes `true`,
  the extractor `True`; `seg_checks.py` compared against the latter, so on an
  exported CSV Background segments were never excluded.
- **`dciodvfy_check.py` fails loudly on a build without `-allpffgitems`.** A
  dicom3tools build predating the flag prints "unrecognized option" and validates
  nothing, and the runner then reported every object clean. It now stops, names
  the binary, and prefers the `dciodvfy` installed beside the running interpreter
  over one found earlier on `PATH`.
- `scripts/make_fixture.py` writes a five-file synthetic delivery with known
  defects, for checking an installation end to end. `requirements.txt` lists
  every optional dependency. `cielab.py --help` no longer raises.
- SKILL.md is rewritten at about two thirds the length: the description no longer
  enumerates every check, the workflow carries its own commands, and the per-issue
  traps live only in the catalogue. Issue 3's citation of the modifier context
  group now reads "Defined CID 2" in the catalogue as it did in terminology.md.
- 42 more tests, 151 in all. The counts in the 1.2.0 and 1.3.0 entries below are
  corrected to what the suite actually gained.

## 1.3.0 — 2026-09-16

Four checks about how a delivery is *encoded* and *presented*, rather than coded.

- **Issue 11 — empty frames, and segments with no voxels.** `scripts/seg_encoding.py`
  reads the pixel data and reports the all-zero frames a BINARY object kept, with the
  fraction and the bytes they cost. Omitting them is what `dcmqi` and `highdicom` do
  by default and nothing in PS3.3 requires it, so this is Low and the *measurement* is
  the argument. A segment whose every frame is empty is Medium and a different
  finding: the object declares an annotation that is not there. PS3.3 C.8.20.2.3.3
  permits it for LABELMAP explicitly — "despite the inefficiency of encoding unused
  information".
- **The bit-packing trap, which makes a naive version wrong.** PS3.5 §8.1: with
  `BitsAllocated` 1 in Native Format "the individual Frames are not padded... a frame
  other than the first frame may start in the middle of a byte". Slicing `PixelData`
  at byte boundaries reads a neighbour's pixels, so an empty frame whose predecessor
  ended mid-byte reads as full. The check masks a bit range instead — and does the
  opposite for encapsulated frames, which *are* byte-aligned, one per fragment
  (PS3.5 A.4.13). Both directions are pinned by tests.
- **Issue 12 — compression.** The transfer syntax distribution, and what deflate would
  actually have saved, **measured** on each object's own pixel data rather than
  asserted. Two deflates exist and are not the same: Deflated Explicit VR Little
  Endian (`1.2.840.10008.1.2.1.99`, the whole Data Set) and Deflated Image Frame
  Compression (`1.2.840.10008.1.2.8.1`, per frame), the latter named by PS3.5 §8.2.16
  as intended for exactly these bilevel objects. Recommending it has a cost worth
  stating: pydicom 3.0.1 does not know that UID at all, so `seg_encoding.py` splits
  the fragments and inflates them itself.
- **Lossy compression is the one High finding here** — PS3.3 C.8.20.2.2, "BINARY or
  LABELMAP Segmentation Instances should not be lossy compressed" — and the verdict
  comes from the **transfer syntax**, never from `LossyImageCompression` (0028,2110),
  which the same section requires to be `01` when the *source images* were lossy.
  Reading it the other way manufactures a High finding out of a correct object.
- **Issue 13 — the recommended display colour.** What colours a delivery assigns, and
  where two *different* structures in one object share one, which is what makes a
  viewer draw two organs identically. Also the absent, the malformed, the
  not-permitted (PS3.3 C.8.20.2 forbids it on a PALETTE COLOR LABELMAP) and the
  inconsistent — one structure drawn several ways across the batch.
- **Decided in CIELab, rendered only for humans.** `scripts/cielab.py` implements the
  PS3.3 C.10.7.1.1 scaling, ΔE\*ab, and the sRGB conversion that inverts PixelMed's
  (D50, the ICC PCS, which is what dcmqi follows). The findings depend on the scaling
  alone, so they are exact; a swatch is the producer's intent. `--color-delta-e`
  defaults to 10 because 2.3 — the classic JND — is for large flat patches, not small
  scattered overlays at partial opacity.
- **Colour previews in the Markdown report.** `seg_checks.py` writes
  `issue13_color_palette.md` with an SVG swatch per row, plus the swatch files. An SVG
  referenced relatively is the one approach that renders everywhere: GitHub strips
  `style`, refuses `data:` image sources, and supports its `#RRGGBB` chips only in
  issues and pull requests, not in committed Markdown. The hex is always printed as
  text beside it, for the same reason a UID is printed beside its link.
- **Issue 14 — an automatic segmentation that does not say what made it.**
  `SegmentAlgorithmName` (0062,0009) is **Type 1C**, "Required if Segment Algorithm
  Type is not MANUAL", so its absence there is a conformance violation (Medium). The
  missing *version* beside it is not: that lives in
  `SegmentationAlgorithmIdentificationSequence` (0062,0007), Type 3, which carries the
  Algorithm Identification Macro (PS3.3 Table 10-19) — name, version, source, and
  `AlgorithmNameCodeSequence`, the closest DICOM has to a model identifier. Low, and
  still the reason nobody can say which model version produced a delivery.
  `ManufacturerModelName` is not a substitute; on a converted segmentation it names
  the converter. It is a cross-check instead.
- **Thirteen columns added to the per-segment table**, identically on all three access
  paths, plus `sql/14_recommended_color.sql` and `sql/15_algorithm_identification.sql`
  and the new tags in `sql/12`. Issues 13 and 14 run everywhere; issues 11 and 12
  join issue 9 as objects-only, and every reference now says which of the six tags a
  given path cannot produce — silence in a triage CSV looks the same whether a check
  passed or never ran.
- 41 more tests, including the mid-byte frame boundary, the encapsulated opposite, the
  sRGB round trip against independently computed values, and same-structure colour
  sharing *not* being a finding. The suite still needs no network, no dicom3tools and
  no fixture files.

## 1.2.0 — 2026-09-15

The review now validates the objects, not only their metadata table.

- **Issue 9 — IOD conformance, via `dciodvfy`.** `scripts/dciodvfy_check.py` runs
  David Clunie's validator over a delivery (`pip install dicom3tools`) and normalises
  its output into the per-segment shape everything else uses, so a Type 1 violation
  lands on a segment in the triage list rather than in a log. Three CSVs: one row per
  message, one row per distinct defect (what the report quotes), one row per object.
  `seg_checks.py --iod` folds the verdict in as `IOD_ERROR` / `IOD_WARNING`.
- **`-new`, `-allpffgitems` and `-filename` are always passed**, and the reasons are
  documented because each is a way to miss findings. `-new` supplies the attribute
  path that makes segment attribution possible at all. Without `-allpffgitems`,
  dciodvfy checks only the **first** per-frame functional group item — verified by
  breaking the last frame of a three-frame object and getting a silent pass. It costs
  about 10× (~4 ms per frame item), which is why the runner parallelises across files.
- **The exit status is documented as unusable**: 1 means "IOD errors *or* unreadable
  file", 0 means "clean *or* warnings only", and everything goes to stderr.
- **A boundary statement, in SKILL.md and the catalogue.** dciodvfy validates
  structure, not semantics — confirmed by planting nonexistent codes in
  `AnatomicRegionSequence` and `SegmentedPropertyTypeCodeSequence` and getting silence.
  Issues 1, 2, 3 and 8 are outside its reach, so a clean run is not a statement about
  the coding. Nothing it reports is ever High: a message *is* the detection.
- **The skill is no longer metadata-only, and now says so.** dciodvfy reads `PixelData`
  and checks its length against Rows × Columns × Frames × BitsAllocated, catching a
  truncated object or one claiming more frames than it carries — which every other
  check in the review passes. Class `BAD_VALUE_LENGTH`. The description, the overview
  and the scope section are reworded: the boundary is the **voxel values**, not the
  pixel data. Nothing here still interprets what was segmented.
- **Issue 10 — retired coding scheme designators** (`SRT`, `SNM3`, `SNM`, `99SDM`).
  dciodvfy reports these as a Warning; the review promotes them, because
  `lookup_codes.py` skips any non-`SCT` scheme and `dcmterm.py` does not count `SRT` as
  private. A batch coded in SRT therefore reports near-total coverage gap and zero
  verified codes, which looks like exotic anatomy and is not. Computed from the
  per-segment table, so unlike issue 9 it runs on all three access paths —
  `sql/13_retired_coding_scheme.sql` is the BigQuery form. The repair is a re-coding,
  not a rename: `T-62000` becomes `10200004`.
- **What each access path can and cannot check** is now stated in all three access
  references and in `sql/12`: a triage list built on BigQuery carries no IOD tag, and
  that silence is "not checked", not "conformant".
- Report provenance extends to the validator build alongside the two terminology
  sources.
- 25 more tests, including the `dciodvfy -new` grammar pinned against captured real
  output — so a dicom3tools build that changes the message format fails the suite
  rather than silently parsing to nothing. The suite still needs neither network nor
  dicom3tools.

## 1.1.0 — 2026-09-15

Checking a batch against the code set DICOM itself uses now needs nothing but a
download — no BigQuery table, no credentials.

- `scripts/dcmterm.py` — the DICOM code set from
  [fedorov/dcmterms](https://github.com/fedorov/dcmterms), published as Parquet from
  PS3.16's context groups, downloaded and cached locally. Four commands: `coverage`
  (the coverage-gap deliverable), `lookup` (what DICOM records for a code, and in which
  CIDs), `search` (find a standard code by meaning), `suggest` (structure-flavour
  replacements for the "Entire X" codes).
- **A computed shortlist for issue 1.** Comparing the batch's `CodeMeaning` against
  DICOM's own catches `10200004` "Large bowel" — DICOM's liver code — with no human in
  the loop. It reaches only the covered half of a batch and says that the two disagree,
  not which is wrong, so it shortens the review rather than replacing it.
- **Issue 8's evidence is reproducible.** `suggest` reports both halves of the argument
  — the "Entire" code absent from DICOM's set, the structure flavour present — in the
  column names `sql/07` expects.
- **Provenance in the report.** Every run prints the DICOM edition and extraction date;
  `references/reporting.md` now requires both terminology sources to be cited by
  version, since "not in DICOM's code set" is a claim about one edition.
- `sql/03` and `references/access-bigquery.md` keep the BigQuery path, giving the
  `bq load` command for the public Parquet.
- 13 more tests, run against a stub table so the suite still needs no network.

## 1.0.0 — 2026-09-15

First release.

- `SKILL.md` — the review method: the computed/curated split, the workflow and its
  ordering, the severity scale, and the traps.
- Eight checks catalogued with DICOM references and severity rationale
  (`references/issue-catalogue.md`).
- Three access paths, all converging on one per-segment table: BigQuery metadata table,
  DICOMweb, local files.
- `scripts/seg_attributes.py` — extraction from local files or DICOMweb.
- `scripts/seg_checks.py` — the computed checks and the per-series triage roll-up.
  Standard library only.
- `scripts/lookup_codes.py` — fully specified names from `tx.fhir.org`, with mechanical
  detection of the SNOMED "Entire X" flavour.
- `scripts/sql/01`–`12` — the same checks as BigQuery templates, numbered in run order.
- 30 tests over the extraction and check logic.
