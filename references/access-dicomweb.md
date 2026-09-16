# Access path: DICOMweb

Use when the segmentations live in a DICOM store you can reach over DICOMweb but there
is no BigQuery metadata export — a Google Cloud Healthcare API store, the IDC public
proxy, dcm4chee, Orthanc.

`scripts/seg_attributes.py --dicomweb` implements what follows; this reference explains
the choices so you can adapt it to a store that behaves differently.

## Requirements

```bash
pip install 'dicomweb-client>=0.59'          # MIT, ImagingDataCommons
pip install 'dicomweb-client[gcp]' google-auth  # only for Healthcare API stores
```

Use `dicomweb-client` rather than hand-rolling QIDO/WADO: it already handles QIDO-RS
pagination, WADO-RS multipart parsing and Google credentials, and those are exactly
the three places a hand-rolled client goes wrong.

Healthcare API stores take Application Default Credentials —
`gcloud auth application-default login`, or a service account on the VM.

## QIDO finds the objects; WADO gets the segments

**This is the one thing to get right.** A QIDO-RS search returns only the attributes
the server chooses to include, and `SegmentSequence` (0062,0002) is never among them.
Searching with `includefield=all` does not reliably change that, and a reviewer who
trusts a QIDO response ends up with an empty catalogue and no error.

Two requests per series:

1. **QIDO-RS** to enumerate the segmentation instances:
   `/instances?SOPClassUID=1.2.840.10008.5.1.4.1.1.66.4`
   Query each SEG SOP class separately (`...66.4` and `...66.7`); a comma-separated
   list of UIDs in one `SOPClassUID` parameter is not portable across servers. Some
   stores do not support searching on `SOPClassUID` at all — fall back to
   `Modality=SEG` and filter client-side on the returned `SOPClassUID` (0008,0016).

2. **WADO-RS metadata** for the full dataset:
   `/studies/{study}/series/{series}/metadata`
   This returns DICOM JSON for every instance in the series **with bulk data excluded**,
   so `PixelData` never crosses the wire — a segmentation's metadata is a few tens of
   kilobytes regardless of how large the object is. One request per *series* rather
   than per instance; most stores hold one SEG instance per series anyway.

Do **not** use `retrieve_instance()` to get the whole Part 10 object. It downloads the
pixel data, which is the entire cost and none of the value.

## DICOM JSON, tersely

WADO-RS metadata comes back as DICOM JSON: tag keys in hexadecimal, a `vr`, and a
`Value` list. Codes are nested sequences (`SQ`), so a segment's anatomic region is

```
"00620002"            SegmentSequence
  → Value[i]
    → "00082218"      AnatomicRegionSequence
      → Value[0]
        → "00080100"  CodeValue
        → "00080102"  CodingSchemeDesignator
        → "00080104"  CodeMeaning
```

Person names arrive as `{"Alphabetic": "..."}` and `Value` is absent entirely for an
empty attribute — distinguish "absent" from "present and empty", because issue 4 turns
on exactly that. The tags this review needs are listed in `scripts/seg_attributes.py`;
`pydicom.Dataset.from_json()` will parse the whole thing if you would rather work with
keywords than hex.

## Scale

One metadata request per series, so a 3,000-series store is 3,000 requests —
comfortably parallelisable (the script uses a thread pool) and a few minutes over a
normal connection. Past roughly ten thousand series, get a BigQuery export instead;
the checks in `seg_checks.py` are in-memory and set-based checks over a CSV stop being
the right shape well before the extraction does.

Cache the extracted CSV. Re-running the checks is cheap; re-extracting is not.

## Endpoints

| Store | Base URL |
|---|---|
| Healthcare API | `https://healthcare.googleapis.com/v1/projects/{p}/locations/{l}/datasets/{d}/dicomStores/{s}/dicomWeb` |
| IDC public proxy | `https://proxy.imaging.datacommons.cancer.gov/current/viewer-only-no-downloads-see-tinyurl-dot-com-slash-3j3d9jyp/dicomWeb` (no auth) |

