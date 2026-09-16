#standardSQL

# table-description:
# Issue 18 - severity Low. One row per (segmentation series, segmented image
# series): both grids side by side, and what differs.
#
# NOTHING HERE IS A CONFORMANCE FAILURE. PS3.3 A.51.1 requires a segmentation
# to share its source's Frame of Reference - that is issue 17, and it IS a
# rule - but it does not require the same SAMPLING. A segmentation resampled to
# an isotropic grid, or cropped to a bounding box, is conformant and sometimes
# deliberate. What a mismatch costs is that a consumer has to resample before
# the two can be overlaid or compared voxel-wise, so this belongs in the report
# as a documented property of the delivery, not in the defect list.
#
# Two absences are reported here too, and both matter more than any mismatch:
#
#   * sourceImageReferenceLevel != 'SERIES' - the object names no segmented
#     SERIES at all, so nothing downstream can find the images by series. Where
#     it is INSTANCE_ONLY the derivation IS recorded, per SOP instance, and
#     recovering the series from it needs an instance-level index.
#   * the referenced series is not in @@IMAGE_TABLE@@ - the same dangling
#     reference issue 17 reports. Point @@IMAGE_TABLE@@ at
#     bigquery-public-data.idc_current.dicom_all and this column answers
#     "is the segmented series available in IDC".
#
# @@SPACING_TOLERANCE@@ is the RELATIVE tolerance for spacings and
# thicknesses, 0.001 to match seg_geometry.py. Relative rather than absolute
# because DS rounding scales with the value: a segmentation written as
# 7.031003e-01 against a source's 0.7031 is the same spacing, and an absolute
# tolerance tight enough for 0.5 mm would reject a 5 mm one that differs only
# in its last digit. Substitute the SAME value seg_geometry.py was given, or
# the two will disagree about which series are tagged.
#
# @@ORIENTATION_TOLERANCE@@ is the per-component tolerance on the direction
# cosines, 0.001 to match 0.1 degrees closely enough for a Baseline check. The
# script compares the two orientations as an ANGLE; doing that in SQL needs
# trigonometry over arrays for no practical gain, so this compares component by
# component, which is stricter for a large rotation and looser for a tiny one.
#
# ONE COMPARISON THIS FILE CANNOT MAKE: the MEASURED slice spacing of the
# source. SliceThickness is nominal and says nothing about gaps or overlap
# between slices, and the real spacing has to be computed from the Image
# Position (Patient) of every instance projected onto the slice normal. Run
# `seg_geometry.py --probe full`, or `--files`, for that. Here, the slice
# comparison uses SpacingBetweenSlices where the source declares it, and is
# silent otherwise - silent, not clean.
#
#   sed -e 's|@@SEG_ATTRIBUTES@@|<project>.<dataset>.seg_attributes|' \
#       -e 's|@@IMAGE_TABLE@@|bigquery-public-data.idc_current.dicom_all|' \
#       -e 's|@@SPACING_TOLERANCE@@|0.001|' \
#       -e 's|@@ORIENTATION_TOLERANCE@@|0.001|' \
#       18_geometry.sql | bq query --use_legacy_sql=false

