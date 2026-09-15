#standardSQL

# table-description:
# Issue 6 - severity Low, and arguably not a defect: SegmentsOverlap
# (0062,0013) is Type 3 in the Segmentation Image Module (DICOM PS3.3
# C.8.20.2), so omitting it is fully conformant.
#
# One row per segmentation OBJECT that omits it - the attribute is object-level,
# not per-segment. Fully computed.
#
# Reported so that its absence is not later mistaken for a defect, and so a
# consumer needing to know whether segments share voxels can see which objects
# it must compute that from instead.
#
# MULTI-SEGMENT OBJECTS ARE THE ONLY ONES THAT LOSE ANYTHING, so segmentsInObject
# is given and the ordering puts them first. Quote both numbers in the report.

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
  # DICOM SOPInstanceUID of the object omitting SegmentsOverlap
  SOPInstanceUID,
  # description:
  # Number of segments in the object, Background excluded. Only above 1 does the
  # missing attribute withhold anything.
  COUNT(*) AS segmentsInObject,
  # description:
  # DICOM SeriesDescription of the segmentation series
  ANY_VALUE(SeriesDescription) AS SeriesDescription,
  # description:
  # URL opening the segmentation series in the viewer
  ANY_VALUE(viewer_url) AS viewer_url
FROM
  `@@SEG_ATTRIBUTES@@`
WHERE
  SegmentsOverlap IS NULL
  AND NOT isBackgroundSegment
GROUP BY
  PatientID, StudyInstanceUID, SeriesInstanceUID, SOPInstanceUID
ORDER BY
  segmentsInObject DESC, PatientID, SeriesInstanceUID
