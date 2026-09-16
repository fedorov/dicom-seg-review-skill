#standardSQL

# table-description:
# Issue 3. Segments whose code cannot carry the laterality their CodeMeaning
# states (UNCODED, Medium), or states the opposite side (INVERTED, High) - in
# the anatomic region, the segmented property type or the segmented property
# category.
#
# One row per affected (segment, sequence). CURATED - verdicts from
# @@CODE_REVIEW@@; a review row with a NULL codeSequence applies to whichever
# sequence carries the pairing.
#
# ROOT CAUSE, worth reporting separately from the symptom: laterality expressed
# by PRE-coordination, choosing a code that already names the side. DICOM's
# post-coordinated alternative is a general anatomic code plus a modifier -
# AnatomicRegionModifierSequence (0008,2220), Defined CID 2, or
# SegmentedPropertyTypeModifierCodeSequence (0062,0011) - with laterality from
# CID 244: 7771000 Left, 24028007 Right, 66459002 Unilateral, 51440002
# "Right and left". CHECK WHICH MECHANISM THE BATCH USES before reporting: a
# pre-coordinating batch populates neither sequence, while a post-coordinating
# one may use either, and 0062,0011 is as likely as 0008,2220.
#
# Where no pre-coordinated code exists for a side, the laterality falls into
# free text (UNCODED). Where two similar codes exist, picking the wrong one
# flips the side silently (INVERTED).
#
# State the preference as a PRACTICAL argument, not a conformance violation:
# PS3.3 10.5 defines the modifier mechanism and the laterality context groups
# but does not require post- over pre-coordination.
#
# EXPECT INVERSIONS TO BE RARE AND EASY TO MISS. An inverted code can sit
# outside the DICOM-derived code table, where no automated sweep reaches it and
# only a human comparing codes to fully specified names finds it. That is the
# argument for doing the code review by hand.

WITH
  # One row per (segment, code sequence) - identical to the CTE in 02.
  codes AS (
    SELECT seg.*, 'AnatomicRegion' AS codeSequence,
      AnatomicRegionCodingSchemeDesignator AS CodingSchemeDesignator,
      AnatomicRegionCodeValue AS CodeValue,
      AnatomicRegionCodeMeaning AS CodeMeaning
    FROM `@@SEG_ATTRIBUTES@@` AS seg
    WHERE AnatomicRegionCodeValue IS NOT NULL AND NOT isBackgroundSegment
    UNION ALL
    SELECT seg.*, 'SegmentedPropertyType',
      SegmentedPropertyTypeCodingSchemeDesignator,
      SegmentedPropertyTypeCodeValue,
      SegmentedPropertyTypeCodeMeaning
    FROM `@@SEG_ATTRIBUTES@@` AS seg
    WHERE SegmentedPropertyTypeCodeValue IS NOT NULL AND NOT isBackgroundSegment
    UNION ALL
    SELECT seg.*, 'SegmentedPropertyCategory',
      SegmentedPropertyCategoryCodingSchemeDesignator,
      SegmentedPropertyCategoryCodeValue,
      SegmentedPropertyCategoryCodeMeaning
    FROM `@@SEG_ATTRIBUTES@@` AS seg
    WHERE SegmentedPropertyCategoryCodeValue IS NOT NULL AND NOT isBackgroundSegment
  )

SELECT
  # description:
  # DICOM PatientID
  codes.PatientID,
  # description:
  # DICOM StudyInstanceUID of the study containing the segmentation
  codes.StudyInstanceUID,
  # description:
  # DICOM SeriesInstanceUID of the segmentation series
  codes.SeriesInstanceUID,
  # description:
  # DICOM SegmentNumber within its segmentation object
  codes.SegmentNumber,
  # description:
  # DICOM SegmentLabel of the affected segment
  codes.SegmentLabel,
  # description:
  # Which code sequence carries the code
  codes.codeSequence,
  # description:
  # CodingSchemeDesignator of the code
  codes.CodingSchemeDesignator,
  # description:
  # The CodeValue carried by the segment
  codes.CodeValue,
  # description:
  # What that code actually denotes
  review.codeActuallyMeans,
  # description:
  # The CodeMeaning the segment records, which contradicts it
  codes.CodeMeaning AS meaningRecorded,
  # description:
  # INVERTED when the code states the opposite side - a factual error a consumer
  # cannot detect. UNCODED when the side merely has nowhere coded to live.
  review.verdict AS lateralityProblem,
  # description:
  # Where the verdict came from
  review.reviewSource,
  # description:
  # URL opening the segmentation series in the viewer
  codes.viewer_url
FROM
  codes
JOIN
  `@@CODE_REVIEW@@` AS review
  ON review.CodeValue = codes.CodeValue
  AND review.CodingSchemeDesignator = codes.CodingSchemeDesignator
  AND review.meaningRecorded = codes.CodeMeaning
  AND (review.codeSequence IS NULL OR review.codeSequence = codes.codeSequence)
WHERE
  review.issue = 3
ORDER BY
  review.verdict,
  codes.CodeValue,
  codes.codeSequence,
  codes.PatientID
