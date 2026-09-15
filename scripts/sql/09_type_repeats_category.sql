#standardSQL

# table-description:
# Issue 5 - severity Low. Segments where SegmentedPropertyTypeCodeSequence
# (0062,000F) repeats SegmentedPropertyCategoryCodeSequence (0062,0003), so the
# type adds no information beyond the category.
#
# One row per affected segment. Fully computed. Conformant, just uninformative.
#
# Report the correctly coded majority alongside these, or the finding reads as
# bigger than it is:
#   SELECT SegmentedPropertyTypeCodeValue, ANY_VALUE(SegmentedPropertyTypeCodeMeaning),
#          COUNT(*)
#   FROM `@@SEG_ATTRIBUTES@@` WHERE NOT isBackgroundSegment
#   GROUP BY 1 ORDER BY 3 DESC

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
  # DICOM SegmentNumber within its segmentation object
  SegmentNumber,
  # description:
  # DICOM SegmentLabel of the affected segment
  SegmentLabel,
  # description:
  # The code used for both category and type
  SegmentedPropertyCategoryCodeValue AS repeatedCodeValue,
  # description:
  # Its CodeMeaning
  SegmentedPropertyCategoryCodeMeaning AS repeatedCodeMeaning,
  # description:
  # Anatomic region CodeMeaning, the only remaining hint at what was segmented
  AnatomicRegionCodeMeaning,
  # description:
  # URL opening the segmentation series in the viewer
  viewer_url
FROM
  `@@SEG_ATTRIBUTES@@`
WHERE
  NOT isBackgroundSegment
  AND SegmentedPropertyTypeCodeValue = SegmentedPropertyCategoryCodeValue
  AND SegmentedPropertyTypeCodingSchemeDesignator
    = SegmentedPropertyCategoryCodingSchemeDesignator
ORDER BY
  PatientID, SeriesInstanceUID, SegmentNumber