WITH
  # One row per (segmentation series, referenced series). Grouped at the series
  # and not the object because a grid is a property of the series in practice;
  # `objects` says how many objects the row stands for.
  segGeometry AS (
    SELECT
      SeriesInstanceUID,
      referencedSeriesInstanceUID,
      ANY_VALUE(PatientID) AS PatientID,
      ANY_VALUE(StudyInstanceUID) AS StudyInstanceUID,
      ANY_VALUE(sourceImageReferenceLevel) AS sourceImageReferenceLevel,
      ANY_VALUE(referencedSeriesCount) AS referencedSeriesCount,
      ANY_VALUE(referencedSeriesFound) AS referencedSeriesFound,
      ANY_VALUE(referencedModality) AS referencedModality,
      ANY_VALUE(Rows) AS segRows,
      ANY_VALUE(Columns) AS segColumns,
      ANY_VALUE(PixelSpacing) AS segPixelSpacing,
      ANY_VALUE(SliceThickness) AS segSliceThickness,
      ANY_VALUE(SpacingBetweenSlices) AS segSpacingBetweenSlices,
      ANY_VALUE(ImageOrientationPatient) AS segImageOrientationPatient,
      ANY_VALUE(geometrySource) AS segGeometrySource,
      ANY_VALUE(SeriesDescription) AS SeriesDescription,
      ANY_VALUE(viewer_url) AS viewer_url,
      COUNT(DISTINCT SOPInstanceUID) AS objects
    FROM
      `@@SEG_ATTRIBUTES@@`
    WHERE
      NOT isBackgroundSegment
    GROUP BY
      SeriesInstanceUID, referencedSeriesInstanceUID
  ),

  # The segmented series' own grid, one row per series. An attribute whose
  # instances DISAGREE is left NULL rather than averaged: a series whose slices
  # have different in-plane spacings has no one spacing, and reporting the
  # first instance's would compare the segmentation against a grid that does
  # not exist. COUNT(DISTINCT) over the string form is what decides that.
  sourceGeometry AS (
    SELECT
      SeriesInstanceUID,
      ANY_VALUE(Modality) AS Modality,
      COUNT(DISTINCT SOPInstanceUID) AS instanceCount,
      IF(COUNT(DISTINCT CAST(Rows AS STRING)) = 1,
         ANY_VALUE(Rows), NULL) AS sourceRows,
      IF(COUNT(DISTINCT CAST(Columns AS STRING)) = 1,
         ANY_VALUE(Columns), NULL) AS sourceColumns,
      IF(COUNT(DISTINCT FORMAT('%T', PixelSpacing)) = 1,
         ANY_VALUE(PixelSpacing), NULL) AS sourcePixelSpacing,
      IF(COUNT(DISTINCT FORMAT('%T', ImageOrientationPatient)) = 1,
         ANY_VALUE(ImageOrientationPatient),
         NULL) AS sourceImageOrientationPatient,
      IF(COUNT(DISTINCT CAST(SliceThickness AS STRING)) = 1,
         ANY_VALUE(SliceThickness), NULL) AS sourceSliceThickness,
      IF(COUNT(DISTINCT CAST(SpacingBetweenSlices AS STRING)) = 1,
         ANY_VALUE(SpacingBetweenSlices), NULL) AS sourceSpacingBetweenSlices
    FROM
      `@@IMAGE_TABLE@@`
    WHERE
      SeriesInstanceUID IN (
        SELECT referencedSeriesInstanceUID FROM segGeometry)
    GROUP BY
      SeriesInstanceUID
  ),

  # The segmentation's own spacings and orientation, parsed out of the "a/b/c"
  # strings the view carries. The view stores them verbatim - what the producer
  # wrote - so the numbers have to be recovered here before anything is
  # compared. SAFE_CAST rather than CAST: a malformed value becomes NULL and
  # the dimension reports NOT_COMPARED, which is the honest answer.
  parsed AS (
    SELECT
      segGeometry.*,
      ARRAY(
        SELECT SAFE_CAST(part AS FLOAT64)
        FROM UNNEST(SPLIT(segPixelSpacing, '/')) AS part) AS segSpacing,
      ARRAY(
        SELECT SAFE_CAST(part AS FLOAT64)
        FROM UNNEST(SPLIT(segImageOrientationPatient, '/')) AS part)
        AS segOrientation,
      SAFE_CAST(segSliceThickness AS FLOAT64) AS segThickness,
      SAFE_CAST(segSpacingBetweenSlices AS FLOAT64) AS segSpacing3d
    FROM
      segGeometry
  ),

  compared AS (
    SELECT
      parsed.*,
      sourceGeometry.Modality AS sourceModality,
      sourceGeometry.instanceCount AS sourceInstanceCount,
      sourceGeometry.sourceRows,
      sourceGeometry.sourceColumns,
      sourceGeometry.sourcePixelSpacing,
      sourceGeometry.sourceImageOrientationPatient,
      sourceGeometry.sourceSliceThickness,
      sourceGeometry.sourceSpacingBetweenSlices,

      parsed.segRows IS NOT NULL
        AND sourceGeometry.sourceRows IS NOT NULL
        AND (parsed.segRows != sourceGeometry.sourceRows
             OR parsed.segColumns != sourceGeometry.sourceColumns)
        AS gridSizeDiffers,

      ARRAY_LENGTH(parsed.segSpacing) = 2
        AND ARRAY_LENGTH(sourceGeometry.sourcePixelSpacing) = 2
        AND EXISTS(
          SELECT 1
          FROM UNNEST(parsed.segSpacing) AS mine WITH OFFSET AS i
          WHERE ABS(mine - sourceGeometry.sourcePixelSpacing[OFFSET(i)])
                > @@SPACING_TOLERANCE@@
                  * GREATEST(ABS(mine),
                             ABS(sourceGeometry.sourcePixelSpacing[OFFSET(i)])))
        AS pixelSpacingDiffers,

      ARRAY_LENGTH(parsed.segOrientation) = 6
        AND ARRAY_LENGTH(sourceGeometry.sourceImageOrientationPatient) = 6
        AND EXISTS(
          SELECT 1
          FROM UNNEST(parsed.segOrientation) AS mine WITH OFFSET AS i
          WHERE ABS(
            mine - sourceGeometry.sourceImageOrientationPatient[OFFSET(i)])
            > @@ORIENTATION_TOLERANCE@@)
        AS orientationDiffers,

      parsed.segSpacing3d IS NOT NULL
        AND sourceGeometry.sourceSpacingBetweenSlices IS NOT NULL
        AND ABS(parsed.segSpacing3d
                - sourceGeometry.sourceSpacingBetweenSlices)
            > @@SPACING_TOLERANCE@@
              * GREATEST(ABS(parsed.segSpacing3d),
                         ABS(sourceGeometry.sourceSpacingBetweenSlices))
        AS sliceSpacingDiffers,

      parsed.segThickness IS NOT NULL
        AND sourceGeometry.sourceSliceThickness IS NOT NULL
        AND ABS(parsed.segThickness - sourceGeometry.sourceSliceThickness)
            > @@SPACING_TOLERANCE@@
              * GREATEST(ABS(parsed.segThickness),
                         ABS(sourceGeometry.sourceSliceThickness))
        AS sliceThicknessDiffers,

      # What could NOT be compared. Reported, not dropped: a dimension nobody
      # looked at must read as neither a match nor a mismatch.
      ARRAY_LENGTH(parsed.segOrientation) != 6
        OR ARRAY_LENGTH(sourceGeometry.sourceImageOrientationPatient) != 6
        AS orientationNotCompared,
      parsed.segSpacing3d IS NULL
        OR sourceGeometry.sourceSpacingBetweenSlices IS NULL
        AS sliceSpacingNotCompared,
      parsed.segThickness IS NULL
        OR sourceGeometry.sourceSliceThickness IS NULL
        AS sliceThicknessNotCompared
    FROM
      parsed
    LEFT JOIN
      sourceGeometry
      ON sourceGeometry.SeriesInstanceUID = parsed.referencedSeriesInstanceUID
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
  # SeriesInstanceUID of the segmented image series, NULL when the object names
  # none
  referencedSeriesInstanceUID,
  # description:
  # Semicolon-separated observations, what differs first. None of them is a
  # conformance verdict - see the header. GEOMETRY_MATCHES means every
  # dimension agreed; GEOMETRY_MATCHES_PARTIAL means every dimension that COULD
  # be compared agreed, which is not the same statement.
  ARRAY_TO_STRING(
    ARRAY(
      SELECT observation
      FROM UNNEST([
        IF(gridSizeDiffers, 'GRID_SIZE_DIFFERS', NULL),
        IF(pixelSpacingDiffers, 'PIXEL_SPACING_DIFFERS', NULL),
        IF(orientationDiffers, 'ORIENTATION_DIFFERS', NULL),
        IF(sliceSpacingDiffers, 'SLICE_SPACING_DIFFERS', NULL),
        IF(sliceThicknessDiffers, 'SLICE_THICKNESS_DIFFERS', NULL),
        IF(segGeometrySource = 'PER_FRAME_VARYING',
           'SEG_GEOMETRY_PER_FRAME', NULL),
        IF(segGeometrySource = 'ABSENT', 'SEG_GEOMETRY_ABSENT', NULL),
        IF(referencedSeriesInstanceUID IS NULL, 'NO_REFERENCED_SERIES', NULL),
        IF(referencedSeriesInstanceUID IS NOT NULL AND sourceRows IS NULL
             AND sourceInstanceCount IS NULL,
           'SOURCE_NOT_IN_IDC', NULL),
        IF(referencedSeriesInstanceUID IS NOT NULL AND orientationNotCompared,
           'ORIENTATION_NOT_COMPARED', NULL),
        IF(referencedSeriesInstanceUID IS NOT NULL AND sliceSpacingNotCompared,
           'SLICE_SPACING_NOT_COMPARED', NULL),
        IF(referencedSeriesInstanceUID IS NOT NULL
             AND sliceThicknessNotCompared,
           'SLICE_THICKNESS_NOT_COMPARED', NULL),
        IF(referencedSeriesCount > 1, 'MULTIPLE_REFERENCED_SERIES', NULL),
        IF(sourceInstanceCount IS NOT NULL
             AND NOT (gridSizeDiffers OR pixelSpacingDiffers
                      OR orientationDiffers OR sliceSpacingDiffers
                      OR sliceThicknessDiffers),
           IF(orientationNotCompared OR sliceSpacingNotCompared
                OR sliceThicknessNotCompared,
              'GEOMETRY_MATCHES_PARTIAL', 'GEOMETRY_MATCHES'),
           NULL)
      ]) AS observation
      WHERE observation IS NOT NULL),
    '; ') AS geometryObservation,
  # description:
  # SERIES, INSTANCE_ONLY or NONE - how the object says which images it
  # segments. Anything but SERIES is why referencedSeriesInstanceUID is NULL.
  sourceImageReferenceLevel,
  # description:
  # TRUE when the segmented series is in @@IMAGE_TABLE@@. Point that at
  # idc_current.dicom_all and this is "available in IDC".
  sourceInstanceCount IS NOT NULL AS sourceFound,
  # description:
  # Modality of the segmented series
  sourceModality,
  # description:
  # Number of instances in the segmented series
  sourceInstanceCount,
  # description:
  # Rows of the segmentation
  segRows,
  # description:
  # Rows of the segmented series, NULL when its instances disagree
  sourceRows,
  # description:
  # Columns of the segmentation
  segColumns,
  # description:
  # Columns of the segmented series
  sourceColumns,
  # description:
  # PixelSpacing of the segmentation as "row/column", verbatim
  segPixelSpacing,
  # description:
  # PixelSpacing of the segmented series
  sourcePixelSpacing,
  # description:
  # ImageOrientationPatient of the segmentation, six "/"-joined cosines
  segImageOrientationPatient,
  # description:
  # ImageOrientationPatient of the segmented series
  sourceImageOrientationPatient,
  # description:
  # SpacingBetweenSlices of the segmentation
  segSpacingBetweenSlices,
  # description:
  # SpacingBetweenSlices DECLARED by the segmented series. NOT measured from
  # the slice positions - see the header.
  sourceSpacingBetweenSlices,
  # description:
  # SliceThickness of the segmentation
  segSliceThickness,
  # description:
  # SliceThickness of the segmented series
  sourceSliceThickness,
  # description:
  # SHARED, PER_FRAME_UNIFORM, PER_FRAME_VARYING or ABSENT - where the
  # segmentation's geometry was found
  segGeometrySource,
  # description:
  # Segmentation objects this row stands for
  objects,
  # description:
  # Items in ReferencedSeriesSequence; above 1, this row describes only the
  # first
  referencedSeriesCount,
  # description:
  # DICOM SeriesDescription of the segmentation series
  SeriesDescription,
  # description:
  # URL opening the segmentation series in the viewer
  viewer_url
FROM
  compared
ORDER BY
  gridSizeDiffers OR pixelSpacingDiffers OR orientationDiffers
    OR sliceSpacingDiffers OR sliceThicknessDiffers DESC,
  PatientID,
  SeriesInstanceUID
