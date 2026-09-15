#standardSQL

# table-description:
# Issue 1 - severity High for WRONG_ANATOMY, Medium for the rest.
# Segments whose recorded AnatomicRegionSequence CodeMeaning disagrees with what
# the code actually denotes, so the code and the meaning cannot both be right.
#
# One row per affected segment. CURATED: the verdicts come from
# @@CODE_REVIEW@@ - see 04_code_review_template.sql for why it is curated rather
# than a join against a terminology table, and for how to re-derive it.
#
# `verdict` separates what a reader should act on:
#   WRONG_ANATOMY        the code names materially different anatomy. Errors.
#   NARROWER_OR_BROADER  right region, wrong granularity.
#   SPELLING             hyphenation or wording only.
#
# WHICH FIELD IS WRONG VARIES - usually the code, sometimes the CodeMeaning - so
# the report must say which, per case. A consumer that systematically trusted
# either field would get some segments wrong.
#
# Laterality problems are excluded here and reported by 06_laterality.sql.

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
  # The anatomic region CodeValue carried by the segment
  seg.AnatomicRegionCodeValue,
  # description:
  # What that code actually denotes
  review.codeActuallyMeans,
  # description:
  # The CodeMeaning the segment records, which contradicts it
  seg.AnatomicRegionCodeMeaning AS meaningRecorded,
  # description:
  # WRONG_ANATOMY, NARROWER_OR_BROADER or SPELLING - see the header
  review.verdict,
  # description:
  # Where the verdict came from
  review.reviewSource,
  # description:
  # URL opening the segmentation series in the viewer
  seg.viewer_url
FROM
  `@@SEG_ATTRIBUTES@@` AS seg
JOIN
  `@@CODE_REVIEW@@` AS review
  ON review.CodeValue = seg.AnatomicRegionCodeValue
  AND review.CodingSchemeDesignator = seg.AnatomicRegionCodingSchemeDesignator
  AND review.meaningRecorded = seg.AnatomicRegionCodeMeaning
WHERE
  review.issue = 1
  AND NOT seg.isBackgroundSegment
ORDER BY
  CASE review.verdict
    WHEN 'WRONG_ANATOMY' THEN 0 WHEN 'NARROWER_OR_BROADER' THEN 1 ELSE 2 END,
  seg.AnatomicRegionCodeValue,
  seg.PatientID
