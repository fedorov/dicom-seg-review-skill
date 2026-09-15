#standardSQL

# table-description:
# The curated review: one row per (CodeValue, CodeMeaning) pairing a human has
# judged wrong, with what the code actually denotes. Deploy as a view:
#
#   bq mk --project_id=<project> --use_legacy_sql=false \
#     --view "$(cat 04_code_review_template.sql)" <dataset>.seg_code_review
#
# THIS IS THE ONLY PLACE VERDICTS LIVE. 05_anatomy_conflict.sql,
# 06_laterality.sql and 12_series_triage.sql all join it, so editing a verdict
# means editing this file and redeploying, not three files. Inline code lists in
# each query were tried first and drifted immediately.
#
# Keyed on the PAIRING, not the code: the same code can be correct under one
# CodeMeaning and wrong under another, which is exactly what 02 surfaces.
#
# THIS WILL NOT FOLLOW A NEW DELIVERY. It encodes judgements about the codes one
# batch happened to use. Re-derive it by running 02_ambiguous_code.sql and
# 03_codes_not_in_dcmterm.sql against the new data, resolving every code with
# scripts/lookup_codes.py, and re-checking what they surface. Until that is
# done the verdicts are stale and the report must say so.
#
# `verdict` values:
#   WRONG_ANATOMY        the code names materially different anatomy. An error.
#   NARROWER_OR_BROADER  right region, wrong granularity.
#   SPELLING             hyphenation or wording only.
#   INVERTED             the code names the opposite side (issue 3).
#   UNCODED              the code carries no side, but the meaning states one.
#
# `reviewSource` records where the verdict came from - a DICOM-derived table, a
# terminology server, or a human. A reader needs to know which findings a
# machine can reproduce.
#
# `issue` is 1 for anatomy verdicts and 3 for laterality verdicts, matching the
# section numbering of the report.

SELECT * FROM UNNEST([
    # Replace these two examples with the batch's own verdicts. They are real
    # findings from the batch this skill was built from, kept only to show the
    # shape - DELETE THEM before deploying against your data.
    STRUCT(
      1 AS issue,
      'SCT' AS CodingSchemeDesignator,
      '10200004' AS CodeValue,
      'liver' AS codeActuallyMeans,
      'Large bowel' AS meaningRecorded,
      'WRONG_ANATOMY' AS verdict,
      'dcmterm' AS reviewSource),
    (3, 'SCT', '110634007', 'right uterine adnexa', 'Left adnexa',
     'INVERTED', 'tx.fhir.org')])
