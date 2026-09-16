#standardSQL

# table-description:
# One row per segment of every DICOM Segmentation object in @@SOURCE_TABLE@@,
# flattening the Segment Sequence (0062,0002). Deploy as a view; every other
# query in this directory reads it.
#
#   sed -e 's|@@SOURCE_TABLE@@|<project>.<dataset>.<table>|' \
#       -e 's|@@IMAGE_TABLE@@|<table holding the segmented IMAGES>|' \
#       -e 's|@@VIEWER_BASE@@|https://viewer.example.org/...|' \
#       01_seg_attributes.sql > /tmp/v.sql
#   bq mk --project_id=<project> --use_legacy_sql=false \
#     --view "$(cat /tmp/v.sql)" <dataset>.seg_attributes
#   # replace `mk` with `update` once it exists
#
# THIS CARRIES ATTRIBUTES ONLY - no data-quality judgements. Each check has its
# own numbered file here, and the verdicts that need a human live in
# 04_code_review_template.sql. Keeping the three apart is what lets the computed
# checks follow a new delivery while the curated ones are re-derived.
#
# Granularity is (SOPInstanceUID, SegmentNumber). A segment with a NULL
# SegmentNumber breaks that key; 08_malformed_segment.sql reports it, and any
# claim about the key should be qualified accordingly.
#
# BEFORE DEPLOYING, check which attributes the source table actually has:
#
#   SELECT field_path, data_type
#   FROM `<project>.<dataset>.INFORMATION_SCHEMA.COLUMN_FIELD_PATHS`
#   WHERE table_name = '<table>'
#     AND (field_path LIKE '%Segment%' OR field_path LIKE '%AnatomicRegion%'
#          OR field_path LIKE '%Tracking%')
#
# A Google Healthcare API export creates a column ONLY for attributes some
# instance populates, so referencing an absent one is a COMPILE error, not a
# NULL column. Delete the columns your source lacks - and report each absence,
# because it means no instance in the batch uses that attribute. Absent in real
# batches: TrackingID / TrackingUID, AnatomicRegionModifierSequence,
# SegmentAlgorithmName, SegmentedPropertyTypeModifierCodeSequence,
# SegmentationAlgorithmIdentificationSequence, RecommendedDisplayCIELabValue.
#
# Two columns this view CANNOT fill, on any BigQuery source: TransferSyntaxUID
# (file meta, which an export drops) and anything about the pixel data. Issues
# 11 and 12 are therefore unavailable here, exactly as issue 9 is - run
# scripts/seg_encoding.py over the files, or say in the report that the
# encoding was not reviewed.
#
# The code sequences are taken at SAFE_OFFSET(0). `multiValuedCodeSequence` is
# the guard that says so, and is the one derived column here because it protects
# every query built on this view. Confirm it is FALSE everywhere before
# trusting any result.

WITH
  # Segmentation instances only. SOPClassUID is more reliable than
  # Modality = "SEG", and accepting both classes means a future labelmap
  # delivery is picked up rather than silently dropped.
  segInstances AS (
    SELECT
      PatientID,
      StudyInstanceUID,
      SeriesInstanceUID,
      SOPInstanceUID,
      SeriesNumber,
      SeriesDescription,
      SeriesDate,
      FrameOfReferenceUID,
      SegmentationType,
      SegmentsOverlap,
      PhotometricInterpretation,
      Manufacturer,
      ManufacturerModelName,
      SoftwareVersions,
      NumberOfFrames,
      SegmentSequence,
      ARRAY_LENGTH(ReferencedSeriesSequence) AS referencedSeriesCount,
      ReferencedSeriesSequence[SAFE_OFFSET(0)] AS referencedSeries
    FROM
      `@@SOURCE_TABLE@@`
    WHERE
      SOPClassUID IN (
        # Segmentation Storage
        '1.2.840.10008.5.1.4.1.1.66.4',
        # Labelmap Segmentation Storage
        '1.2.840.10008.5.1.4.1.1.66.7')
  ),

  # The segmented image series. @@IMAGE_TABLE@@ is a SEPARATE placeholder from
  # @@SOURCE_TABLE@@ because the two real cases differ:
  #
  #   * a store holding images AND segmentations - set both placeholders to the
  #     same table
  #   * a store holding ONLY segmentations - set this to wherever the images
  #     live, e.g. bigquery-public-data.idc_current.dicom_all. Everything about
  #     the segmented series then comes from there, since
  #     ReferencedSeriesSequence names only the SeriesInstanceUID.
  #
  # Cost: an IN list restricts the RESULT, not the scan, so a table clustered on
  # something else is read in full (~7 GB for idc_current.dicom_all). If that
  # matters, materialise this CTE into a small table once and point the view at
  # it.
  imageSeries AS (
    SELECT
      SeriesInstanceUID,
      ANY_VALUE(Modality) AS Modality,
      ANY_VALUE(BodyPartExamined) AS BodyPartExamined
    FROM
      `@@IMAGE_TABLE@@`
    WHERE
      SeriesInstanceUID IN (
        SELECT referencedSeries.SeriesInstanceUID FROM segInstances)
    GROUP BY
      SeriesInstanceUID
  )

