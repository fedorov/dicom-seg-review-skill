#standardSQL

# table-description:
# Issue 13 - what colour each segment recommends, and where two structures in
# one object share one. One row per segment with a problem, plus a `colorIssue`
# saying which problem. Fully computed - this will follow a new delivery.
#
# Recommended Display CIELab Value (0062,000D) is Type 3, so its absence is
# conformant and is reported (ABSENT) rather than flagged. What is NOT
# conformant: PS3.3 C.8.20.2 says it "shall not be present if Segmentation Type
# (0062,0001) is LABELMAP and Photometric Interpretation (0028,0004) is PALETTE
# COLOR", because the palette already carries the colour.
#
# The one that a reader actually sees is DUPLICATE_IN_OBJECT: two segments of
# DIFFERENT structures, in ONE object, given the same colour. A viewer draws
# both overlays identically and no amount of correct coding tells them apart on
# screen. Two segments of the SAME structure sharing a colour is the point of a
# colour convention, not a defect, so the structure key below is what separates
# the finding from the norm.
#
# Everything here is decided in CIELab, never in RGB. The scaling is PS3.3
# C.10.7.1.1:
#
#   L*     0x0000 -> 0.0     0xFFFF -> 100.0
#   a*, b* 0x0000 -> -128.0  0x8080 -> 0.0    0xFFFF -> 127.0
#
# so dE*ab (CIE76) is a plain Euclidean distance once each component is scaled.
# Rendering a colour for a human needs a white point the Standard only implies
# and belongs to scripts/cielab.py; the swatches for the report come from
# scripts/seg_checks.py, which also writes the paste-ready palette table. This
# query gives the numbers, not the picture.
#
# @@DELTA_E@@ is the threshold below which two colours in one object count as
# confusable, in dE*ab. 10 is the default in seg_checks.py: 2.3 is the classic
# just-noticeable difference for two large flat patches side by side, and
# segment overlays are small, scattered and drawn at partial opacity over grey.
#
# Background segments are excluded, as everywhere else.

WITH
  parsed AS (
    SELECT
      seg.*,
      SAFE_CAST(SPLIT(seg.RecommendedDisplayCIELabValue, '/')[SAFE_OFFSET(0)]
                AS FLOAT64) / 65535.0 * 100.0 AS Lstar,
      SAFE_CAST(SPLIT(seg.RecommendedDisplayCIELabValue, '/')[SAFE_OFFSET(1)]
                AS FLOAT64) / 65535.0 * 255.0 - 128.0 AS astar,
      SAFE_CAST(SPLIT(seg.RecommendedDisplayCIELabValue, '/')[SAFE_OFFSET(2)]
                AS FLOAT64) / 65535.0 * 255.0 - 128.0 AS bstar,
      # What a viewer's user would call "a different thing on screen": the
      # coded identity first, because that is what the object asserts, and the
      # label only where there is no code at all.
      IF(seg.SegmentedPropertyTypeCodeValue IS NOT NULL
           OR seg.AnatomicRegionCodeValue IS NOT NULL,
         CONCAT(
           IFNULL(seg.SegmentedPropertyTypeCodingSchemeDesignator, ''), ':',
           IFNULL(seg.SegmentedPropertyTypeCodeValue, ''), '|',
           IFNULL(seg.AnatomicRegionCodingSchemeDesignator, ''), ':',
           IFNULL(seg.AnatomicRegionCodeValue, '')),
         CONCAT('label:', IFNULL(seg.SegmentLabel, ''))) AS structureKey,
      # VM 3, so anything else is malformed - including a fourth component,
      # which is why the length is checked and not just the casts.
      ARRAY_LENGTH(SPLIT(seg.RecommendedDisplayCIELabValue, '/')) != 3
        AS isWrongComponentCount,
      IFNULL(
        CONCAT(
          IFNULL(seg.SegmentedPropertyTypeCodeMeaning, ''),
          IF(seg.SegmentedPropertyTypeCodeMeaning IS NOT NULL
               AND seg.AnatomicRegionCodeMeaning IS NOT NULL, ' / ', ''),
          IFNULL(seg.AnatomicRegionCodeMeaning, '')),
        IFNULL(seg.SegmentLabel, '(uncoded)')) AS structure
    FROM
      `@@SEG_ATTRIBUTES@@` AS seg
    WHERE
      NOT seg.isBackgroundSegment
  ),

  # Two segments of different structures in one object, close enough in CIELab
  # that a reader cannot separate them. Self-join on the object, ordered pair
  # de-duplicated by SegmentNumber so each collision is seen once per segment.
  collisions AS (
    SELECT
      a.SOPInstanceUID,
      a.SegmentNumber,
      # One row per SEGMENT, not per pair: the segment is what gets recoloured,
      # and a three-way collision is one job, not three. An exact duplicate
      # anywhere in the group outranks a merely confusable neighbour, which is
      # the same rule scripts/seg_checks.py applies.
      IF(LOGICAL_OR(a.RecommendedDisplayCIELabValue
                    = b.RecommendedDisplayCIELabValue),
         'DUPLICATE_IN_OBJECT', 'CONFUSABLE_IN_OBJECT') AS colorIssue,
      STRING_AGG(CAST(b.SegmentNumber AS STRING), ';'
                 ORDER BY b.SegmentNumber) AS collidesWithSegments,
      STRING_AGG(DISTINCT b.structure, '; ') AS collidesWithStructures,
      MIN(SQRT(POW(a.Lstar - b.Lstar, 2)
               + POW(a.astar - b.astar, 2)
               + POW(a.bstar - b.bstar, 2))) AS deltaE
    FROM parsed AS a
    JOIN parsed AS b
      ON a.SOPInstanceUID = b.SOPInstanceUID
      AND a.SegmentNumber != b.SegmentNumber
      AND a.structureKey != b.structureKey
    WHERE
      a.Lstar IS NOT NULL AND b.Lstar IS NOT NULL
      AND SQRT(POW(a.Lstar - b.Lstar, 2)
               + POW(a.astar - b.astar, 2)
               + POW(a.bstar - b.bstar, 2)) < @@DELTA_E@@
    GROUP BY
      a.SOPInstanceUID, a.SegmentNumber
  ),

  # One structure drawn in several colours across the batch. Not wrong
  # anywhere in particular; it makes two series unreadable side by side.
  inconsistent AS (
    SELECT structureKey
    FROM parsed
    WHERE Lstar IS NOT NULL
    GROUP BY structureKey
    HAVING COUNT(DISTINCT RecommendedDisplayCIELabValue) > 1
  ),

  flagged AS (
    SELECT
      parsed.*,
      collisions.colorIssue AS collisionIssue,
      collisions.collidesWithSegments,
      collisions.collidesWithStructures,
      collisions.deltaE,
      parsed.structureKey IN (SELECT structureKey FROM inconsistent)
        AS isInconsistent
    FROM parsed
    LEFT JOIN collisions
      ON collisions.SOPInstanceUID = parsed.SOPInstanceUID
      AND collisions.SegmentNumber = parsed.SegmentNumber
  )

