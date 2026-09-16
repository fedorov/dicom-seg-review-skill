#standardSQL

# table-description:
# Issue 2 - severity High. Codes used with more than one CodeMeaning across
# the batch, in any of the three code sequences that carry a segment's
# meaning: AnatomicRegionSequence, SegmentedPropertyTypeCodeSequence and
# SegmentedPropertyCategoryCodeSequence.
#
# One row per (segment, sequence). FULLY COMPUTED - no curation - so this is
# the query to run FIRST against a new delivery. It is typically the
# highest-yield check in the set, and it produces the shortlist that
# 05_anatomy_conflict and 06_laterality then judge by hand.
#
# WHY THREE SEQUENCES. The anatomic region is Type 3 in the Segment
# Description Macro; category and type are Type 1. A producer that puts the
# organ in the type sequence and omits the region is conformant and common,
# and a version of this query that read the region alone reported such a
# batch clean. Ambiguity is judged per (sequence, code): a code used as the
# region on one segment and as the type on another is compared only with
# other uses in the same role.
#
# Consequence worth stating in the report: neither field works as a grouping
# key. Grouping on CodeValue merges segments labelled as different anatomy;
# grouping on CodeMeaning splits segments sharing a code.
#
# `scope` is the column that matters, because ambiguity is a property of a CODE
# measured across the whole population, not of the segment in front of you:
#   SELF_INCONSISTENT   this segment's series uses one code two ways by itself
#   MINORITY_MEANING    this segment uses the code's minority reading
#   DOMINANT_MEANING    this segment uses the code's usual reading and is only
#                       implicated because some OTHER series differs
# Only the first two are evidence about this segment. Letting the third raise a
# series' severity puts most of the batch in the work queue for no reason.

WITH
  # One row per (segment, code sequence). The same CTE, verbatim, opens 03,
  # 05, 06, 07 and 12 - keep them identical.
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
  ),

  codeUsage AS (
    SELECT
      codeSequence,
      CodeValue,
      COUNT(DISTINCT CodeMeaning) AS distinctMeanings
    FROM codes
    GROUP BY codeSequence, CodeValue
    HAVING distinctMeanings > 1
  ),

  dominant AS (
    SELECT codeSequence, CodeValue, CodeMeaning AS dominantMeaning
    FROM (
      SELECT
        codeSequence,
        CodeValue,
        CodeMeaning,
        ROW_NUMBER() OVER (
          PARTITION BY codeSequence, CodeValue
          ORDER BY COUNT(*) DESC, CodeMeaning) AS rn
      FROM codes
      GROUP BY codeSequence, CodeValue, CodeMeaning)
    WHERE rn = 1
  ),

  selfInconsistentSeries AS (
    SELECT DISTINCT SeriesInstanceUID
    FROM (
      SELECT SeriesInstanceUID, codeSequence, CodeValue
      FROM codes
      GROUP BY SeriesInstanceUID, codeSequence, CodeValue
      HAVING COUNT(DISTINCT CodeMeaning) > 1)
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
  # DICOM SegmentLabel of the segment
  codes.SegmentLabel,
  # description:
  # Which code sequence carries the ambiguous code: AnatomicRegion,
  # SegmentedPropertyType or SegmentedPropertyCategory
  codes.codeSequence,
  # description:
  # CodingSchemeDesignator of the ambiguous code
  codes.CodingSchemeDesignator,
  # description:
  # The ambiguous CodeValue
  codes.CodeValue,
  # description:
  # The CodeMeaning this segment records for it
  codes.CodeMeaning,
  # description:
  # The most-used CodeMeaning for this code, in this sequence, across the batch
  dominant.dominantMeaning,
  # description:
  # Number of distinct CodeMeanings this code carries batch-wide, in this sequence
  codeUsage.distinctMeanings,
  # description:
  # Whether this segment is itself suspect (SELF_INCONSISTENT, MINORITY_MEANING)
  # or only implicated by other series (DOMINANT_MEANING)
  CASE
    WHEN codes.SeriesInstanceUID IN (
      SELECT SeriesInstanceUID FROM selfInconsistentSeries)
      THEN 'SELF_INCONSISTENT'
    WHEN codes.CodeMeaning != dominant.dominantMeaning
      THEN 'MINORITY_MEANING'
    ELSE 'DOMINANT_MEANING'
    END AS scope,
  # description:
  # URL opening the segmentation series in the viewer
  codes.viewer_url
FROM
  codes
JOIN codeUsage
  ON codeUsage.codeSequence = codes.codeSequence
  AND codeUsage.CodeValue = codes.CodeValue
JOIN dominant
  ON dominant.codeSequence = codes.codeSequence
  AND dominant.CodeValue = codes.CodeValue
ORDER BY
  CASE scope
    WHEN 'SELF_INCONSISTENT' THEN 0 WHEN 'MINORITY_MEANING' THEN 1 ELSE 2 END,
  codeUsage.distinctMeanings DESC,
  codes.CodeValue,
  codes.codeSequence,
  codes.PatientID