SELECT
  # description:
  # DICOM PatientID
  segInstances.PatientID,

  # description:
  # DICOM StudyInstanceUID of the study containing the segmentation
  segInstances.StudyInstanceUID,

  # description:
  # DICOM SeriesInstanceUID of the segmentation series
  segInstances.SeriesInstanceUID,

  # description:
  # DICOM SOPInstanceUID of the segmentation object the segment belongs to
  segInstances.SOPInstanceUID,

  # description:
  # DICOM SegmentNumber (0062,0004), unique within its segmentation object.
  # Starts at 0 on labelmap objects, where 0 is the Background segment.
  segment.SegmentNumber,

  # description:
  # DICOM SeriesDescription of the segmentation series
  segInstances.SeriesDescription,

  # description:
  # DICOM SeriesNumber of the segmentation series
  segInstances.SeriesNumber,

  # description:
  # DICOM SeriesDate of the segmentation series
  segInstances.SeriesDate,

  # description:
  # DICOM FrameOfReferenceUID shared with the images the segment was drawn on
  segInstances.FrameOfReferenceUID,

  # description:
  # DICOM SegmentLabel (0062,0005). Usually NOT independent evidence - it is
  # commonly the CodeMeaning plus an enumerator. Measure how often the two
  # differ before leaning on it.
  segment.SegmentLabel,

  # description:
  # DICOM SegmentDescription (0062,0006)
  segment.SegmentDescription,

  # description:
  # DICOM TrackingID (0062,0020). Identifies a finding, not a segment.
  # DELETE THIS COLUMN if the source schema lacks it.
  segment.TrackingID,

  # description:
  # DICOM TrackingUID (0062,0021) - see 11_tracking_uid.sql.
  # DELETE THIS COLUMN if the source schema lacks it.
  segment.TrackingUID,

  # description:
  # CodeValue of AnatomicRegionSequence (0008,2218), the anatomic location
  segment.AnatomicRegionSequence[SAFE_OFFSET(0)].CodeValue
    AS AnatomicRegionCodeValue,

  # description:
  # CodingSchemeDesignator of AnatomicRegionSequence, e.g. "SCT"
  segment.AnatomicRegionSequence[SAFE_OFFSET(0)].CodingSchemeDesignator
    AS AnatomicRegionCodingSchemeDesignator,

  # description:
  # CodeMeaning of AnatomicRegionSequence. Often uncontrolled free text - see
  # 02_ambiguous_code.sql and 05_anatomy_conflict.sql.
  segment.AnatomicRegionSequence[SAFE_OFFSET(0)].CodeMeaning
    AS AnatomicRegionCodeMeaning,

  # description:
  # CodeValue of AnatomicRegionModifierSequence (0008,2220), which carries
  # post-coordinated laterality (CID 244).
  # DELETE THIS COLUMN if the source schema lacks it - and report the absence:
  # it means the batch expresses laterality by pre-coordination instead.
  segment.AnatomicRegionSequence[SAFE_OFFSET(0)]
    .AnatomicRegionModifierSequence[SAFE_OFFSET(0)].CodeValue
    AS AnatomicRegionModifierCodeValue,

  # description:
  # CodingSchemeDesignator of AnatomicRegionModifierSequence
  segment.AnatomicRegionSequence[SAFE_OFFSET(0)]
    .AnatomicRegionModifierSequence[SAFE_OFFSET(0)].CodingSchemeDesignator
    AS AnatomicRegionModifierCodingSchemeDesignator,

  # description:
  # CodeMeaning of AnatomicRegionModifierSequence, e.g. "Left"
  segment.AnatomicRegionSequence[SAFE_OFFSET(0)]
    .AnatomicRegionModifierSequence[SAFE_OFFSET(0)].CodeMeaning
    AS AnatomicRegionModifierCodeMeaning,

  # description:
  # CodeValue of SegmentedPropertyCategoryCodeSequence (0062,0003)
  segment.SegmentedPropertyCategoryCodeSequence[SAFE_OFFSET(0)].CodeValue
    AS SegmentedPropertyCategoryCodeValue,

  # description:
  # CodingSchemeDesignator of SegmentedPropertyCategoryCodeSequence
  segment.SegmentedPropertyCategoryCodeSequence[SAFE_OFFSET(0)]
    .CodingSchemeDesignator AS SegmentedPropertyCategoryCodingSchemeDesignator,

  # description:
  # CodeMeaning of SegmentedPropertyCategoryCodeSequence
  segment.SegmentedPropertyCategoryCodeSequence[SAFE_OFFSET(0)].CodeMeaning
    AS SegmentedPropertyCategoryCodeMeaning,

  # description:
  # CodeValue of SegmentedPropertyTypeCodeSequence (0062,000F)
  segment.SegmentedPropertyTypeCodeSequence[SAFE_OFFSET(0)].CodeValue
    AS SegmentedPropertyTypeCodeValue,

  # description:
  # CodingSchemeDesignator of SegmentedPropertyTypeCodeSequence
  segment.SegmentedPropertyTypeCodeSequence[SAFE_OFFSET(0)]
    .CodingSchemeDesignator AS SegmentedPropertyTypeCodingSchemeDesignator,

  # description:
  # CodeMeaning of SegmentedPropertyTypeCodeSequence, e.g. "Lesion"
  segment.SegmentedPropertyTypeCodeSequence[SAFE_OFFSET(0)].CodeMeaning
    AS SegmentedPropertyTypeCodeMeaning,

  # description:
  # CodeValue of SegmentedPropertyTypeModifierCodeSequence (0062,0011), the
  # other place post-coordinated laterality can live.
  # DELETE THIS COLUMN if the source schema lacks it.
  segment.SegmentedPropertyTypeCodeSequence[SAFE_OFFSET(0)]
    .SegmentedPropertyTypeModifierCodeSequence[SAFE_OFFSET(0)].CodeValue
    AS SegmentedPropertyTypeModifierCodeValue,

  # description:
  # CodingSchemeDesignator of SegmentedPropertyTypeModifierCodeSequence
  segment.SegmentedPropertyTypeCodeSequence[SAFE_OFFSET(0)]
    .SegmentedPropertyTypeModifierCodeSequence[SAFE_OFFSET(0)]
    .CodingSchemeDesignator
    AS SegmentedPropertyTypeModifierCodingSchemeDesignator,

  # description:
  # CodeMeaning of SegmentedPropertyTypeModifierCodeSequence: "Left", "Right"
  # or "Right and left"
  segment.SegmentedPropertyTypeCodeSequence[SAFE_OFFSET(0)]
    .SegmentedPropertyTypeModifierCodeSequence[SAFE_OFFSET(0)].CodeMeaning
    AS SegmentedPropertyTypeModifierCodeMeaning,

  # description:
  # DICOM SegmentAlgorithmType (0062,0008): MANUAL, SEMIAUTOMATIC or AUTOMATIC
  segment.SegmentAlgorithmType,

  # description:
  # DICOM SegmentAlgorithmName (0062,0009).
  # DELETE THIS COLUMN if the source schema lacks it.
  segment.SegmentAlgorithmName[SAFE_OFFSET(0)] AS SegmentAlgorithmName,

  # description:
  # TRUE when this segment carries a Segmentation Algorithm Identification
  # Sequence (0062,0007). Type 3, and the only standard home for the algorithm
  # VERSION - see 15_algorithm_identification.sql.
  # DELETE THIS COLUMN, and the five below, if the source schema lacks the
  # sequence - and report the absence: it means no instance identifies the
  # algorithm that produced it beyond a bare name.
  ARRAY_LENGTH(segment.SegmentationAlgorithmIdentificationSequence) > 0
    AS hasAlgorithmIdentification,

  # description:
  # Algorithm Name (0066,0036) within (0062,0007). Type 1 within the sequence.
  segment.SegmentationAlgorithmIdentificationSequence[SAFE_OFFSET(0)]
    .AlgorithmName AS AlgorithmName,

  # description:
  # Algorithm Version (0066,0031) within (0062,0007). Type 1 within the
  # sequence, and the only place a model's version can be recorded.
  segment.SegmentationAlgorithmIdentificationSequence[SAFE_OFFSET(0)]
    .AlgorithmVersion AS AlgorithmVersion,

  # description:
  # Algorithm Source (0024,0202) within (0062,0007): who produced the model
  segment.SegmentationAlgorithmIdentificationSequence[SAFE_OFFSET(0)]
    .AlgorithmSource AS AlgorithmSource,

  # description:
  # CodeValue of AlgorithmFamilyCodeSequence (0066,002F), baseline CID 7162
  segment.SegmentationAlgorithmIdentificationSequence[SAFE_OFFSET(0)]
    .AlgorithmFamilyCodeSequence[SAFE_OFFSET(0)].CodeValue
    AS AlgorithmFamilyCodeValue,

  # description:
  # CodingSchemeDesignator of AlgorithmFamilyCodeSequence
  segment.SegmentationAlgorithmIdentificationSequence[SAFE_OFFSET(0)]
    .AlgorithmFamilyCodeSequence[SAFE_OFFSET(0)].CodingSchemeDesignator
    AS AlgorithmFamilyCodingSchemeDesignator,

  # description:
  # CodeMeaning of AlgorithmFamilyCodeSequence
  segment.SegmentationAlgorithmIdentificationSequence[SAFE_OFFSET(0)]
    .AlgorithmFamilyCodeSequence[SAFE_OFFSET(0)].CodeMeaning
    AS AlgorithmFamilyCodeMeaning,

  # description:
  # CodeValue of AlgorithmNameCodeSequence (0066,0030) - the manufacturer's
  # code for a SPECIFIC algorithm, i.e. the closest DICOM has to a model
  # identifier. Type 3 and rare; its absence is the usual finding.
  segment.SegmentationAlgorithmIdentificationSequence[SAFE_OFFSET(0)]
    .AlgorithmNameCodeSequence[SAFE_OFFSET(0)].CodeValue
    AS AlgorithmNameCodeValue,

  # description:
  # CodingSchemeDesignator of AlgorithmNameCodeSequence
  segment.SegmentationAlgorithmIdentificationSequence[SAFE_OFFSET(0)]
    .AlgorithmNameCodeSequence[SAFE_OFFSET(0)].CodingSchemeDesignator
    AS AlgorithmNameCodingSchemeDesignator,

  # description:
  # CodeMeaning of AlgorithmNameCodeSequence
  segment.SegmentationAlgorithmIdentificationSequence[SAFE_OFFSET(0)]
    .AlgorithmNameCodeSequence[SAFE_OFFSET(0)].CodeMeaning
    AS AlgorithmNameCodeMeaning,

  # description:
  # RecommendedDisplayCIELabValue (0062,000D) as "L/a/b", the three unsigned
  # shorts exactly as stored - scaled per PS3.3 C.10.7.1.1, NOT RGB. Rendering
  # it needs a white point the Standard only implies, so the conversion lives
  # with the check (scripts/cielab.py) rather than here, where it would look
  # like data. See 14_recommended_color.sql.
  # DELETE THIS COLUMN if the source schema lacks it - and report the absence:
  # it means no segment in the batch recommends a colour.
  ARRAY_TO_STRING(
    ARRAY(
      SELECT CAST(component AS STRING)
      FROM UNNEST(segment.RecommendedDisplayCIELabValue) AS component),
    '/') AS RecommendedDisplayCIELabValue,

  # description:
  # DICOM SegmentationType (0062,0001): BINARY, FRACTIONAL or LABELMAP
  segInstances.SegmentationType,

  # description:
  # DICOM SegmentsOverlap (0062,0013). Type 3, so often absent - see
  # 10_segments_overlap_absent.sql.
  segInstances.SegmentsOverlap,

  # description:
  # DICOM PhotometricInterpretation (0028,0004). Needed by
  # 14_recommended_color.sql: (0062,000D) shall NOT be present when a LABELMAP
  # object is PALETTE COLOR.
  segInstances.PhotometricInterpretation,

  # description:
  # DICOM TransferSyntaxUID (0002,0010). NULL here on purpose: it lives in the
  # file meta group, which a Healthcare API export does not carry, so issue 12
  # cannot be answered from BigQuery at all. The column is kept so the
  # per-segment table is the same shape on all three access paths; if your
  # source does carry the transfer syntax, substitute it here. Otherwise run
  # scripts/seg_encoding.py over the files and say in the report that
  # compression was not checked on this path.
  CAST(NULL AS STRING) AS TransferSyntaxUID,

  # description:
  # DICOM Manufacturer of the producing software
  segInstances.Manufacturer,

  # description:
  # DICOM ManufacturerModelName of the producing software. On a converted
  # segmentation this names the CONVERTER, not the segmentation tool.
  segInstances.ManufacturerModelName,

  # description:
  # First value of DICOM SoftwareVersions of the producing software
  segInstances.SoftwareVersions[SAFE_OFFSET(0)] AS SoftwareVersion,

  # description:
  # Number of segments in the segmentation object this segment belongs to
  ARRAY_LENGTH(segInstances.SegmentSequence) AS segmentsInInstance,

  # description:
  # DICOM NumberOfFrames (0028,0008) of the segmentation object
  SAFE_CAST(segInstances.NumberOfFrames AS INT64) AS numberOfFrames,

  # description:
  # SeriesInstanceUID of the image series the segment was drawn on, from
  # ReferencedSeriesSequence (0008,1115)
  segInstances.referencedSeries.SeriesInstanceUID
    AS referencedSeriesInstanceUID,

  # description:
  # Modality of the referenced image series.
  # The COALESCE fallback reads Modality out of ReferencedSeriesSequence, which
  # only some producers copy there. DROP THE COALESCE and keep
  # `imageSeries.Modality` alone if your source's ReferencedSeriesSequence
  # carries nothing but SeriesInstanceUID - a segmentation-only store is the
  # usual case.
  COALESCE(imageSeries.Modality, segInstances.referencedSeries.Modality)
    AS referencedModality,

  # description:
  # BodyPartExamined of the referenced image series. Same caveat as above.
  COALESCE(
    imageSeries.BodyPartExamined,
    segInstances.referencedSeries.BodyPartExamined)
    AS referencedBodyPartExamined,

  # description:
  # Number of instances of the referenced series listed in
  # ReferencedInstanceSequence. Compare with numberOfFrames: equal means the
  # segmentation covers the whole series.
  ARRAY_LENGTH(segInstances.referencedSeries.ReferencedInstanceSequence)
    AS referencedInstanceCount,

  # description:
  # Number of items in ReferencedSeriesSequence. A value above 1 means the
  # referenced series columns report only the first.
  segInstances.referencedSeriesCount,

  # description:
  # TRUE for the SegmentNumber 0 "Background" segment every labelmap object
  # carries. An artefact of the encoding, not a segmented finding - exclude it
  # from any count of what was segmented, and from the conformance check.
  segment.SegmentNumber = 0 AS isBackgroundSegment,

  # description:
  # TRUE when any code sequence of this segment holds more than one item, so
  # the single code reported above is only the first. Guards the SAFE_OFFSET(0)
  # assumption every other query makes.
  ARRAY_LENGTH(segment.AnatomicRegionSequence) > 1
    OR ARRAY_LENGTH(segment.SegmentedPropertyCategoryCodeSequence) > 1
    OR ARRAY_LENGTH(segment.SegmentedPropertyTypeCodeSequence) > 1
    OR ARRAY_LENGTH(
      segment.SegmentedPropertyTypeCodeSequence[SAFE_OFFSET(0)]
        .SegmentedPropertyTypeModifierCodeSequence) > 1
    OR ARRAY_LENGTH(
      segment.AnatomicRegionSequence[SAFE_OFFSET(0)]
        .AnatomicRegionModifierSequence) > 1
    AS multiValuedCodeSequence,

  # description:
  # URL opening the segmentation series in a viewer. Every finding needs a
  # clickable example; a UID a reader cannot open is not evidence.
  CONCAT(
    '@@VIEWER_BASE@@',
    segInstances.StudyInstanceUID,
    '?initialSeriesInstanceUID=',
    segInstances.SeriesInstanceUID) AS viewer_url

FROM
  segInstances
CROSS JOIN
  UNNEST(segInstances.SegmentSequence) AS segment
LEFT JOIN
  imageSeries
  ON imageSeries.SeriesInstanceUID
    = segInstances.referencedSeries.SeriesInstanceUID
