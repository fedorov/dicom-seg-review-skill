#standardSQL

# table-description:
# Issue 4 - severity Medium. Segments missing an attribute the Segment
# Description Macro (DICOM PS3.3 Table C.8.20-4) makes Type 1, or carrying a
# code sequence without its CodingSchemeDesignator.
#
# One row per affected segment. Fully computed - this will follow a new delivery.
#
# Type 1 in that macro: SegmentNumber (0062,0004), SegmentLabel (0062,0005),
# SegmentedPropertyCategoryCodeSequence (0062,0003),
# SegmentedPropertyTypeCodeSequence (0062,000F) and SegmentAlgorithmType
# (0062,0008).
#
# AnatomicRegionSequence and RecommendedDisplayCIELabValue are OPTIONAL, so
# their absence is conformant and is deliberately not reported here.
# TrackingID / TrackingUID are Type 1C.
#
# A code value without its CodingSchemeDesignator does not identify a concept,
# so it is reported even though the sequence itself is present.
#
# Background segments are excluded: a labelmap's SegmentNumber 0 segment is an
# artefact of the encoding and would otherwise fire on every object.
#
# A NULL SegmentNumber also breaks the (SOPInstanceUID, SegmentNumber) key, so
# qualify any claim about that key by whatever this returns.

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
  # DICOM SOPInstanceUID. Given because a NULL SegmentNumber means the segment
  # cannot be addressed by number.
  SOPInstanceUID,
  # description:
  # DICOM SegmentNumber, NULL when the Type 1 attribute is absent
  SegmentNumber,
  # description:
  # Comma-separated list of the Type 1 attributes this segment omits
  ARRAY_TO_STRING(
    ARRAY(
      SELECT missing FROM UNNEST([
        IF(SegmentNumber IS NULL, 'SegmentNumber', NULL),
        IF(SegmentLabel IS NULL, 'SegmentLabel', NULL),
        IF(SegmentedPropertyCategoryCodeValue IS NULL,
           'SegmentedPropertyCategoryCodeSequence', NULL),
        IF(SegmentedPropertyTypeCodeValue IS NULL,
           'SegmentedPropertyTypeCodeSequence', NULL),
        IF(SegmentAlgorithmType IS NULL, 'SegmentAlgorithmType', NULL),
        IF(SegmentedPropertyTypeCodeValue IS NOT NULL
             AND SegmentedPropertyTypeCodingSchemeDesignator IS NULL,
           'SegmentedPropertyTypeCodeSequence.CodingSchemeDesignator', NULL),
        IF(SegmentedPropertyCategoryCodeValue IS NOT NULL
             AND SegmentedPropertyCategoryCodingSchemeDesignator IS NULL,
           'SegmentedPropertyCategoryCodeSequence.CodingSchemeDesignator', NULL),
        IF(AnatomicRegionCodeValue IS NOT NULL
             AND AnatomicRegionCodingSchemeDesignator IS NULL,
           'AnatomicRegionSequence.CodingSchemeDesignator', NULL)
      ]) AS missing
      WHERE missing IS NOT NULL),
    ', ') AS missingType1Attributes,
  # description:
  # DICOM SeriesDescription of the segmentation series
  SeriesDescription,
  # description:
  # URL opening the segmentation series in the viewer
  viewer_url
FROM
  `@@SEG_ATTRIBUTES@@`
WHERE
  NOT isBackgroundSegment
  AND (SegmentNumber IS NULL
    OR SegmentLabel IS NULL
    OR SegmentedPropertyCategoryCodeValue IS NULL
    OR SegmentedPropertyTypeCodeValue IS NULL
    OR SegmentAlgorithmType IS NULL
    OR (SegmentedPropertyTypeCodeValue IS NOT NULL
        AND SegmentedPropertyTypeCodingSchemeDesignator IS NULL)
    OR (SegmentedPropertyCategoryCodeValue IS NOT NULL
        AND SegmentedPropertyCategoryCodingSchemeDesignator IS NULL)
    OR (AnatomicRegionCodeValue IS NOT NULL
        AND AnatomicRegionCodingSchemeDesignator IS NULL))
ORDER BY
  PatientID, SeriesInstanceUID, SegmentNumber
