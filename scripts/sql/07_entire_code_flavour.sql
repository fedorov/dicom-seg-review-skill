#standardSQL

# table-description:
# Issue 8 - severity Medium. Segments coded with the SNOMED "Entire X" flavour
# of an anatomic concept rather than the "X structure" flavour DICOM uses.
#
# One row per affected segment.
#
# SNOMED carries two body-structure concepts for most anatomy: 302508007
# "Entire colon (body structure)" denotes the whole organ EXCLUSIVELY, while
# 71854001 "Colon structure (body structure)" denotes the organ OR ANY PART of
# it. DICOM's context groups draw on the structure flavour, so a segmentation of
# part of an organ coded "Entire ..." asserts more than was segmented.
#
# EVIDENCE, since PS3.16 section 6 does not state the rule: every "Entire" code
# in the source batch was ABSENT from the DICOM-derived code table, while the
# structure counterpart was PRESENT for each one that had a findable
# equivalent. Argue it that way, from DICOM's own code set, rather than
# asserting a preference.
#
# Detection is mechanical once you have fully specified names: flag every code
# whose FSN begins "Entire ". scripts/lookup_codes.py does that and writes
# isEntireFlavour; fill the list below from its output.
#
# SPLIT THE FINDING. Codes with a drop-in structure equivalent are a
# substitution; codes without one need a decision and may also be at the wrong
# granularity. In the source batch that was 111 segments versus 31.
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
  )

SELECT
  # description:
  # DICOM PatientID
  seg.PatientID,
  # description:
  # DICOM StudyInstanceUID of the study containing the segmentation
  seg.StudyInstanceUID,
  # description:
  # DICOM SeriesInstanceUID of the segmentation series
  seg.SeriesInstanceUID,
  # description:
  # DICOM SegmentNumber within its segmentation object
  seg.SegmentNumber,
  # description:
  # DICOM SegmentLabel of the affected segment
  seg.SegmentLabel,
  # description:
  # The "Entire" flavour CodeValue the segment carries
  seg.AnatomicRegionCodeValue,
  # description:
  # Its SNOMED fully specified name, which is what marks it as that flavour
  entireFlavourCodes.entireFsn,
  # description:
  # The CodeMeaning the segment records
  seg.AnatomicRegionCodeMeaning AS meaningRecorded,
  # description:
  # The "structure" flavour code DICOM uses instead, where one exists. NULL
  # means the coding needs a decision rather than a substitution.
  entireFlavourCodes.suggestedReplacement,
  # description:
  # CodeMeaning of the suggested replacement
  entireFlavourCodes.replacementMeaning,
  # description:
  # URL opening the segmentation series in the viewer
  seg.viewer_url
FROM
  `@@SEG_ATTRIBUTES@@` AS seg
JOIN
  entireFlavourCodes
  ON entireFlavourCodes.code = seg.AnatomicRegionCodeValue
WHERE
  NOT seg.isBackgroundSegment
ORDER BY
  entireFlavourCodes.suggestedReplacement IS NULL DESC,
  seg.AnatomicRegionCodeValue,
  seg.PatientID
