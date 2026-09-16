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
#
# The code-level issues (1, 2, 3, 8) are judged over the anatomic region, the
# segmented property type AND the segmented property category - see the
# `codes` CTE, identical to the one in 02 - and counted once per SEGMENT
# however many of its sequences offend: the segment is the unit of work.
#
# @@DELTA_E@@ is issue 13's confusability threshold in dE*ab; use the same value
# here as in 14_recommended_color.sql, or the two will disagree.

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
  ),

  # Issue 2. Ambiguity is a property of a CODE IN ONE ROLE across the whole
  # population, so the tags below separate what is evidence about a series
  # from what is not.
  ambiguousCodes AS (
    SELECT codeSequence, CodeValue
    FROM codes
    GROUP BY codeSequence, CodeValue
    HAVING COUNT(DISTINCT CodeMeaning) > 1
  ),

  dominantMeaning AS (
    SELECT codeSequence, CodeValue, CodeMeaning AS dominant
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
  ),

  # Issues 1, 2, 3 and 8 per (segment, sequence).
  codeRows AS (
    SELECT
      codes.SOPInstanceUID,
      codes.SegmentNumber,
      codes.CodeValue,
      codes.CodeMeaning,
      EXISTS (SELECT 1 FROM review r
              WHERE r.CodeValue = codes.CodeValue
                AND r.CodingSchemeDesignator = codes.CodingSchemeDesignator
                AND r.meaningRecorded = codes.CodeMeaning
                AND (r.codeSequence IS NULL OR r.codeSequence = codes.codeSequence)
                AND r.verdict = 'INVERTED') AS isLateralityInverted,
      EXISTS (SELECT 1 FROM review r
              WHERE r.CodeValue = codes.CodeValue
                AND r.CodingSchemeDesignator = codes.CodingSchemeDesignator
                AND r.meaningRecorded = codes.CodeMeaning
                AND (r.codeSequence IS NULL OR r.codeSequence = codes.codeSequence)
                AND r.issue = 1
                AND r.verdict IN ('WRONG_ANATOMY', 'NARROWER_OR_BROADER'))
        AS isAnatomyConflict,
      EXISTS (SELECT 1 FROM review r
              WHERE r.CodeValue = codes.CodeValue
                AND r.CodingSchemeDesignator = codes.CodingSchemeDesignator
                AND r.meaningRecorded = codes.CodeMeaning
                AND (r.codeSequence IS NULL OR r.codeSequence = codes.codeSequence)
                AND r.verdict = 'UNCODED') AS isLateralityUncoded,
      EXISTS (SELECT 1 FROM review r
              WHERE r.CodeValue = codes.CodeValue
                AND r.CodingSchemeDesignator = codes.CodingSchemeDesignator
                AND r.meaningRecorded = codes.CodeMeaning
                AND (r.codeSequence IS NULL OR r.codeSequence = codes.codeSequence)
                AND r.verdict = 'SPELLING') AS isSpellingVariant,
      codes.SeriesInstanceUID IN (SELECT SeriesInstanceUID FROM selfInconsistentSeries)
        AS isSelfInconsistent,
      ambiguousCodes.CodeValue IS NOT NULL AS isCodeAmbiguousSomewhere,
      ambiguousCodes.CodeValue IS NOT NULL
        AND codes.CodeMeaning != dominantMeaning.dominant AS isMinorityMeaning,
      codes.CodeValue IN (SELECT code FROM entireFlavourCodes) AS isEntireFlavour,
      codes.CodeValue IN (SELECT code FROM cosmeticCodes) AS isCosmetic
    FROM codes
    LEFT JOIN ambiguousCodes
      ON ambiguousCodes.codeSequence = codes.codeSequence
      AND ambiguousCodes.CodeValue = codes.CodeValue
    LEFT JOIN dominantMeaning
      ON dominantMeaning.codeSequence = codes.codeSequence
      AND dominantMeaning.CodeValue = codes.CodeValue
  ),

  # ...folded back to one row per SEGMENT: a liver coded as bowel in both the
  # region and the type is one segment to recode, not two.
  codeFlags AS (
    SELECT
      SOPInstanceUID,
      SegmentNumber,
      LOGICAL_OR(isLateralityInverted) AS isLateralityInverted,
      LOGICAL_OR(isAnatomyConflict) AS isAnatomyConflict,
      LOGICAL_OR(isLateralityUncoded) AS isLateralityUncoded,
      LOGICAL_OR(isSpellingVariant) AS isSpellingVariant,
      LOGICAL_OR(isSelfInconsistent) AS isSelfInconsistent,
      LOGICAL_OR(isCodeAmbiguousSomewhere) AS isCodeAmbiguousSomewhere,
      LOGICAL_OR(isMinorityMeaning) AS isMinorityMeaning,
      LOGICAL_OR(isEntireFlavour) AS isEntireFlavour,
      LOGICAL_OR(isCosmetic) AS isCosmetic,
      STRING_AGG(
        DISTINCT IF(isAnatomyConflict OR isLateralityInverted,
                    CONCAT(CodeValue, ' "', CodeMeaning, '"'), NULL),
        '; ') AS offendingCodes
    FROM codeRows
    GROUP BY SOPInstanceUID, SegmentNumber
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

  # Issue 7, the serious case: one identifier spanning two patients. Normally
  # absent, and a real problem if it appears.
  crossPatientTrackingUIDs AS (
    SELECT TrackingUID
    FROM `@@SEG_ATTRIBUTES@@`
    WHERE TrackingUID IS NOT NULL AND NOT isBackgroundSegment
    GROUP BY TrackingUID
    HAVING COUNT(DISTINCT PatientID) > 1
  ),

  # Issue 13. Two segments of DIFFERENT structures in ONE object, close enough
  # in CIELab that a viewer draws them alike. Definitions and the dE*ab scaling
  # are in 14_recommended_color.sql; @@DELTA_E@@ is the same threshold.
  colouredSegments AS (
    SELECT
      SOPInstanceUID,
      SegmentNumber,
      RecommendedDisplayCIELabValue,
      SAFE_CAST(SPLIT(RecommendedDisplayCIELabValue, '/')[SAFE_OFFSET(0)]
                AS FLOAT64) / 65535.0 * 100.0 AS Lstar,
      SAFE_CAST(SPLIT(RecommendedDisplayCIELabValue, '/')[SAFE_OFFSET(1)]
                AS FLOAT64) / 65535.0 * 255.0 - 128.0 AS astar,
      SAFE_CAST(SPLIT(RecommendedDisplayCIELabValue, '/')[SAFE_OFFSET(2)]
                AS FLOAT64) / 65535.0 * 255.0 - 128.0 AS bstar,
      # Must match 14_recommended_color.sql exactly, designators included, or
      # the two disagree about what counts as "a different structure".
      IF(SegmentedPropertyTypeCodeValue IS NOT NULL
           OR AnatomicRegionCodeValue IS NOT NULL,
         CONCAT(IFNULL(SegmentedPropertyTypeCodingSchemeDesignator, ''), ':',
                IFNULL(SegmentedPropertyTypeCodeValue, ''), '|',
                IFNULL(AnatomicRegionCodingSchemeDesignator, ''), ':',
                IFNULL(AnatomicRegionCodeValue, '')),
         CONCAT('label:', IFNULL(SegmentLabel, ''))) AS structureKey
    FROM `@@SEG_ATTRIBUTES@@`
    WHERE RecommendedDisplayCIELabValue IS NOT NULL AND NOT isBackgroundSegment
  ),

  colourCollisions AS (
    SELECT
      a.SOPInstanceUID,
      a.SegmentNumber,
      LOGICAL_OR(a.RecommendedDisplayCIELabValue
                 = b.RecommendedDisplayCIELabValue) AS isDuplicate
    FROM colouredSegments AS a
    JOIN colouredSegments AS b
      ON a.SOPInstanceUID = b.SOPInstanceUID
      AND a.SegmentNumber != b.SegmentNumber
      AND a.structureKey != b.structureKey
    WHERE
      a.Lstar IS NOT NULL AND b.Lstar IS NOT NULL
      AND SQRT(POW(a.Lstar - b.Lstar, 2)
               + POW(a.astar - b.astar, 2)
               + POW(a.bstar - b.bstar, 2)) < @@DELTA_E@@
    GROUP BY a.SOPInstanceUID, a.SegmentNumber
  ),

  # Issue 13. One structure drawn in several colours across the batch.
  inconsistentStructures AS (
    SELECT structureKey
    FROM colouredSegments
    GROUP BY structureKey
    HAVING COUNT(DISTINCT RecommendedDisplayCIELabValue) > 1
  ),

  # Issue 16, per object. Background rows are deliberately INCLUDED: a
  # LABELMAP's 0 is legitimate and a BINARY object's 0 is the defect. See
  # 16_segment_numbers.sql.
  segmentNumbering AS (
    SELECT
      SOPInstanceUID,
      COUNTIF(SegmentNumber IS NOT NULL) > COUNT(DISTINCT SegmentNumber)
        AS hasDuplicateNumber,
      ANY_VALUE(SegmentationType) IN ('BINARY', 'FRACTIONAL')
        AND COUNT(DISTINCT SegmentNumber) > 0
        AND (MIN(SegmentNumber) != 1
             OR MAX(SegmentNumber) != COUNT(DISTINCT SegmentNumber))
        AS isNotSequential
    FROM `@@SEG_ATTRIBUTES@@`
    GROUP BY SOPInstanceUID
  ),

  flagged AS (
    SELECT
      seg.*,
      IFNULL(codeFlags.isLateralityInverted, FALSE) AS isLateralityInverted,
      IFNULL(codeFlags.isAnatomyConflict, FALSE) AS isAnatomyConflict,
      IFNULL(codeFlags.isLateralityUncoded, FALSE) AS isLateralityUncoded,
      IFNULL(codeFlags.isSelfInconsistent, FALSE) AS isSelfInconsistent,
      IFNULL(codeFlags.isMinorityMeaning, FALSE) AS isMinorityMeaning,
      IFNULL(codeFlags.isCodeAmbiguousSomewhere, FALSE) AS isCodeAmbiguousSomewhere,
      IFNULL(codeFlags.isEntireFlavour, FALSE) AS isEntireFlavour,
      IFNULL(codeFlags.isSpellingVariant, FALSE) AS isSpellingVariant,
      IFNULL(codeFlags.isCosmetic, FALSE) AS isCosmetic,
      codeFlags.offendingCodes,
      seg.SegmentNumber IS NULL
        OR seg.SegmentLabel IS NULL
        OR seg.SegmentedPropertyCategoryCodeValue IS NULL
        OR seg.SegmentedPropertyTypeCodeValue IS NULL
        OR seg.SegmentAlgorithmType IS NULL AS isMalformed,
      # Issue 10. SRT/SNM3/SNM/99SDM are retired in favour of SCT - and the
      # CodeValue changes with the designator, so this is a re-coding job.
      seg.AnatomicRegionCodingSchemeDesignator IN ('SRT', 'SNM3', 'SNM', '99SDM')
        OR seg.AnatomicRegionModifierCodingSchemeDesignator
             IN ('SRT', 'SNM3', 'SNM', '99SDM')
        OR seg.SegmentedPropertyCategoryCodingSchemeDesignator
             IN ('SRT', 'SNM3', 'SNM', '99SDM')
        OR seg.SegmentedPropertyTypeCodingSchemeDesignator
             IN ('SRT', 'SNM3', 'SNM', '99SDM')
        OR seg.SegmentedPropertyTypeModifierCodingSchemeDesignator
             IN ('SRT', 'SNM3', 'SNM', '99SDM') AS isRetiredScheme,
      seg.TrackingUID IN (SELECT TrackingUID FROM ambiguousTrackingUIDs)
        AS isTrackingAmbiguous,
      seg.TrackingUID IN (SELECT TrackingUID FROM crossPatientTrackingUIDs)
        AS isTrackingCrossPatient,
      seg.SegmentedPropertyTypeCodeValue = seg.SegmentedPropertyCategoryCodeValue
        AS isTypeRepeatsCategory,
      seg.SegmentsOverlap IS NULL AS isNoSegmentsOverlap,
      # Issue 13. See 14_recommended_color.sql for every definition here.
      IFNULL(colourCollisions.isDuplicate, FALSE) AS isColorDuplicate,
      # A collision that is not an exact duplicate. NULL means no collision.
      IFNULL(NOT colourCollisions.isDuplicate, FALSE) AS isColorConfusable,
      IF(seg.SegmentedPropertyTypeCodeValue IS NOT NULL
           OR seg.AnatomicRegionCodeValue IS NOT NULL,
         CONCAT(IFNULL(seg.SegmentedPropertyTypeCodingSchemeDesignator, ''), ':',
                IFNULL(seg.SegmentedPropertyTypeCodeValue, ''), '|',
                IFNULL(seg.AnatomicRegionCodingSchemeDesignator, ''), ':',
                IFNULL(seg.AnatomicRegionCodeValue, '')),
         CONCAT('label:', IFNULL(seg.SegmentLabel, '')))
        IN (SELECT structureKey FROM inconsistentStructures)
        AS isColorInconsistent,
      # PS3.3 C.8.20.2: shall not be present on a PALETTE COLOR LABELMAP.
      seg.RecommendedDisplayCIELabValue IS NOT NULL
        AND seg.SegmentationType = 'LABELMAP'
        AND seg.PhotometricInterpretation = 'PALETTE COLOR'
        AS isColorNotPermitted,
      # VM 3, so anything else is malformed - including a fourth component,
      # which is why the length is checked and not just the casts.
      seg.RecommendedDisplayCIELabValue IS NOT NULL
        AND (ARRAY_LENGTH(SPLIT(seg.RecommendedDisplayCIELabValue, '/')) != 3
             OR SAFE_CAST(SPLIT(seg.RecommendedDisplayCIELabValue, '/')[SAFE_OFFSET(0)]
                          AS FLOAT64) IS NULL
             OR SAFE_CAST(SPLIT(seg.RecommendedDisplayCIELabValue, '/')[SAFE_OFFSET(1)]
                          AS FLOAT64) IS NULL
             OR SAFE_CAST(SPLIT(seg.RecommendedDisplayCIELabValue, '/')[SAFE_OFFSET(2)]
                          AS FLOAT64) IS NULL) AS isColorMalformed,
      seg.RecommendedDisplayCIELabValue IS NULL AS isColorAbsent,
      # Issue 14. Type 1C, so this one is a conformance violation; the rest of
      # 15_algorithm_identification.sql is about what the name is worth.
      UPPER(IFNULL(seg.SegmentAlgorithmType, '')) IN ('AUTOMATIC', 'SEMIAUTOMATIC')
        AND (seg.SegmentAlgorithmName IS NULL
             OR TRIM(seg.SegmentAlgorithmName) = '') AS isAlgorithmNameMissing,
      UPPER(IFNULL(seg.SegmentAlgorithmType, '')) IN ('AUTOMATIC', 'SEMIAUTOMATIC')
        AND (NOT IFNULL(seg.hasAlgorithmIdentification, FALSE)
             OR seg.AlgorithmVersion IS NULL
             OR TRIM(seg.AlgorithmVersion) = '') AS isAlgorithmUnidentified,
      # Issue 16. See 16_segment_numbers.sql.
      IFNULL(segmentNumbering.hasDuplicateNumber, FALSE) AS isSegmentNumberDuplicate,
      IFNULL(segmentNumbering.isNotSequential, FALSE) AS isSegmentNumberNotSequential,
      # Issue 17. See 17_frame_of_reference.sql. Both are FALSE, not NULL,
      # where the referenced series was never resolved - silence, not a pass.
      seg.referencedSeriesInstanceUID IS NOT NULL
        AND IFNULL(seg.referencedSeriesFound, TRUE) = FALSE
        AS isReferencedSeriesMissing,
      seg.referencedFrameOfReferenceUID IS NOT NULL
        AND seg.FrameOfReferenceUID != seg.referencedFrameOfReferenceUID
        AS isFrameOfReferenceMismatch
    FROM
      `@@SEG_ATTRIBUTES@@` AS seg
    LEFT JOIN
      codeFlags
      ON codeFlags.SOPInstanceUID = seg.SOPInstanceUID
      AND codeFlags.SegmentNumber = seg.SegmentNumber
    LEFT JOIN
      colourCollisions
      ON colourCollisions.SOPInstanceUID = seg.SOPInstanceUID
      AND colourCollisions.SegmentNumber = seg.SegmentNumber
    LEFT JOIN
      segmentNumbering
      ON segmentNumbering.SOPInstanceUID = seg.SOPInstanceUID
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
      COUNTIF(isRetiredScheme) AS nRetiredScheme,
      COUNTIF(isTrackingAmbiguous) AS nTrackingAmbiguous,
      COUNTIF(isTrackingCrossPatient) AS nTrackingCrossPatient,
      COUNTIF(isSpellingVariant) AS nSpellingVariant,
      COUNTIF(isCosmetic) AS nCosmetic,
      COUNTIF(isTypeRepeatsCategory) AS nTypeRepeatsCategory,
      LOGICAL_OR(isNoSegmentsOverlap) AS anyNoSegmentsOverlap,
      COUNTIF(isColorDuplicate) AS nColorDuplicate,
      COUNTIF(isColorConfusable) AS nColorConfusable,
      COUNTIF(isColorInconsistent) AS nColorInconsistent,
      COUNTIF(isColorNotPermitted) AS nColorNotPermitted,
      COUNTIF(isColorMalformed) AS nColorMalformed,
      COUNTIF(isColorAbsent) AS nColorAbsent,
      COUNTIF(isAlgorithmNameMissing) AS nAlgorithmNameMissing,
      COUNTIF(isAlgorithmUnidentified) AS nAlgorithmUnidentified,
      LOGICAL_OR(isSegmentNumberDuplicate) AS anySegmentNumberDuplicate,
      LOGICAL_OR(isSegmentNumberNotSequential) AS anySegmentNumberNotSequential,
      LOGICAL_OR(isReferencedSeriesMissing) AS anyReferencedSeriesMissing,
      LOGICAL_OR(isFrameOfReferenceMismatch) AS anyFrameOfReferenceMismatch,
      STRING_AGG(DISTINCT offendingCodes, '; ' ORDER BY offendingCodes)
        AS offendingCodes
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
  #   RETIRED_CODING_SCHEME    (Medium) 13_retired_coding_scheme.sql
  #   TRACKINGUID_AMBIGUOUS    (Medium) 11_tracking_uid.sql
  #   COLOR_DUPLICATE          (Medium) 14_recommended_color.sql
  #   COLOR_NOT_PERMITTED      (Medium) 14_recommended_color.sql
  #   COLOR_MALFORMED          (Medium) 14_recommended_color.sql
  #   ALGORITHM_NAME_MISSING   (Medium) 15_algorithm_identification.sql
  #   SEGMENT_NUMBER_DUPLICATE (Medium) 16_segment_numbers.sql
  #   SEGMENT_NUMBER_NOT_SEQUENTIAL (Medium) 16_segment_numbers.sql
  #   FRAME_OF_REFERENCE_MISMATCH (Medium) 17_frame_of_reference.sql
  #   REFERENCED_SERIES_MISSING (Medium) 17_frame_of_reference.sql
  #   COSMETIC_VARIANT         (Low)    02_ambiguous_code.sql
  #   CODE_MEANING_SPELLING    (Low)    05_anatomy_conflict.sql
  #   TYPE_REPEATS_CATEGORY    (Low)    09_type_repeats_category.sql
  #   NO_SEGMENTS_OVERLAP      (Low)    10_segments_overlap_absent.sql
  #   COLOR_CONFUSABLE         (Low)    14_recommended_color.sql
  #   COLOR_INCONSISTENT       (Low)    14_recommended_color.sql
  #   COLOR_ABSENT             (Low)    14_recommended_color.sql
  #   ALGORITHM_UNIDENTIFIED   (Low)    15_algorithm_identification.sql
  # Tags deliberately ABSENT here, because the checks that would produce them
  # cannot run on a metadata table alone:
  #   IOD_ERROR / IOD_WARNING            issue 9, from dciodvfy
  #   EMPTY_SEGMENT / EMPTY_FRAMES_RETAINED   issue 11, from the pixel data
  #   UNCOMPRESSED / LOSSY_COMPRESSED    issue 12, from the file meta group
  #   TYPE_OUTSIDE_CATEGORY / TYPE_NOT_IN_CID / CATEGORY_NOT_IN_CID
  #                                      issue 15, from dcmterms' context-group
  #                                      tables - export the per-segment view
  #                                      and run scripts/dcmterm.py property
  # The first three need the objects; scripts/seg_checks.py adds them on the
  # local-files path via --iod and --encoding, and issue 15 on every path via
  # --property. A triage list built from this query alone is SILENT about all
  # of them; say so rather than letting the silence read as a pass.
  #
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
        IF(nRetiredScheme > 0, 'RETIRED_CODING_SCHEME', NULL),
        IF(nTrackingAmbiguous > 0, 'TRACKINGUID_AMBIGUOUS', NULL),
        IF(nColorDuplicate > 0, 'COLOR_DUPLICATE', NULL),
        IF(nColorNotPermitted > 0, 'COLOR_NOT_PERMITTED', NULL),
        IF(nColorMalformed > 0, 'COLOR_MALFORMED', NULL),
        IF(nAlgorithmNameMissing > 0, 'ALGORITHM_NAME_MISSING', NULL),
        IF(anySegmentNumberDuplicate, 'SEGMENT_NUMBER_DUPLICATE', NULL),
        IF(anySegmentNumberNotSequential, 'SEGMENT_NUMBER_NOT_SEQUENTIAL', NULL),
        IF(anyFrameOfReferenceMismatch, 'FRAME_OF_REFERENCE_MISMATCH', NULL),
        IF(anyReferencedSeriesMissing, 'REFERENCED_SERIES_MISSING', NULL),
        IF(nCosmetic > 0, 'COSMETIC_VARIANT', NULL),
        IF(nSpellingVariant > 0, 'CODE_MEANING_SPELLING', NULL),
        IF(nTypeRepeatsCategory > 0, 'TYPE_REPEATS_CATEGORY', NULL),
        IF(anyNoSegmentsOverlap, 'NO_SEGMENTS_OVERLAP', NULL),
        IF(nColorConfusable > 0, 'COLOR_CONFUSABLE', NULL),
        IF(nColorInconsistent > 0, 'COLOR_INCONSISTENT', NULL),
        IF(nColorAbsent > 0, 'COLOR_ABSENT', NULL),
        IF(nAlgorithmUnidentified > 0, 'ALGORITHM_UNIDENTIFIED', NULL),
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
      OR nEntireFlavour > 0 OR nColorDuplicate > 0 OR nColorNotPermitted > 0
      OR nColorMalformed > 0 OR nAlgorithmNameMissing > 0
      OR anySegmentNumberDuplicate OR anySegmentNumberNotSequential
      OR anyFrameOfReferenceMismatch OR anyReferencedSeriesMissing THEN 'Medium'
    WHEN nCosmetic > 0 OR nSpellingVariant > 0 OR nTypeRepeatsCategory > 0
      OR anyNoSegmentsOverlap OR nColorConfusable > 0 OR nColorInconsistent > 0
      OR nColorAbsent > 0 OR nAlgorithmUnidentified > 0 THEN 'Low'
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
