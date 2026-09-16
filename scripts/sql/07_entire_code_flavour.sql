#standardSQL

# table-description:
# Issue 8 - severity Medium. Segments coded with the SNOMED "Entire X" flavour
# of an anatomic concept rather than the "X structure" flavour DICOM uses - in
# the anatomic region, the segmented property type or the segmented property
# category.
#
# One row per affected (segment, sequence).
#
# SNOMED carries two body-structure concepts for most anatomy: 302508007
# "Entire colon (body structure)" denotes the complete organ only, while
# 71854001 "Colon structure (body structure)" is the parent concept, denoting
# the organ OR ANY PART of it. DICOM's context groups draw on the structure
# flavour, so a segmentation of part of an organ coded "Entire ..." asserts more
# than was segmented.
#
# PS3.16 section 8.1.1 "Use of SNOMED Anatomic Concepts" STATES the rule: "In
# general, DICOM uses the anatomic concepts with the term 'structure', rather
# than with the term 'entire'... Since imaging typically targets both the
# anatomic feature and the area around it, or sometimes just part of the
# anatomic feature, DICOM usually uses 'structure' concepts that are more
# inclusive than the 'entire' concepts." Cite it.
#
# It says "in general" and "usually", not "shall", which is why this is a Medium
# and not a conformance violation - and it is not applied uniformly, since
# CID 4031 includes 38266002 "Entire body". So ALSO BUILD THE EVIDENCE PER
# BATCH: show that each "Entire" code the batch uses is ABSENT from the
# DICOM-derived code table while its structure counterpart is PRESENT. The
# quotation says what DICOM intends; the table says what DICOM did.
# scripts/dcmterm.py suggest reports both halves against the public dcmterms
# Parquet, and names the DICOM edition.
#
# Note that DICOM's Code Meanings never carry the "structure"/"entire" suffix -
# CID 4031 lists 71854001 as plain "Colon" - so only the code value tells you
# which flavour is in play. That is why this check needs the FSN lookup.
#
# Detection is mechanical once you have fully specified names: flag every code
# whose FSN begins "Entire ". scripts/lookup_codes.py does that and writes
# isEntireFlavour; scripts/dcmterm.py suggest then turns that file into this
# list, with column names matching the struct below.
#
# SPLIT THE FINDING. Codes with a drop-in structure equivalent are a
# substitution; codes without one - typically the more specific lymph-node and
# region concepts, for which SNOMED has no "structure" sibling - need a decision
# and may also be at the wrong granularity.
#
# Watch for codes with two problems at once: 181616008 was both the entire
# flavour AND used for a meaning that contradicts it under issue 1.

WITH
  # From scripts/lookup_codes.py: every code whose FSN begins "Entire ".
  # suggestedReplacement is the structure-flavour code where one exists; NULL
  # means no clean equivalent was found and the coding needs a decision.
  entireFlavourCodes AS (
    SELECT * FROM UNNEST([
      STRUCT('302508007' AS code,
             'Entire colon' AS entireFsn,
             '71854001' AS suggestedReplacement,
             'Colon' AS replacementMeaning),
      ('181279003', 'Entire spleen', '78961009', 'Spleen'),
      ('181757009', 'Entire cervical lymph node', NULL, NULL)])
  ),

  # One row per (segment, code sequence) - identical to the CTE in 02.
  codes AS (
    SELECT seg.*, 'AnatomicRegion' AS codeSequence,
      AnatomicRegionCodingSchemeDesignator AS CodingSchemeDesignator,
      AnatomicRegionCodeValue AS CodeValue,
      AnatomicRegionCodeMeaning AS CodeMeaning
    FROM `@@SEG_ATTRIBUTES@@` AS seg
    WHERE AnatomicRegionCodeValue IS NOT NULL AND NOT isBackgroundSegment
    UNION ALL
    SELECT seg.*, 'SegmentedPropertyType',
      SegmentedPropertyTypeCodingSchemeDesignator,
      SegmentedPropertyTypeCodeValue,
      SegmentedPropertyTypeCodeMeaning
    FROM `@@SEG_ATTRIBUTES@@` AS seg
    WHERE SegmentedPropertyTypeCodeValue IS NOT NULL AND NOT isBackgroundSegment
    UNION ALL
    SELECT seg.*, 'SegmentedPropertyCategory',
      SegmentedPropertyCategoryCodingSchemeDesignator,
      SegmentedPropertyCategoryCodeValue,
      SegmentedPropertyCategoryCodeMeaning
    FROM `@@SEG_ATTRIBUTES@@` AS seg
    WHERE SegmentedPropertyCategoryCodeValue IS NOT NULL AND NOT isBackgroundSegment
  )

SELECT
  # description:
  # DICOM PatientID
  codes.PatientID,
  # description:
  # DICOM StudyInstanceUID of the study containing the segmentation
  codes.StudyInstanceUID,
  # description:
  # DICOM SeriesInstanceUID of the segmentation series
  codes.SeriesInstanceUID,
  # description:
  # DICOM SegmentNumber within its segmentation object
  codes.SegmentNumber,
  # description:
  # DICOM SegmentLabel of the affected segment
  codes.SegmentLabel,
  # description:
  # Which code sequence carries the "Entire" flavour code
  codes.codeSequence,
  # description:
  # CodingSchemeDesignator of the code
  codes.CodingSchemeDesignator,
  # description:
  # The "Entire" flavour CodeValue the segment carries
  codes.CodeValue,
  # description:
  # Its SNOMED fully specified name, which is what marks it as that flavour
  entireFlavourCodes.entireFsn,
  # description:
  # The CodeMeaning the segment records
  codes.CodeMeaning AS meaningRecorded,
  # description:
  # The "structure" flavour code DICOM uses instead, where one exists. NULL
  # means the coding needs a decision rather than a substitution.
  entireFlavourCodes.suggestedReplacement,
  # description:
  # CodeMeaning of the suggested replacement
  entireFlavourCodes.replacementMeaning,
  # description:
  # URL opening the segmentation series in the viewer
  codes.viewer_url
FROM
  codes
JOIN
  entireFlavourCodes
  ON entireFlavourCodes.code = codes.CodeValue
ORDER BY
  entireFlavourCodes.suggestedReplacement IS NULL DESC,
  codes.CodeValue,
  codes.codeSequence,
  codes.PatientID
