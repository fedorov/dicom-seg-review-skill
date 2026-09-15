#standardSQL

# table-description:
# Issue 10 - severity Medium. Segments carrying codes under a
# CodingSchemeDesignator DICOM has retired: SRT, SNM3, SNM and 99SDM, all
# superseded by SCT.
#
# One row per affected segment. Fully computed - this will follow a new
# delivery.
#
# Why it is Medium and not cosmetic: every terminology check downstream is
# keyed on (CodingSchemeDesignator, CodeValue) and SCT is the only scheme they
# resolve. lookup_codes.py skips a non-SCT designator outright, and
# dcmterm.py counts it as a code DICOM's context groups do not carry. A batch
# coded entirely in SRT therefore reports near-zero terminology coverage and
# zero verified codes, which reads like a coverage gap and is not one. Say so
# in the report.
#
# The fix is NOT a rename. An SRT code and its SCT equivalent have different
# CodeValues - T-62000 "Liver" becomes 10200004 - so swapping the designator
# alone produces a code that does not exist. The mapping is a per-code lookup
# and belongs to the annotation producer.
#
# dciodvfy reports the same thing as a Warning ("CodingSchemeDesignator is
# deprecated"), per object. This query exists because it runs on a metadata
# table, where dciodvfy cannot: the validator needs the files.
#
# Background segments are excluded, as everywhere else.

WITH
  retired AS (
    SELECT * FROM UNNEST([
      STRUCT('SRT' AS scheme, 'SCT' AS replacement),
      STRUCT('SNM3', 'SCT'),
      STRUCT('SNM', 'SCT'),
      STRUCT('99SDM', 'SCT')
    ])
  ),

  perSegment AS (
    SELECT
      seg.*,
      ARRAY(
        SELECT affected FROM UNNEST([
          IF(seg.AnatomicRegionCodingSchemeDesignator IN (
               SELECT scheme FROM retired),
             CONCAT('AnatomicRegionSequence=',
                    seg.AnatomicRegionCodingSchemeDesignator, ':',
                    seg.AnatomicRegionCodeValue), NULL),
          IF(seg.AnatomicRegionModifierCodingSchemeDesignator IN (
               SELECT scheme FROM retired),
             CONCAT('AnatomicRegionModifierSequence=',
                    seg.AnatomicRegionModifierCodingSchemeDesignator, ':',
                    seg.AnatomicRegionModifierCodeValue), NULL),
          IF(seg.SegmentedPropertyCategoryCodingSchemeDesignator IN (
               SELECT scheme FROM retired),
             CONCAT('SegmentedPropertyCategoryCodeSequence=',
                    seg.SegmentedPropertyCategoryCodingSchemeDesignator, ':',
                    seg.SegmentedPropertyCategoryCodeValue), NULL),
          IF(seg.SegmentedPropertyTypeCodingSchemeDesignator IN (
               SELECT scheme FROM retired),
             CONCAT('SegmentedPropertyTypeCodeSequence=',
                    seg.SegmentedPropertyTypeCodingSchemeDesignator, ':',
                    seg.SegmentedPropertyTypeCodeValue), NULL),
          IF(seg.SegmentedPropertyTypeModifierCodingSchemeDesignator IN (
               SELECT scheme FROM retired),
             CONCAT('SegmentedPropertyTypeModifierCodeSequence=',
                    seg.SegmentedPropertyTypeModifierCodingSchemeDesignator, ':',
                    seg.SegmentedPropertyTypeModifierCodeValue), NULL)
        ]) AS affected
        WHERE affected IS NOT NULL) AS affectedSequences
    FROM
      `@@SEG_ATTRIBUTES@@` AS seg
    WHERE
      NOT seg.isBackgroundSegment
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
  # The retired designators this segment uses, comma separated
  ARRAY_TO_STRING(
    ARRAY(SELECT DISTINCT SPLIT(SPLIT(a, '=')[OFFSET(1)], ':')[OFFSET(0)]
          FROM UNNEST(affectedSequences) AS a
          ORDER BY 1), ', ') AS retiredSchemes,
  # description:
  # What replaces them. Always SCT for the SNOMED family - but the CodeValue
  # changes too, so this is a re-coding, not a rename.
  'SCT' AS replacementScheme,
  # description:
  # Which code sequences are affected, as sequence=scheme:codeValue
  ARRAY_TO_STRING(affectedSequences, '; ') AS affectedSequences,
  # description:
  # DICOM SeriesDescription of the segmentation series
  SeriesDescription,
  # description:
  # URL opening the segmentation series in the viewer
  viewer_url
FROM
  perSegment
WHERE
  ARRAY_LENGTH(affectedSequences) > 0
ORDER BY
  PatientID, SeriesInstanceUID, SegmentNumber
