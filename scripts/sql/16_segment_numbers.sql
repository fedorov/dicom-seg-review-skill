#standardSQL

# table-description:
# Issue 16 - severity Medium. Objects whose Segment Numbers (0062,0004) break
# PS3.3 C.8.20.2.4: "Segment Number (0062,0004) shall be unique within each
# Instance", and, where Segmentation Type (0062,0001) is BINARY or FRACTIONAL,
# "shall start at a Value of 1, and increase monotonically by 1".
#
# One row per affected OBJECT. Fully computed - this will follow a new
# delivery.
#
# Background rows are deliberately NOT excluded here, unlike every other
# query: a LABELMAP's Segment Number 0 is legitimate, and a 0 on a BINARY
# object is exactly the defect. LABELMAP objects are held to uniqueness only -
# their numbers are pixel values and need not be consecutive.
#
# The order of items in the Segment Sequence is not recoverable from the
# per-segment view, so "increase by 1" is checked as "the distinct numbers are
# exactly 1..n". An object numbered 2, 1 passes here and is dciodvfy's to
# report (issue 9). A NULL Segment Number is issue 4's finding and is ignored
# here.

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
  # DICOM SegmentationType: BINARY, FRACTIONAL or LABELMAP
  ANY_VALUE(SegmentationType) AS SegmentationType,
  # description:
  # DUPLICATE when a number repeats within the object; NOT_SEQUENTIAL when a
  # BINARY or FRACTIONAL object's numbers are not exactly 1..n; both when both.
  ARRAY_TO_STRING(
    ARRAY(
      SELECT problem FROM UNNEST([
        IF(COUNTIF(SegmentNumber IS NOT NULL) > COUNT(DISTINCT SegmentNumber),
           'DUPLICATE', NULL),
        IF(ANY_VALUE(SegmentationType) IN ('BINARY', 'FRACTIONAL')
             AND COUNT(DISTINCT SegmentNumber) > 0
             AND (MIN(SegmentNumber) != 1
                  OR MAX(SegmentNumber) != COUNT(DISTINCT SegmentNumber)),
           'NOT_SEQUENTIAL', NULL)]) AS problem
      WHERE problem IS NOT NULL),
    '; ') AS segmentNumberProblem,
  # description:
  # The distinct Segment Numbers the object carries, ascending, slash-separated
  ARRAY_TO_STRING(
    ARRAY(SELECT CAST(n AS STRING)
          FROM UNNEST(ARRAY_AGG(DISTINCT SegmentNumber IGNORE NULLS)) AS n
          ORDER BY n),
    '/') AS segmentNumbers,
  # description:
  # Number of segments in the object, Background included
  COUNT(*) AS segmentsInObject,
  # description:
  # DICOM SeriesDescription of the segmentation series
  ANY_VALUE(SeriesDescription) AS SeriesDescription,
  # description:
  # URL opening the segmentation series in the viewer
  ANY_VALUE(viewer_url) AS viewer_url
FROM
  `@@SEG_ATTRIBUTES@@`
GROUP BY
  SOPInstanceUID
HAVING
  COUNTIF(SegmentNumber IS NOT NULL) > COUNT(DISTINCT SegmentNumber)
  OR (ANY_VALUE(SegmentationType) IN ('BINARY', 'FRACTIONAL')
      AND COUNT(DISTINCT SegmentNumber) > 0
      AND (MIN(SegmentNumber) != 1
           OR MAX(SegmentNumber) != COUNT(DISTINCT SegmentNumber)))
ORDER BY
  segmentNumberProblem, PatientID, SOPInstanceUID
