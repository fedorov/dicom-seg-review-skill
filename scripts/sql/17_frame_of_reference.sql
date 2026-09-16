#standardSQL

# table-description:
# Issue 17 - severity Medium. Objects whose Frame of Reference differs from
# the image series they reference, or whose referenced series does not exist
# in @@IMAGE_TABLE@@.
#
# One row per affected OBJECT. Fully computed - this will follow a new
# delivery.
#
# PS3.3 A.51.1 (Segmentation IOD Description): "If the referenced images have
# a defined Frame of Reference, the Segmentation Instance shall have the same
# Frame of Reference." A viewer decides whether to overlay a segmentation on an
# image by comparing exactly these two UIDs, so a mismatch is a segmentation
# nobody can display on its source - non-conformant AND unusable, but
# detectable, hence Medium rather than High.
#
# A referenced series absent from @@IMAGE_TABLE@@ is the dangling reference:
# the object claims a source that the store does not hold. Either the images
# were never delivered or the UID is wrong; the report cannot tell which.
#
# Decided only where the referenced series was resolved. referencedFrameOfReferenceUID
# is NULL when the series is not in @@IMAGE_TABLE@@, which on this path is the
# same fact as referencedSeriesFound = FALSE; on the file and DICOMweb paths it
# is also NULL when --resolve-referenced was not passed, and the silence then
# means "not checked", never "matches".

SELECT
  # description:
  # DICOM PatientID
  ANY_VALUE(PatientID) AS PatientID,
  # description:
  # DICOM StudyInstanceUID of the study containing the segmentation
  ANY_VALUE(StudyInstanceUID) AS StudyInstanceUID,
  # description:
  # DICOM SeriesInstanceUID of the segmentation series
  ANY_VALUE(SeriesInstanceUID) AS SeriesInstanceUID,
  # description:
  # DICOM SOPInstanceUID of the affected object
  SOPInstanceUID,
  # description:
  # REFERENCED_SERIES_MISSING when the referenced series is not in
  # @@IMAGE_TABLE@@; FRAME_OF_REFERENCE_MISMATCH when it is and its Frame of
  # Reference differs from the segmentation's
  IF(NOT ANY_VALUE(referencedSeriesFound),
     'REFERENCED_SERIES_MISSING',
     'FRAME_OF_REFERENCE_MISMATCH') AS frameOfReferenceProblem,
  # description:
  # The segmentation's FrameOfReferenceUID
  ANY_VALUE(FrameOfReferenceUID) AS FrameOfReferenceUID,
  # description:
  # SeriesInstanceUID of the referenced image series
  ANY_VALUE(referencedSeriesInstanceUID) AS referencedSeriesInstanceUID,
  # description:
  # FrameOfReferenceUID of the referenced image series, NULL when not found
  ANY_VALUE(referencedFrameOfReferenceUID) AS referencedFrameOfReferenceUID,
  # description:
  # Modality of the referenced image series
  ANY_VALUE(referencedModality) AS referencedModality,
  # description:
  # DICOM SeriesDescription of the segmentation series
  ANY_VALUE(SeriesDescription) AS SeriesDescription,
  # description:
  # URL opening the segmentation series in the viewer
  ANY_VALUE(viewer_url) AS viewer_url
FROM
  `@@SEG_ATTRIBUTES@@`
WHERE
  referencedSeriesInstanceUID IS NOT NULL
  AND (referencedSeriesFound = FALSE
       OR (referencedFrameOfReferenceUID IS NOT NULL
           AND FrameOfReferenceUID != referencedFrameOfReferenceUID))
GROUP BY
  SOPInstanceUID
ORDER BY
  frameOfReferenceProblem, PatientID, SOPInstanceUID
