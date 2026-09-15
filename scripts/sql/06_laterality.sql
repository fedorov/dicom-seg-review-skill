#standardSQL

# table-description:
# Issue 3. Segments whose anatomic region code cannot carry the laterality their
# CodeMeaning states (UNCODED, Medium), or states the opposite side (INVERTED,
# High).
#
# One row per affected segment. CURATED - verdicts from @@CODE_REVIEW@@.
#
# ROOT CAUSE, worth reporting separately from the symptom: laterality expressed
# by PRE-coordination, choosing a code that already names the side. DICOM's
# post-coordinated alternative is a general anatomic code plus a modifier -
# AnatomicRegionModifierSequence (0008,2220), baseline CID 2, or
# SegmentedPropertyTypeModifierCodeSequence (0062,0011) - with laterality from
# CID 244: 7771000 Left, 24028007 Right, 66459002 Unilateral, 51440002
# Bilateral. CHECK WHICH MECHANISM THE BATCH USES before reporting: one batch
# populated neither, another carried laterality on 26,428 segments in 0062,0011.
#
# Where no pre-coordinated code exists for a side, the laterality falls into
# free text (UNCODED). Where two similar codes exist, picking the wrong one
# flips the side silently (INVERTED).
#
# State the preference as a PRACTICAL argument, not a conformance violation:
# PS3.3 10.5 defines the modifier mechanism and the laterality context groups
# but does not require post- over pre-coordination.
#
# The one genuine inversion found in the source batch was ABSENT from the
# DICOM-derived code table, so no automated sweep would have caught it. That
# segment is the argument for doing the code review by hand.

SELECT
  # description:
  # DICOM PatientID
  seg.PatientID,
  # description:
  # DICOM StudyInstanceUID of the study containing the segmentation
  seg.StudyInstanceUID,
  # description:
  # DICOM SeriesInstanceUID of the segmentation series
  seg.SeriesInstanceUID,
  # description:
  # DICOM SegmentNumber within its segmentation object
  seg.SegmentNumber,
  # description:
  # DICOM SegmentLabel of the affected segment
  seg.SegmentLabel,
  # description:
  # The anatomic region CodeValue carried by the segment
  seg.AnatomicRegionCodeValue,
  # description:
  # What that code actually denotes
  review.codeActuallyMeans,
  # description:
  # The CodeMeaning the segment records, which contradicts it
  seg.AnatomicRegionCodeMeaning AS meaningRecorded,
  # description:
  # INVERTED when the code states the opposite side - a factual error a consumer
  # cannot detect. UNCODED when the side merely has nowhere coded to live.
  review.verdict AS lateralityProblem,
  # description:
  # Where the verdict came from
  review.reviewSource,
  # description:
  # URL opening the segmentation series in the viewer
  seg.viewer_url
FROM
  `@@SEG_ATTRIBUTES@@` AS seg
JOIN
  `@@CODE_REVIEW@@` AS review
  ON review.CodeValue = seg.AnatomicRegionCodeValue
  AND review.CodingSchemeDesignator = seg.AnatomicRegionCodingSchemeDesignator
  AND review.meaningRecorded = seg.AnatomicRegionCodeMeaning
WHERE
  review.issue = 3
  AND NOT seg.isBackgroundSegment
ORDER BY
  review.verdict,
  seg.AnatomicRegionCodeValue,
  seg.PatientID
