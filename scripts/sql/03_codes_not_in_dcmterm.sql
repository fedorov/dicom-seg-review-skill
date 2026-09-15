#standardSQL

# table-description:
# The terminology coverage gap: anatomic region codes the DICOM-derived code
# table does not carry, with how much of the batch depends on each.
#
# One row per (CodingSchemeDesignator, CodeValue). Fully computed.
#
# THIS IS A DELIVERABLE, not a diagnostic. A DICOM-derived table holds codes
# used by DICOM's context groups, not all of SNOMED: in one batch it covered
# 57 of 134 anatomic codes - 880 of 1,961 segments. A check that joins it and
# treats non-matches as clean therefore passes more than half the batch without
# looking, and the single worst error in that batch was in the gap.
#
# So: publish this list with the report, say the clean bill does not extend to
# it, and check every code here against a terminology server -
# tx.fhir.org/r4/CodeSystem/$lookup?system=http://snomed.info/sct&code=<CODE>
# which is what scripts/lookup_codes.py automates.
#
# @@DCMTERM_TABLE@@ is a table of (coding_scheme_designator, code_value) pairs
# extracted from the DICOM standard's context groups - e.g.
# idc-sandbox-000.dcmterm.codes_unique. Skip this query if you have none; run
# lookup_codes.py over every code instead.

SELECT
  # description:
  # CodingSchemeDesignator of the code, e.g. "SCT"
  seg.AnatomicRegionCodingSchemeDesignator,
  # description:
  # CodeValue the DICOM-derived table does not carry
  seg.AnatomicRegionCodeValue,
  # description:
  # Distinct CodeMeanings the batch records for it, pipe-separated. More than
  # one means the batch itself disagrees about what the code means.
  STRING_AGG(DISTINCT seg.AnatomicRegionCodeMeaning, ' | '
             ORDER BY seg.AnatomicRegionCodeMeaning) AS meaningsInBatch,
  # description:
  # Number of distinct CodeMeanings recorded for it
  COUNT(DISTINCT seg.AnatomicRegionCodeMeaning) AS distinctMeanings,
  # description:
  # Number of segments carrying this code
  COUNT(*) AS segments,
  # description:
  # Number of series carrying this code
  COUNT(DISTINCT seg.SeriesInstanceUID) AS series
FROM
  `@@SEG_ATTRIBUTES@@` AS seg
LEFT JOIN (
  SELECT DISTINCT coding_scheme_designator AS scheme, code_value AS code
  FROM `@@DCMTERM_TABLE@@`) AS lut
  ON lut.code = seg.AnatomicRegionCodeValue
  AND lut.scheme = seg.AnatomicRegionCodingSchemeDesignator
WHERE
  seg.AnatomicRegionCodeValue IS NOT NULL
  AND NOT seg.isBackgroundSegment
  AND lut.code IS NULL
GROUP BY
  seg.AnatomicRegionCodingSchemeDesignator,
  seg.AnatomicRegionCodeValue
ORDER BY
  segments DESC, seg.AnatomicRegionCodeValue
