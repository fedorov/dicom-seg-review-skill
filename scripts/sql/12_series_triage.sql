#standardSQL

# table-description:
# Per-series triage roll-up: one row per segmentation series, flagging which
# issues affect it, sorted worst-first so it can be handed to whoever fixes the
# annotations. Export to CSV; that CSV is the version of record.
#
# Reads @@SEG_ATTRIBUTES@@ and @@CODE_REVIEW@@. The per-issue detail lives in
# the numbered files here - this one only rolls their verdicts up to the series.
# Definitions and counts belong in those files, not here.
#
# Fill entireFlavourCodes and cosmeticCodes below from 07_entire_code_flavour.sql
# and from your reading of 02_ambiguous_code.sql. Everything else is computed.

WITH
  review AS (
    SELECT * FROM `@@CODE_REVIEW@@`
  ),

  # From 07_entire_code_flavour.sql - codes whose FSN begins "Entire ".
  entireFlavourCodes AS (
    SELECT * FROM UNNEST(['302508007', '181279003', '181757009']) AS code
  ),

  # Codes whose several CodeMeanings are the same anatomy written two ways.
  # A judgement, so it is listed rather than computed; scripts/seg_checks.py
  # offers near-match candidates to start from.
  cosmeticCodes AS (
    SELECT * FROM UNNEST(['12003004']) AS code
  ),

  # Issue 2. Ambiguity is a property of a CODE across the whole population, so
  # the tags below separate what is evidence about a series from what is not.
  ambiguousCodes AS (
    SELECT AnatomicRegionCodeValue AS code
    FROM `@@SEG_ATTRIBUTES@@`
    WHERE AnatomicRegionCodeValue IS NOT NULL AND NOT isBackgroundSegment
    GROUP BY code
    HAVING COUNT(DISTINCT AnatomicRegionCodeMeaning) > 1
  ),

  dominantMeaning AS (
    SELECT code, meaning AS dominant
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
  ),

  # Issue 7, the unclear case only: a UID reused between two series of one study.
  ambiguousTrackingUIDs AS (
    SELECT TrackingUID
    FROM `@@SEG_ATTRIBUTES@@`
    WHERE TrackingUID IS NOT NULL AND NOT isBackgroundSegment
    GROUP BY TrackingUID
    HAVING
      COUNT(*) > 1
      AND COUNT(DISTINCT PatientID) = 1
      AND COUNT(DISTINCT StudyInstanceUID) = 1
      AND COUNT(DISTINCT SeriesInstanceUID) > 1
      AND LOGICAL_AND(UPPER(SeriesDescription) NOT LIKE '%SEED POINT%')
  ),

  # Issue 7, the serious case: one identifier spanning two patients. Absent from
  # both batches this was built against, and a real problem if it appears.
  crossPatientTrackingUIDs AS (
    SELECT TrackingUID
    FROM `@@SEG_ATTRIBUTES@@`
    WHERE TrackingUID IS NOT NULL AND NOT isBackgroundSegment
    GROUP BY TrackingUID
    HAVING COUNT(DISTINCT PatientID) > 1
  ),

  flagged AS (
    SELECT
      seg.*,
      EXISTS (SELECT 1 FROM review r
              WHERE r.CodeValue = seg.AnatomicRegionCodeValue
                AND r.meaningRecorded = seg.AnatomicRegionCodeMeaning
                AND r.verdict = 'INVERTED') AS isLateralityInverted,
      EXISTS (SELECT 1 FROM review r
              WHERE r.CodeValue = seg.AnatomicRegionCodeValue
                AND r.meaningRecorded = seg.AnatomicRegionCodeMeaning
                AND r.issue = 1
                AND r.verdict IN ('WRONG_ANATOMY', 'NARROWER_OR_BROADER'))
        AS isAnatomyConflict,
      EXISTS (SELECT 1 FROM review r
              WHERE r.CodeValue = seg.AnatomicRegionCodeValue
                AND r.meaningRecorded = seg.AnatomicRegionCodeMeaning
                AND r.verdict = 'UNCODED') AS isLateralityUncoded,
      seg.SeriesInstanceUID IN (
        SELECT SeriesInstanceUID FROM selfInconsistentSeries)
        AS isSelfInconsistent,
      seg.AnatomicRegionCodeMeaning != dominantMeaning.dominant
        AS isMinorityMeaning,
      seg.AnatomicRegionCodeValue IN (SELECT code FROM ambiguousCodes)
        AS isCodeAmbiguousSomewhere,
      seg.AnatomicRegionCodeValue IN (SELECT code FROM entireFlavourCodes)
        AS isEntireFlavour,
      seg.SegmentNumber IS NULL
        OR seg.SegmentLabel IS NULL
        OR seg.SegmentedPropertyCategoryCodeValue IS NULL
        OR seg.SegmentedPropertyTypeCodeValue IS NULL
        OR seg.SegmentAlgorithmType IS NULL AS isMalformed,
      seg.TrackingUID IN (SELECT TrackingUID FROM ambiguousTrackingUIDs)
        AS isTrackingAmbiguous,
      seg.TrackingUID IN (SELECT TrackingUID FROM crossPatientTrackingUIDs)
        AS isTrackingCrossPatient,
      EXISTS (SELECT 1 FROM review r
              WHERE r.CodeValue = seg.AnatomicRegionCodeValue
                AND r.meaningRecorded = seg.AnatomicRegionCodeMeaning
                AND r.verdict = 'SPELLING') AS isSpellingVariant,
      seg.AnatomicRegionCodeValue IN (SELECT code FROM cosmeticCodes)
        AS isCosmetic,
      seg.SegmentedPropertyTypeCodeValue = seg.SegmentedPropertyCategoryCodeValue
        AS isTypeRepeatsCategory,
      seg.SegmentsOverlap IS NULL AS isNoSegmentsOverlap
    FROM
      `@@SEG_ATTRIBUTES@@` AS seg
    LEFT JOIN
      dominantMeaning ON dominantMeaning.code = seg.AnatomicRegionCodeValue
    WHERE
      NOT seg.isBackgroundSegment
  ),

  perSeries AS (
    SELECT
      PatientID,
      StudyInstanceUID,
      SeriesInstanceUID,
      ANY_VALUE(SeriesDescription) AS SeriesDescription,
      ANY_VALUE(viewer_url) AS viewer_url,
      COUNT(*) AS segmentCount,
      COUNTIF(isLateralityInverted) AS nLateralityInverted,
      COUNTIF(isAnatomyConflict) AS nAnatomyConflict,
      COUNTIF(isSelfInconsistent) AS nSelfInconsistent,
      COUNTIF(isMinorityMeaning) AS nMinorityMeaning,
      COUNTIF(isCodeAmbiguousSomewhere) AS nCodeAmbiguousSomewhere,
      COUNTIF(isLateralityUncoded) AS nLateralityUncoded,
      COUNTIF(isEntireFlavour) AS nEntireFlavour,
      COUNTIF(isMalformed) AS nMalformed,
      COUNTIF(isTrackingAmbiguous) AS nTrackingAmbiguous,
      COUNTIF(isTrackingCrossPatient) AS nTrackingCrossPatient,
      COUNTIF(isSpellingVariant) AS nSpellingVariant,
      COUNTIF(isCosmetic) AS nCosmetic,
      COUNTIF(isTypeRepeatsCategory) AS nTypeRepeatsCategory,
      LOGICAL_OR(isNoSegmentsOverlap) AS anyNoSegmentsOverlap,
      STRING_AGG(
        DISTINCT
        IF(isAnatomyConflict OR isLateralityInverted,
           CONCAT(AnatomicRegionCodeValue, ' "', AnatomicRegionCodeMeaning, '"'),
           NULL),
        '; ' ORDER BY IF(isAnatomyConflict OR isLateralityInverted,
           CONCAT(AnatomicRegionCodeValue, ' "', AnatomicRegionCodeMeaning, '"'),
           NULL)) AS offendingCodes
    FROM flagged
    GROUP BY PatientID, StudyInstanceUID, SeriesInstanceUID
  )