SELECT
  # description:
  # DICOM PatientID
  PatientID,
  # description:
  # DICOM StudyInstanceUID of the study containing the segmentation
  StudyInstanceUID,
  # description:
  # DICOM SeriesInstanceUID of the segmentation series
  SeriesInstanceUID,
  # description:
  # DICOM SOPInstanceUID of the segmentation object
  SOPInstanceUID,
  # description:
  # DICOM SegmentNumber within that object
  SegmentNumber,
  # description:
  # DICOM SegmentLabel
  SegmentLabel,
  # description:
  # Which problem this row reports, worst first where a segment has several:
  #   NOT_PERMITTED              LABELMAP + PALETTE COLOR must not carry it
  #   MALFORMED                  present, but not three unsigned shorts
  #   DUPLICATE_IN_OBJECT        another structure in this object, same colour
  #   CONFUSABLE_IN_OBJECT       ... within @@DELTA_E@@ dE*ab of this one
  #   INCONSISTENT_ACROSS_BATCH  this structure is drawn in several colours
  #   ABSENT                     no colour at all. Type 3, so conformant.
  CASE
    WHEN RecommendedDisplayCIELabValue IS NOT NULL
      AND SegmentationType = 'LABELMAP'
      AND PhotometricInterpretation = 'PALETTE COLOR' THEN 'NOT_PERMITTED'
    WHEN RecommendedDisplayCIELabValue IS NOT NULL
      AND (isWrongComponentCount
           OR Lstar IS NULL OR astar IS NULL OR bstar IS NULL)
      THEN 'MALFORMED'
    WHEN collisionIssue IS NOT NULL THEN collisionIssue
    WHEN isInconsistent THEN 'INCONSISTENT_ACROSS_BATCH'
    WHEN RecommendedDisplayCIELabValue IS NULL THEN 'ABSENT'
    END AS colorIssue,
  # description:
  # The three unsigned shorts exactly as stored, "L/a/b"
  RecommendedDisplayCIELabValue,
  # description:
  # L* on its real 0..100 scale, per PS3.3 C.10.7.1.1
  ROUND(Lstar, 1) AS Lstar,
  # description:
  # a* on its real -128..127 scale
  ROUND(astar, 1) AS astar,
  # description:
  # b* on its real -128..127 scale
  ROUND(bstar, 1) AS bstar,
  # description:
  # What this segment depicts, as the codes name it
  structure,
  # description:
  # SegmentNumbers in the same object this segment cannot be told apart from
  collidesWithSegments,
  # description:
  # What those segments depict. If this reads like a different organ, the
  # colours are the finding.
  collidesWithStructures,
  # description:
  # dE*ab (CIE76) to the nearest of them. 0 means identical.
  ROUND(deltaE, 2) AS deltaE,
  # description:
  # DICOM SegmentationType, for the NOT_PERMITTED rule
  SegmentationType,
  # description:
  # DICOM PhotometricInterpretation, for the NOT_PERMITTED rule
  PhotometricInterpretation,
  # description:
  # DICOM SeriesDescription of the segmentation series
  SeriesDescription,
  # description:
  # URL opening the segmentation series in the viewer
  viewer_url
FROM
  flagged
WHERE
  RecommendedDisplayCIELabValue IS NULL
  OR isWrongComponentCount
  OR Lstar IS NULL OR astar IS NULL OR bstar IS NULL
  OR collisionIssue IS NOT NULL
  OR isInconsistent
  OR (SegmentationType = 'LABELMAP' AND PhotometricInterpretation = 'PALETTE COLOR')
ORDER BY
  CASE colorIssue
    WHEN 'NOT_PERMITTED' THEN 0 WHEN 'MALFORMED' THEN 1
    WHEN 'DUPLICATE_IN_OBJECT' THEN 2 WHEN 'CONFUSABLE_IN_OBJECT' THEN 3
    WHEN 'INCONSISTENT_ACROSS_BATCH' THEN 4 ELSE 5 END,
  PatientID, SeriesInstanceUID, SegmentNumber