The script accepts a Healthcare API **resource name**
(`projects/…/dicomStores/…`) as well as a full URL and builds the `dicomWeb` suffix
itself.

## Viewer URLs

Build one per row so report examples are clickable. Pass a pattern with
`{StudyInstanceUID}` and `{SeriesInstanceUID}` placeholders via `--viewer-url`:

```
# OHIF against a Healthcare API store
https://viewer.example.org/projects/{p}/locations/{l}/datasets/{d}/dicomStores/{s}/study/{StudyInstanceUID}?initialSeriesInstanceUID={SeriesInstanceUID}

# OHIF with the store as a query parameter
https://viewer.example.org/viewer?StudyInstanceUIDs={StudyInstanceUID}&gcp=projects/{p}/...

# IDC public viewer
https://viewer.imaging.datacommons.cancer.gov/viewer/{StudyInstanceUID}?SeriesInstanceUID={SeriesInstanceUID}
```

## What you lose relative to BigQuery

- **Collection / project membership**, unless the store carries `ClinicalTrial*`
  attributes. The BigQuery path usually gets it from a separate mapping table.
- **The referenced image series' `Modality` and `BodyPartExamined`**, unless the
  producer copied them into `ReferencedSeriesSequence` — many do not. If the referenced
  series is in the same store, the script can fetch its metadata too (`--resolve-referenced`),
  at one extra request per distinct referenced series.
- **The "attribute absent from the schema" signal.** A BigQuery export tells you
  positively that *no* instance in the batch populates an attribute. Over DICOMweb you
  learn the same thing only by extracting everything and finding the column empty —
  which the extractor reports as a coverage summary at the end. Read it: an attribute
  empty across the whole batch is a finding, not a blank column.

## Issues 9, 11 and 12 cannot run from here

`dciodvfy` validates a Part 10 object, and a DICOMweb metadata response is not one.
Retrieving instances with WADO-RS (`application/dicom`) and writing them to disk makes
the local-files path available — but a SEG's pixel data is the bulk of it, so this is a
real download, not a metadata request.

Decide deliberately and say which you did:

- **Retrieve a sample** — a few objects per producer and per `SegmentationType` — run
  `dciodvfy_check.py` over them, and report issue 9 as sampled, giving the sample size.
  IOD defects are usually systematic, so a sample answers most of the question.
- **Skip it**, and say in the report that IOD conformance was not checked. The
  hand-rolled Type 1 check (issue 4) still runs and covers five attributes of one
  macro; it is not a substitute, and the triage list will carry no `IOD_ERROR` tag
  whether or not the objects are conformant.

The same retrieval decides issues 11 and 12, for two further reasons:

- **The pixel data never crosses the wire.** `retrieve_series_metadata` excludes bulk
  data by design — which is what makes this path affordable — so no frame can be
  tested for emptiness. Issue 11 is unavailable.
- **The file meta group is not in the metadata response.** `TransferSyntaxUID`
  (0002,0010) is a property of how the object was *stored*, and a DICOMweb server may
  well re-encode it on retrieval anyway. Some servers report
  `AvailableTransferSyntaxUID` (0008,3002) in a QIDO response; if yours does, it tells
  you what the server can send, not how the delivery arrived. Issue 12 is unavailable,
  and answering it means retrieving objects with `application/dicom` and running
  `scripts/seg_encoding.py` on them.

Both are cheap once the sample is on disk — one pass, no codec — so a sample retrieved
for `dciodvfy` should be run through `seg_encoding.py` at the same time.

Issues 13 and 14 **do** run here: `RecommendedDisplayCIELabValue`,
`SegmentAlgorithmName` and `SegmentationAlgorithmIdentificationSequence` are all in
the metadata response, and `seg_attributes.py` extracts them. If the extractor's
coverage summary lists them as populated by no instance, that absence is the finding.

