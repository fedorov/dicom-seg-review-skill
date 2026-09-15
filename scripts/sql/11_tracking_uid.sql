#standardSQL

# table-description:
# Issue 7 - severity Medium. TrackingUID (0062,0021) exists to identify a
# FINDING, but a shared UID can mean several different things, so "same UID"
# cannot be read as "same finding, different observation" without inspecting the
# surrounding study and series structure.
#
# One row per shared TrackingUID. Fully computed.
#
# `sharingPattern` is the column to work from:
#   CROSS_PATIENT     one identifier spanning two people. A serious problem, and
#                     absent from both batches this was built against.
#   WITHIN_SERIES     repeated inside a single series. Also absent from both.
#   WITHIN_STUDY      two segmentation series of one study; UNCLEAR whether that
#                     is one finding annotated twice or a reused UID. These are
#                     the ones to escalate.
#   SEED_AND_LESION   a seed-point series paired with its lesion segmentations
#   LONGITUDINAL      across studies of one patient - the intended use
#
# REPORT THE REASSURING NEGATIVES TOO. That no UID is shared across patients and
# none is repeated within a series bounds the problem, and a report that lists
# only what is broken cannot be acted on proportionately.
#
# Skip this query entirely if the source schema has no TrackingUID column - that
# absence means no instance in the batch tracks findings, which is itself worth
# a sentence in the report.

WITH
  shared AS (
    SELECT
      TrackingUID,
      COUNT(*) AS segments,
      COUNT(DISTINCT PatientID) AS patients,
      COUNT(DISTINCT StudyInstanceUID) AS studies,
      COUNT(DISTINCT SeriesInstanceUID) AS series,
      # Adjust this predicate to the batch's own SeriesDescription convention.
      LOGICAL_OR(UPPER(SeriesDescription) LIKE '%SEED POINT%') AS hasSeedPoints,
      ANY_VALUE(PatientID) AS PatientID,
      ANY_VALUE(StudyInstanceUID) AS StudyInstanceUID,
      STRING_AGG(DISTINCT SegmentLabel ORDER BY SegmentLabel) AS segmentLabels,
      STRING_AGG(DISTINCT SeriesInstanceUID ORDER BY SeriesInstanceUID)
        AS seriesInstanceUIDs,
      ANY_VALUE(viewer_url) AS viewer_url
    FROM `@@SEG_ATTRIBUTES@@`
    WHERE TrackingUID IS NOT NULL AND NOT isBackgroundSegment
    GROUP BY TrackingUID
    HAVING COUNT(*) > 1
  )

SELECT
  # description:
  # DICOM PatientID. Check `patients` before assuming a UID stays within one.
  PatientID,
  # description:
  # DICOM StudyInstanceUID of one of the studies involved; see `studies` for
  # whether the UID spans more than one
  StudyInstanceUID,
  # description:
  # Semicolon-separated SeriesInstanceUIDs the shared UID appears in
  seriesInstanceUIDs,
  # description:
  # The shared DICOM TrackingUID
  TrackingUID,
  # description:
  # How the UID is shared, and so what it is asserting
  CASE
    WHEN patients > 1 THEN 'CROSS_PATIENT'
    WHEN studies > 1 THEN 'LONGITUDINAL'
    WHEN series = 1 THEN 'WITHIN_SERIES'
    WHEN hasSeedPoints THEN 'SEED_AND_LESION'
    ELSE 'WITHIN_STUDY'
    END AS sharingPattern,
  # description:
  # Number of segments carrying this TrackingUID
  segments,
  # description:
  # Number of distinct patients it spans. Above 1 is a serious problem.
  patients,
  # description:
  # Number of distinct studies it spans
  studies,
  # description:
  # Number of distinct series it spans
  series,
  # description:
  # The SegmentLabels involved. Identical labels across a WITHIN_STUDY pair are
  # what make it unclear whether the finding was annotated twice.
  segmentLabels,
  # description:
  # URL opening one of the segmentation series in the viewer
  viewer_url
FROM
  shared
ORDER BY
  CASE sharingPattern
    WHEN 'CROSS_PATIENT' THEN 0 WHEN 'WITHIN_SERIES' THEN 1
    WHEN 'WITHIN_STUDY' THEN 2 WHEN 'SEED_AND_LESION' THEN 3 ELSE 4 END,
  PatientID,
  TrackingUID
