#standardSQL

# table-description:
# Issue 2 - severity High. Anatomic region codes used with more than one
# CodeMeaning across the batch.
#
# One row per segment. FULLY COMPUTED - no curation - so this is the query to
# run FIRST against a new delivery. It is typically the highest-yield check in
# the set, and it produces the shortlist that 05_anatomy_conflict and
# 06_laterality then judge by hand.
#
# Consequence worth stating in the report: neither field works as a grouping
# key. Grouping on AnatomicRegionCodeValue merges segments labelled as different
# anatomy; grouping on AnatomicRegionCodeMeaning splits segments sharing a code.
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
  codeUsage AS (
    SELECT
      AnatomicRegionCodeValue AS code,
      COUNT(DISTINCT AnatomicRegionCodeMeaning) AS distinctMeanings
    FROM `@@SEG_ATTRIBUTES@@`
    WHERE AnatomicRegionCodeValue IS NOT NULL AND NOT isBackgroundSegment
    GROUP BY code
    HAVING distinctMeanings > 1
  ),

  dominant AS (
    SELECT code, meaning AS dominantMeaning
    FROM (
      SELECT
        AnatomicRegionCodeValue AS code,
        AnatomicRegionCodeMeaning AS meaning,
        ROW_NUMBER() OVER (
          PARTITION BY AnatomicRegionCodeValue
          ORDER BY COUNT(*) DESC, AnatomicRegionCodeMeaning) AS rn
      FROM `@@SEG_ATTRIBUTES@@`
      WHERE AnatomicRegionCodeValue IS NOT NULL AND NOT isBackgroundSegment
      GROUP BY code, meaning)
    WHERE rn = 1
  ),

  selfInconsistentSeries AS (
    SELECT DISTINCT SeriesInstanceUID
    FROM (
      SELECT SeriesInstanceUID, AnatomicRegionCodeValue
      FROM `@@SEG_ATTRIBUTES@@`
      WHERE AnatomicRegionCodeValue IS NOT NULL AND NOT isBackgroundSegment
      GROUP BY SeriesInstanceUID, AnatomicRegionCodeValue
      HAVING COUNT(DISTINCT AnatomicRegionCodeMeaning) > 1)
  )

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
  # DICOM SegmentLabel of the segment
  seg.SegmentLabel,
  # description:
  # The ambiguous anatomic region CodeValue
  seg.AnatomicRegionCodeValue,
  # description:
  # The CodeMeaning this segment records for it
  seg.AnatomicRegionCodeMeaning,
  # description:
  # The most-used CodeMeaning for this code across the whole batch
  dominant.dominantMeaning,
  # description:
  # Number of distinct CodeMeanings this code carries batch-wide
  codeUsage.distinctMeanings,
  # description:
  # Whether this segment is itself suspect (SELF_INCONSISTENT, MINORITY_MEANING)
  # or only implicated by other series (DOMINANT_MEANING)
  CASE
    WHEN seg.SeriesInstanceUID IN (
      SELECT SeriesInstanceUID FROM selfInconsistentSeries)
      THEN 'SELF_INCONSISTENT'
    WHEN seg.AnatomicRegionCodeMeaning != dominant.dominantMeaning
      THEN 'MINORITY_MEANING'
    ELSE 'DOMINANT_MEANING'
    END AS scope,
  # description:
  # URL opening the segmentation series in the viewer
  seg.viewer_url
FROM
  `@@SEG_ATTRIBUTES@@` AS seg
JOIN codeUsage ON codeUsage.code = seg.AnatomicRegionCodeValue
JOIN dominant ON dominant.code = seg.AnatomicRegionCodeValue
WHERE NOT seg.isBackgroundSegment
ORDER BY
  CASE scope
    WHEN 'SELF_INCONSISTENT' THEN 0 WHEN 'MINORITY_MEANING' THEN 1 ELSE 2 END,
  codeUsage.distinctMeanings DESC,
  seg.AnatomicRegionCodeValue,
  seg.PatientID
