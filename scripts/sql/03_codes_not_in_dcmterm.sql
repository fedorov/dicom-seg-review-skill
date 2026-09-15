#standardSQL

# table-description:
# The terminology coverage gap: anatomic region codes the DICOM-derived code
# table does not carry, with how much of the batch depends on each.
#
# One row per (CodingSchemeDesignator, CodeValue). Fully computed.
#
# THIS IS A DELIVERABLE, not a diagnostic. A DICOM-derived table holds codes
# used by DICOM's context groups, not all of SNOMED, and the share of a batch it
# reaches can be well under half. A check that joins it and treats non-matches
# as clean therefore passes everything in the gap without looking - and the most
# serious error in a batch can be sitting in exactly that gap.
#
# So: publish this list with the report, say the clean bill does not extend to
# it, and check every code here against a terminology server -
# tx.fhir.org/r4/CodeSystem/$lookup?system=http://snomed.info/sct&code=<CODE>
# which is what scripts/lookup_codes.py automates.
#
# @@DCMTERM_TABLE@@ is the code set extracted from the DICOM standard's context
# groups. Load it from the public Parquet published by fedorov/dcmterms - no
# private table needed:
#
#   curl -LO https://raw.githubusercontent.com/fedorov/dcmterms/main/docs/data/codes_unique.parquet
#   bq load --project_id=<project> --source_format=PARQUET \
#     <dataset>.dcmterm_codes_unique codes_unique.parquet
#
# NOTE it is deduplicated on the MEANING as well as the code - 21974007 is in it
# as both "Tongue" and "tongue" - so the join below takes DISTINCT pairs. Joining
# it raw multiplies rows.
#
# scripts/dcmterm.py runs this same check with no BigQuery at all, reads the
# Parquet directly, and additionally reports codes that ARE in the set but carry
# a meaning DICOM does not use for them. Prefer it unless the batch is only
# reachable through BigQuery.

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
