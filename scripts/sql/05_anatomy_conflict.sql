#standardSQL

# table-description:
# Issue 1 - severity High for WRONG_ANATOMY, Medium for the rest.
# Segments whose recorded CodeMeaning disagrees with what the code actually
# denotes, so the code and the meaning cannot both be right - in the anatomic
# region, the segmented property type or the segmented property category.
#
# One row per affected (segment, sequence). CURATED: the verdicts come from
# @@CODE_REVIEW@@ - see 04_code_review_template.sql for why it is curated rather
# than a join against a terminology table, and for how to re-derive it. A
# review row with a NULL codeSequence applies whichever sequence carries the
# pairing; one naming a sequence applies to that sequence only.
#
# `verdict` separates what a reader should act on:
#   WRONG_ANATOMY        the code names materially different anatomy. Errors.
#   NARROWER_OR_BROADER  right region, wrong granularity.
#   SPELLING             hyphenation or wording only.
#
# WHICH FIELD IS WRONG VARIES - usually the code, sometimes the CodeMeaning - so
# the report must say which, per case. A consumer that systematically trusted
# either field would get some segments wrong.
#
# Laterality problems are excluded here and reported by 06_laterality.sql.

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
  # Which code sequence carries the conflicting code
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
  # WRONG_ANATOMY, NARROWER_OR_BROADER or SPELLING - see the header
  review.verdict,
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
  review.issue = 1
ORDER BY
  CASE review.verdict
    WHEN 'WRONG_ANATOMY' THEN 0 WHEN 'NARROWER_OR_BROADER' THEN 1 ELSE 2 END,
  codes.CodeValue,
  codes.codeSequence,
  codes.PatientID