SELECT
  # description:
  # DICOM PatientID
  PatientID,

  # description:
  # DICOM StudyInstanceUID of the study holding the segmentation
  StudyInstanceUID,

  # description:
  # DICOM SeriesInstanceUID of the segmentation series
  SeriesInstanceUID,

  # description:
  # Semicolon-separated issues affecting this series, most severe first. Each
  # tag has a query of its own:
  #   LATERALITY_INVERTED      (High)   06_laterality.sql
  #   ANATOMY_CONFLICT         (High)   05_anatomy_conflict.sql
  #   CODE_SELF_INCONSISTENT   (High)   02_ambiguous_code.sql
  #   CODE_MEANING_MINORITY    (High)   02_ambiguous_code.sql
  #   TRACKINGUID_CROSS_PATIENT (High)  11_tracking_uid.sql
  #   ENTIRE_CODE_FLAVOUR      (Medium) 07_entire_code_flavour.sql
  #   LATERALITY_UNCODED       (Medium) 06_laterality.sql
  #   MALFORMED_SEGMENT        (Medium) 08_malformed_segment.sql
  #   TRACKINGUID_AMBIGUOUS    (Medium) 11_tracking_uid.sql
  #   COSMETIC_VARIANT         (Low)    02_ambiguous_code.sql
  #   CODE_MEANING_SPELLING    (Low)    05_anatomy_conflict.sql
  #   TYPE_REPEATS_CATEGORY    (Low)    09_type_repeats_category.sql
  #   NO_SEGMENTS_OVERLAP      (Low)    10_segments_overlap_absent.sql
  #   CODE_AMBIGUOUS_ELSEWHERE (context) a code this series uses is used with
  #     another meaning by some OTHER series. No evidence this series is wrong,
  #     so it does not raise worstSeverity. NOT A WORK QUEUE.
  ARRAY_TO_STRING(
    ARRAY(
      SELECT tag FROM UNNEST([
        IF(nLateralityInverted > 0, 'LATERALITY_INVERTED', NULL),
        IF(nAnatomyConflict > 0, 'ANATOMY_CONFLICT', NULL),
        IF(nSelfInconsistent > 0, 'CODE_SELF_INCONSISTENT', NULL),
        IF(nMinorityMeaning > 0, 'CODE_MEANING_MINORITY', NULL),
        IF(nTrackingCrossPatient > 0, 'TRACKINGUID_CROSS_PATIENT', NULL),
        IF(nEntireFlavour > 0, 'ENTIRE_CODE_FLAVOUR', NULL),
        IF(nLateralityUncoded > 0, 'LATERALITY_UNCODED', NULL),
        IF(nMalformed > 0, 'MALFORMED_SEGMENT', NULL),
        IF(nTrackingAmbiguous > 0, 'TRACKINGUID_AMBIGUOUS', NULL),
        IF(nCosmetic > 0, 'COSMETIC_VARIANT', NULL),
        IF(nSpellingVariant > 0, 'CODE_MEANING_SPELLING', NULL),
        IF(nTypeRepeatsCategory > 0, 'TYPE_REPEATS_CATEGORY', NULL),
        IF(anyNoSegmentsOverlap, 'NO_SEGMENTS_OVERLAP', NULL),
        IF(nCodeAmbiguousSomewhere > 0, 'CODE_AMBIGUOUS_ELSEWHERE', NULL)]) AS tag
      WHERE tag IS NOT NULL),
    '; ') AS issues,

  # description:
  # Highest severity among this series' issues: High, Medium, Low or None.
  # CODE_AMBIGUOUS_ELSEWHERE is excluded - it is a property of a code across the
  # population, not evidence about this series.
  CASE
    WHEN nLateralityInverted > 0 OR nAnatomyConflict > 0
      OR nSelfInconsistent > 0 OR nMinorityMeaning > 0
      OR nTrackingCrossPatient > 0 THEN 'High'
    WHEN nLateralityUncoded > 0 OR nMalformed > 0 OR nTrackingAmbiguous > 0
      OR nEntireFlavour > 0 THEN 'Medium'
    WHEN nCosmetic > 0 OR nSpellingVariant > 0 OR nTypeRepeatsCategory > 0
      OR anyNoSegmentsOverlap THEN 'Low'
    ELSE 'None'
    END AS worstSeverity,

  # description:
  # Number of segments in the series, Background excluded
  segmentCount,

  # description:
  # Segments whose code and meaning name different anatomy, so they need a
  # per-segment human decision. THIS IS THE WORK QUEUE.
  nAnatomyConflict + nLateralityInverted AS segmentsNeedingRecode,

  # description:
  # The offending code and meaning for each such segment
  offendingCodes,

  # description:
  # DICOM SeriesDescription of the segmentation series
  SeriesDescription,

  # description:
  # URL opening the segmentation series in the viewer
  viewer_url

FROM
  perSeries
ORDER BY
  CASE worstSeverity
    WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 WHEN 'Low' THEN 2 ELSE 3 END,
  segmentsNeedingRecode DESC,
  PatientID, StudyInstanceUID, SeriesInstanceUID
