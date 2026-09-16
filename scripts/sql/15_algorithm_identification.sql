#standardSQL

# table-description:
# Issue 14 - a segment produced by an algorithm that does not say which
# algorithm. One row per affected segment, listing every reason. Fully computed
# - this will follow a new delivery.
#
# Segment Algorithm Name (0062,0009) is Type 1C in the Segmentation Image
# Module: "Required if Segment Algorithm Type (0062,0008) is not MANUAL"
# (PS3.3 C.8.20.2). So on an AUTOMATIC or SEMIAUTOMATIC segment its absence is
# a conformance violation, and that is the only one of the four reasons below
# that is.
#
# The rest are about what the name is worth. (0062,0009) is a bare string with
# nowhere to put a version, a source, or a code for the specific model. That is
# what Segmentation Algorithm Identification Sequence (0062,0007) is for -
# Type 3, PS3.3 Table 10-19, holding Algorithm Name (0066,0036) and Algorithm
# Version (0066,0031) as Type 1, plus Algorithm Source (0024,0202) and
# Algorithm Name Code Sequence (0066,0030), the closest DICOM has to a model
# identifier. Without it a delivery cannot be attributed to a model at all, and
# "which version produced these annotations" has no answer.
#
# Manufacturer (0008,0070), ManufacturerModelName (0008,1090) and
# SoftwareVersions (0018,1020) are carried alongside for context but are NOT a
# substitute: on a converted segmentation they name the converter - dcmqi,
# highdicom - and not the model that did the segmenting. Read them as a
# cross-check instead: a batch whose ManufacturerModelName is an inference
# toolkit while every SegmentAlgorithmType says MANUAL is misdescribing itself,
# and that is worth a sentence in the report even though no query returns it.
#
# Background segments are excluded, as everywhere else.

WITH
  # Names that satisfy the letter of 1C while identifying nothing. Toolkit
  # names are here because a converter's name says what WROTE the object, not
  # what segmented it. Extend for your producer, and say in the report that
  # you did - this list is a judgement, unlike everything else in this query.
  uninformative AS (
    SELECT * FROM UNNEST([
      '', '-', 'n/a', 'na', 'none', 'null', 'nil', 'unknown', 'unspecified',
      'not specified', 'not applicable', 'tbd', 'todo', 'test', 'default',
      'algorithm', 'segmentation', 'segment', 'auto', 'automatic', 'manual',
      'ai', 'model', 'dcmqi', 'pydicom', 'highdicom', 'itk', 'simpleitk',
      'slicer', '3d slicer', 'plastimatch', 'dcmtk'
    ]) AS name
  ),

  flagged AS (
    SELECT
      seg.*,
      ARRAY(
        SELECT issue FROM UNNEST([
          IF(seg.SegmentAlgorithmName IS NULL
               OR TRIM(seg.SegmentAlgorithmName) = '',
             'NAME_MISSING', NULL),
          IF(seg.SegmentAlgorithmName IS NOT NULL
               AND TRIM(seg.SegmentAlgorithmName) != ''
               AND LOWER(TRIM(seg.SegmentAlgorithmName))
                   IN (SELECT name FROM uninformative),
             'NAME_UNINFORMATIVE', NULL),
          IF(NOT IFNULL(seg.hasAlgorithmIdentification, FALSE),
             'NO_IDENTIFICATION', NULL),
          IF(IFNULL(seg.hasAlgorithmIdentification, FALSE)
               AND (seg.AlgorithmVersion IS NULL
                    OR TRIM(seg.AlgorithmVersion) = ''),
             'NO_VERSION', NULL)
        ]) AS issue
        WHERE issue IS NOT NULL) AS algorithmIssues
    FROM
      `@@SEG_ATTRIBUTES@@` AS seg
    WHERE
      NOT seg.isBackgroundSegment
      # Type 1C bites only here. A MANUAL segment is required to say nothing.
      AND UPPER(IFNULL(seg.SegmentAlgorithmType, ''))
          IN ('AUTOMATIC', 'SEMIAUTOMATIC')
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
  # DICOM SegmentAlgorithmType (0062,0008): AUTOMATIC or SEMIAUTOMATIC here,
  # since MANUAL requires none of this
  SegmentAlgorithmType,
  # description:
  # Every reason this segment fails to identify its algorithm:
  #   NAME_MISSING        (0062,0009) absent. Type 1C violation.
  #   NAME_UNINFORMATIVE  a name that identifies nothing
  #   NO_IDENTIFICATION   no (0062,0007), so no version and no model code
  #   NO_VERSION          (0062,0007) present, AlgorithmVersion (0066,0031)
  #                       empty - and it is Type 1 within that sequence
  ARRAY_TO_STRING(algorithmIssues, '; ') AS algorithmIssues,
  # description:
  # DICOM SegmentAlgorithmName (0062,0009) as recorded
  SegmentAlgorithmName,
  # description:
  # Algorithm Name (0066,0036) from (0062,0007)
  AlgorithmName,
  # description:
  # Algorithm Version (0066,0031) from (0062,0007) - the answer to "which
  # version produced these annotations"
  AlgorithmVersion,
  # description:
  # Algorithm Source (0024,0202) from (0062,0007)
  AlgorithmSource,
  # description:
  # CodeMeaning of Algorithm Name Code Sequence (0066,0030), the coded model
  # identity where one was given
  AlgorithmNameCodeMeaning,
  # description:
  # DICOM ManufacturerModelName. Context, NOT a substitute: on a converted
  # segmentation this names the converter.
  ManufacturerModelName,
  # description:
  # First value of DICOM SoftwareVersions. Same caveat.
  SoftwareVersion,
  # description:
  # DICOM SeriesDescription of the segmentation series
  SeriesDescription,
  # description:
  # URL opening the segmentation series in the viewer
  viewer_url
FROM
  flagged
WHERE
  ARRAY_LENGTH(algorithmIssues) > 0
ORDER BY
  # The conformance violation first, then the rest.
  IF('NAME_MISSING' IN UNNEST(algorithmIssues), 0, 1),
  PatientID, SeriesInstanceUID, SegmentNumber
