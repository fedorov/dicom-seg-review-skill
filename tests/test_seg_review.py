#!/usr/bin/env python3
"""Tests for the extraction and check logic.

Synthetic datasets rather than fixture files: the branches that matter here are
edge cases a real delivery rarely contains all at once - a Background segment, a
segment missing Type 1 attributes, a code carrying two meanings, a TrackingUID
spanning two patients.

  python -m pytest tests/ -q        (or: python tests/test_seg_review.py)

Needs pydicom, for Dataset only - nothing is read from disk. The dciodvfy tests
run against captured output rather than the binary, so the suite still needs
neither network nor dicom3tools; the captures are real dciodvfy -new output,
copied verbatim.
"""

import sys
import unittest
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from pydicom.dataset import Dataset  # noqa: E402

import dciodvfy_check  # noqa: E402
import dcmterm  # noqa: E402
import seg_attributes  # noqa: E402
import seg_checks  # noqa: E402


def code(value, meaning, scheme="SCT"):
    item = Dataset()
    item.CodeValue = value
    item.CodingSchemeDesignator = scheme
    item.CodeMeaning = meaning
    return item


def segment(number, label, region=None, category="49755003", seg_type="52988006",
            tracking_uid=None, algorithm_type="AUTOMATIC", omit=()):
    seg = Dataset()
    if "SegmentNumber" not in omit:
        seg.SegmentNumber = number
    if "SegmentLabel" not in omit:
        seg.SegmentLabel = label
    if region and "AnatomicRegionSequence" not in omit:
        seg.AnatomicRegionSequence = [code(*region)]
    if category and "SegmentedPropertyCategoryCodeSequence" not in omit:
        seg.SegmentedPropertyCategoryCodeSequence = [code(category, "Category")]
    if seg_type and "SegmentedPropertyTypeCodeSequence" not in omit:
        seg.SegmentedPropertyTypeCodeSequence = [code(seg_type, "Type")]
    if "SegmentAlgorithmType" not in omit:
        seg.SegmentAlgorithmType = algorithm_type
    if tracking_uid:
        seg.TrackingUID = tracking_uid
        seg.TrackingID = label
    return seg


def instance(segments, sop="1.1", series="2.1", study="3.1", patient="P1",
             overlap=None, description="LESION SEGMENTATIONS"):
    ds = Dataset()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.66.4"
    ds.SOPInstanceUID = sop
    ds.SeriesInstanceUID = series
    ds.StudyInstanceUID = study
    ds.PatientID = patient
    ds.SeriesDescription = description
    if overlap:
        ds.SegmentsOverlap = overlap
    ds.SegmentSequence = segments
    return ds


def rows_for(datasets, viewer=None):
    rows = []
    for ds in datasets:
        rows.extend(seg_attributes.extract_instance(ds, viewer))
    return rows


class TestExtraction(unittest.TestCase):
    def test_one_row_per_segment(self):
        rows = rows_for([instance([segment(1, "A"), segment(2, "B")])])
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["SegmentLabel"] for r in rows], ["A", "B"])
        self.assertEqual(rows[0]["segmentsInInstance"], "2")

    def test_codes_are_flattened(self):
        rows = rows_for([instance([segment(1, "A", region=("10200004", "Liver"))])])
        self.assertEqual(rows[0]["AnatomicRegionCodeValue"], "10200004")
        self.assertEqual(rows[0]["AnatomicRegionCodingSchemeDesignator"], "SCT")
        self.assertEqual(rows[0]["AnatomicRegionCodeMeaning"], "Liver")

    def test_background_segment_is_flagged(self):
        """Labelmap objects carry a SegmentNumber 0 Background segment. It is an
        artefact of the encoding, and must not be counted as a finding."""
        rows = rows_for([instance([segment(0, "Background"), segment(1, "Liver")])])
        self.assertEqual(rows[0]["isBackgroundSegment"], "True")
        self.assertEqual(rows[1]["isBackgroundSegment"], "False")

    def test_multi_valued_code_sequence_is_detected(self):
        """The guard on the OFFSET(0) assumption every check makes."""
        seg = segment(1, "A", region=("10200004", "Liver"))
        seg.AnatomicRegionSequence.append(code("78961009", "Spleen"))
        rows = rows_for([instance([seg])])
        self.assertEqual(rows[0]["multiValuedCodeSequence"], "True")
        self.assertEqual(rows[0]["AnatomicRegionCodeValue"], "10200004")

    def test_absent_sequence_is_not_an_error(self):
        rows = rows_for([instance([segment(1, "A", region=None)])])
        self.assertEqual(rows[0]["AnatomicRegionCodeValue"], "")

    def test_instance_with_no_segments_still_yields_a_row(self):
        """Otherwise a malformed object vanishes from the review entirely."""
        ds = instance([])
        ds.SegmentSequence = []
        rows = rows_for([ds])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["SegmentNumber"], "")
        self.assertEqual(rows[0]["SOPInstanceUID"], "1.1")

    def test_laterality_modifier_is_extracted(self):
        """Post-coordinated laterality can live in either modifier sequence."""
        seg = segment(1, "Kidney", region=("64033007", "Kidney"))
        seg.AnatomicRegionSequence[0].AnatomicRegionModifierSequence = [
            code("7771000", "Left")
        ]
        seg.SegmentedPropertyTypeCodeSequence[0].\
            SegmentedPropertyTypeModifierCodeSequence = [code("24028007", "Right")]
        rows = rows_for([instance([seg])])
        self.assertEqual(rows[0]["AnatomicRegionModifierCodeMeaning"], "Left")
        self.assertEqual(rows[0]["SegmentedPropertyTypeModifierCodeMeaning"], "Right")

    def test_viewer_url_is_built(self):
        rows = rows_for(
            [instance([segment(1, "A")])],
            viewer="https://v/{StudyInstanceUID}?s={SeriesInstanceUID}",
        )
        self.assertEqual(rows[0]["viewer_url"], "https://v/3.1?s=2.1")


class TestAmbiguousCodes(unittest.TestCase):
    """Issue 2 - the highest-yield check, and the one whose `scope` column decides
    how big the work queue turns out to be."""

    def test_self_inconsistent_beats_minority(self):
        rows = rows_for([
            instance([segment(1, "A", region=("91394001", "Peritoneal lymph node")),
                      segment(2, "B", region=("91394001", "Hepatic lymph node"))],
                     series="S1"),
            instance([segment(1, "C", region=("91394001", "Peritoneal lymph node"))],
                     sop="1.2", series="S2"),
        ])
        findings, ambiguous, _ = seg_checks.ambiguous_codes(rows)
        self.assertIn("91394001", ambiguous)
        scopes = {(f["SeriesInstanceUID"], f["SegmentLabel"]): f["scope"]
                  for f in findings}
        self.assertEqual(scopes[("S1", "A")], "SELF_INCONSISTENT")
        self.assertEqual(scopes[("S1", "B")], "SELF_INCONSISTENT")
        # S2 uses the dominant meaning and is implicated only by S1 - it must not
        # be treated as evidence, or most of a batch lands in the work queue.
        self.assertEqual(scopes[("S2", "C")], "DOMINANT_MEANING")

    def test_minority_meaning(self):
        rows = rows_for([
            instance([segment(1, "A", region=("12003004", "Left adrenal gland"))],
                     series="S1"),
            instance([segment(1, "B", region=("12003004", "Left adrenal gland"))],
                     sop="1.2", series="S2"),
            instance([segment(1, "C", region=("12003004", "Left adrenal"))],
                     sop="1.3", series="S3"),
        ])
        findings, _, cosmetic = seg_checks.ambiguous_codes(rows)
        scopes = {f["SeriesInstanceUID"]: f["scope"] for f in findings}
        self.assertEqual(scopes["S3"], "MINORITY_MEANING")
        self.assertEqual(scopes["S1"], "DOMINANT_MEANING")
        # Near-identical meanings: the same anatomy written two ways.
        self.assertIn("12003004", cosmetic)

    def test_unambiguous_code_is_not_reported(self):
        rows = rows_for([instance([segment(1, "A", region=("10200004", "Liver")),
                                   segment(2, "B", region=("10200004", "Liver"))])])
        findings, ambiguous, _ = seg_checks.ambiguous_codes(rows)
        self.assertEqual(findings, [])
        self.assertEqual(ambiguous, {})


class TestCosmeticHeuristic(unittest.TestCase):
    """Separating cosmetic variants from genuine granularity differences. Getting
    this wrong in either direction matters: a missed variant inflates the work
    queue, and a NARROWER_OR_BROADER wrongly called cosmetic is a Medium finding
    filed as Low."""

    SAME = [
        ("Left adrenal gland", "Left adrenal"),
        ("Right adrenal gland", "Right adrenal"),
        ("peritoneal deposit", "Peritoneal deposit"),
        ("Right axilary lymph node", "Right axillary lymph node"),
        ("Cardiophrenic angle lymph node", "Cardiophrenic lymph node"),
    ]
    DIFFERENT = [
        ("Lung", "Nodule of lung"),
        ("Sternum", "body of sternum"),
        ("Left thyroid gland", "left lobe of thyroid"),
        ("Peritoneal lymph node", "Peritoneal nodule"),
        ("Neck lymph node", "lymph node of head and neck"),
        ("Large bowel", "liver"),
    ]

    def test_cosmetic_variants_are_recognised(self):
        for a, b in self.SAME:
            with self.subTest(a=a, b=b):
                self.assertTrue(seg_checks._same_anatomy_written_twice(a, b))

    def test_granularity_differences_are_not_cosmetic(self):
        for a, b in self.DIFFERENT:
            with self.subTest(a=a, b=b):
                self.assertFalse(seg_checks._same_anatomy_written_twice(a, b))


class TestConformance(unittest.TestCase):
    """Issue 4 - Type 1 of the Segment Description Macro, PS3.3 Table C.8.20-4."""

    def test_missing_type1_attributes_are_named(self):
        rows = rows_for([instance([
            segment(1, "A", omit=("SegmentLabel", "SegmentAlgorithmType"))])])
        findings = seg_checks.malformed(rows)
        self.assertEqual(len(findings), 1)
        self.assertIn("SegmentLabel", findings[0]["missingType1Attributes"])
        self.assertIn("SegmentAlgorithmType", findings[0]["missingType1Attributes"])

    def test_code_without_scheme_designator_is_reported(self):
        """A code value alone does not identify a concept."""
        seg = segment(1, "A")
        del seg.SegmentedPropertyTypeCodeSequence[0].CodingSchemeDesignator
        findings = seg_checks.malformed(rows_for([instance([seg])]))
        self.assertEqual(len(findings), 1)
        self.assertIn(
            "SegmentedPropertyTypeCodeSequence.CodingSchemeDesignator",
            findings[0]["missingType1Attributes"],
        )

    def test_absent_optional_attributes_are_not_reported(self):
        """AnatomicRegionSequence is optional; its absence is conformant."""
        findings = seg_checks.malformed(
            rows_for([instance([segment(1, "A", region=None)])]))
        self.assertEqual(findings, [])

    def test_conformant_segment_is_clean(self):
        findings = seg_checks.malformed(
            rows_for([instance([segment(1, "A", region=("10200004", "Liver"))])]))
        self.assertEqual(findings, [])


class TestTrackingUid(unittest.TestCase):
    """Issue 7 - a shared UID means several different things."""

    def _pattern(self, datasets):
        findings = seg_checks.tracking_uid(rows_for(datasets))
        return {f["TrackingUID"]: f["sharingPattern"] for f in findings}

    def test_longitudinal_is_the_intended_use(self):
        patterns = self._pattern([
            instance([segment(1, "A", tracking_uid="U1")], study="ST1", series="S1"),
            instance([segment(1, "A", tracking_uid="U1")], sop="1.2", study="ST2",
                     series="S2"),
        ])
        self.assertEqual(patterns["U1"], "LONGITUDINAL")

    def test_within_study_is_the_unclear_case(self):
        patterns = self._pattern([
            instance([segment(1, "A", tracking_uid="U1")], series="S1"),
            instance([segment(1, "A", tracking_uid="U1")], sop="1.2", series="S2"),
        ])
        self.assertEqual(patterns["U1"], "WITHIN_STUDY")

    def test_seed_and_lesion_is_separated(self):
        patterns = self._pattern([
            instance([segment(1, "A", tracking_uid="U1")], series="S1",
                     description="SEED POINTS"),
            instance([segment(1, "A", tracking_uid="U1")], sop="1.2", series="S2"),
        ])
        self.assertEqual(patterns["U1"], "SEED_AND_LESION")

    def test_cross_patient_outranks_everything(self):
        """Normally absent, and serious if it appears."""
        patterns = self._pattern([
            instance([segment(1, "A", tracking_uid="U1")], patient="P1", study="ST1"),
            instance([segment(1, "A", tracking_uid="U1")], sop="1.2", patient="P2",
                     study="ST2", series="S2"),
        ])
        self.assertEqual(patterns["U1"], "CROSS_PATIENT")

    def test_unshared_uid_is_not_reported(self):
        patterns = self._pattern([
            instance([segment(1, "A", tracking_uid="U1"),
                      segment(2, "B", tracking_uid="U2")])])
        self.assertEqual(patterns, {})


class TestTriage(unittest.TestCase):
    def test_curated_verdict_raises_severity_and_work_queue(self):
        rows = rows_for([instance([segment(1, "A", region=("10200004", "Large bowel"))])])
        verdicts = {("10200004", "SCT", "Large bowel"): "WRONG_ANATOMY"}
        out = seg_checks.triage(rows, {}, set(), {}, verdicts, [])
        self.assertEqual(out[0]["worstSeverity"], "High")
        self.assertIn("ANATOMY_CONFLICT", out[0]["issues"])
        self.assertEqual(out[0]["segmentsNeedingRecode"], 1)
        self.assertIn('10200004 "Large bowel"', out[0]["offendingCodes"])

    def test_ambiguous_elsewhere_does_not_raise_severity(self):
        """A series using only dominant meanings is implicated by other series,
        not by anything it did. It must stay out of the work queue."""
        rows = rows_for([
            instance([segment(1, "A", region=("91394001", "Node"))], series="S1"),
            instance([segment(1, "B", region=("91394001", "Node"))], sop="1.2",
                     series="S2"),
            instance([segment(1, "C", region=("91394001", "Other"))], sop="1.3",
                     series="S3"),
        ])
        _, ambiguous, cosmetic = seg_checks.ambiguous_codes(rows)
        out = {r["SeriesInstanceUID"]: r
               for r in seg_checks.triage(rows, ambiguous, cosmetic, {}, {}, [])}
        self.assertIn("CODE_AMBIGUOUS_ELSEWHERE", out["S1"]["issues"])
        self.assertNotEqual(out["S1"]["worstSeverity"], "High")
        self.assertEqual(out["S1"]["segmentsNeedingRecode"], 0)
        # ...while the minority reading is evidence, and does raise it.
        self.assertEqual(out["S3"]["worstSeverity"], "High")

    def test_clean_series_reports_none(self):
        rows = rows_for([instance([segment(1, "A", region=("10200004", "Liver"))],
                                  overlap="NO")])
        out = seg_checks.triage(rows, {}, set(), {}, {}, [])
        self.assertEqual(out[0]["worstSeverity"], "None")
        self.assertEqual(out[0]["issues"], "")

    def test_worst_first_ordering(self):
        rows = rows_for([
            instance([segment(1, "A", region=("10200004", "Large bowel"))],
                     series="BAD"),
            instance([segment(1, "B", region=("78961009", "Spleen"))], sop="1.2",
                     series="OK", overlap="NO"),
        ])
        verdicts = {("10200004", "SCT", "Large bowel"): "WRONG_ANATOMY"}
        out = seg_checks.triage(rows, {}, set(), {}, verdicts, [])
        self.assertEqual(out[0]["SeriesInstanceUID"], "BAD")


class TestOtherChecks(unittest.TestCase):
    def test_type_repeating_category_is_reported(self):
        rows = rows_for([instance([
            segment(1, "A", category="49755003", seg_type="49755003")])])
        self.assertEqual(len(seg_checks.type_repeats_category(rows)), 1)

    def test_distinct_type_and_category_are_clean(self):
        rows = rows_for([instance([
            segment(1, "A", category="49755003", seg_type="52988006")])])
        self.assertEqual(seg_checks.type_repeats_category(rows), [])

    def test_segments_overlap_is_object_level(self):
        """One row per SOP instance, not per segment, and the segment count is
        what says whether the absence costs anything."""
        rows = rows_for([instance([segment(1, "A"), segment(2, "B")])])
        findings = seg_checks.segments_overlap_absent(rows)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["segmentsInObject"], 2)

    def test_present_segments_overlap_is_not_reported(self):
        rows = rows_for([instance([segment(1, "A")], overlap="NO")])
        self.assertEqual(seg_checks.segments_overlap_absent(rows), [])


def reference(*entries):
    """A stand-in for codes_unique, so the tests never touch the network.

    Same shape dcmterm.dcm_codes builds: one entry per code, every meaning DICOM
    spells it with, and the number of context groups using it.
    """
    table = {}
    for scheme, value, meanings, num_cids in entries:
        table[(scheme, value)] = {
            "meanings": list(meanings),
            "normalised": {dcmterm.normalise(m) for m in meanings},
            "numCids": num_cids,
        }
    return table


DCM = reference(
    ("SCT", "10200004", ["Liver"], 9),
    ("SCT", "21974007", ["Tongue", "tongue"], 4),
    ("SCT", "71854001", ["Colon"], 4),
    ("SCT", "78961009", ["Spleen"], 6),
    ("SCT", "60184004", ["Sigmoid colon"], 2),
)


def batch(*entries):
    return {(scheme, value): {"meanings": set(meanings), "segments": segments,
                              "series": {"s1"}}
            for scheme, value, meanings, segments in entries}


class TestDcmtermCoverage(unittest.TestCase):
    """The computed half of the terminology check: what DICOM itself uses."""

    def row_for(self, *entries):
        return {r["CodeValue"]: r for r in dcmterm.coverage(batch(*entries), DCM)}

    def test_meaning_dicom_does_not_use_is_flagged(self):
        """The finding this exists for: 10200004 is DICOM's liver code, and the
        batch calls it large bowel."""
        row = self.row_for(("SCT", "10200004", ["Large bowel"], 3))["10200004"]
        self.assertEqual(row["inDcmterm"], "True")
        self.assertEqual(row["meaningAgrees"], "False")
        self.assertEqual(row["dcmMeanings"], "Liver")
        self.assertEqual(row["numCids"], 9)

    def test_matching_meaning_agrees_regardless_of_case(self):
        row = self.row_for(("SCT", "21974007", ["tongue"], 1))["21974007"]
        self.assertEqual(row["meaningAgrees"], "True")

    def test_one_of_two_meanings_matching_is_partial(self):
        """A code carrying two meanings, only one of which DICOM uses, is neither
        agreement nor disagreement - reporting it as either loses the finding."""
        row = self.row_for(("SCT", "71854001", ["Colon", "Sigmoid"], 2))["71854001"]
        self.assertEqual(row["meaningAgrees"], "PARTIAL")

    def test_hyphenation_is_not_normalised_away(self):
        """"Paraaortic" against "para-aortic" is the SPELLING verdict, a finding."""
        self.assertNotEqual(dcmterm.normalise("para-aortic"),
                            dcmterm.normalise("paraaortic"))

    def test_uncovered_code_reports_no_verdict(self):
        """Absence from DICOM's code set is not a clean bill and not a fault:
        the meaning columns stay empty so nothing downstream reads agreement."""
        row = self.row_for(("SCT", "110634007", ["Left adnexa"], 1))["110634007"]
        self.assertEqual(row["inDcmterm"], "False")
        self.assertEqual(row["meaningAgrees"], "")
        self.assertEqual(row["dcmMeanings"], "")

    def test_private_scheme_is_separated_from_the_gap(self):
        """A 99 designator is an expected miss, not an unreviewed code."""
        row = self.row_for(("99LOCAL", "WIDGET", ["Widget"], 5))["WIDGET"]
        self.assertEqual(row["isPrivateScheme"], "True")
        self.assertEqual(row["inDcmterm"], "False")

    def test_rows_are_ordered_by_segments_affected(self):
        rows = dcmterm.coverage(batch(("SCT", "78961009", ["Spleen"], 2),
                                      ("SCT", "10200004", ["Liver"], 40)), DCM)
        self.assertEqual(rows[0]["CodeValue"], "10200004")

    def test_background_segments_are_excluded(self):
        table = Path(__file__).resolve().parent / "_batch.csv"
        table.write_text(
            "SeriesInstanceUID,isBackgroundSegment,AnatomicRegionCodingSchemeDesignator,"
            "AnatomicRegionCodeValue,AnatomicRegionCodeMeaning\n"
            "s1,True,SCT,10200004,Background\n"
            "s1,False,SCT,10200004,Large bowel\n")
        try:
            codes = dcmterm.batch_codes(
                table, "AnatomicRegionCodeValue",
                "AnatomicRegionCodingSchemeDesignator", "AnatomicRegionCodeMeaning")
        finally:
            table.unlink()
        self.assertEqual(codes[("SCT", "10200004")]["segments"], 1)


class TestDcmtermEntireFlavour(unittest.TestCase):
    """Issue 8's evidence: the "Entire" code absent from DICOM's set, the
    structure flavour present."""

    def suggest_for(self, code, fsn):
        return dcmterm.suggest(
            [{"CodingSchemeDesignator": "SCT", "CodeValue": code, "fsn": fsn,
              "isEntireFlavour": "True", "segments": "7"}], DCM)[0]

    def test_structure_counterpart_is_suggested(self):
        row = self.suggest_for("302508007", "Entire colon (body structure)")
        self.assertEqual(row["inDcmterm"], "False")
        self.assertEqual(row["suggestedReplacement"], "71854001")
        self.assertEqual(row["replacementMeaning"], "Colon")

    def test_semantic_tag_is_stripped_before_matching(self):
        self.assertEqual(
            dcmterm.strip_semantic_tag("Entire colon (body structure)"),
            "Entire colon")

    def test_exact_match_beats_a_more_used_partial_match(self):
        """"Sigmoid colon" contains "colon" and would otherwise compete."""
        row = self.suggest_for("302508007", "Entire colon (body structure)")
        self.assertIn("60184004", row["otherCandidates"])

    def test_no_candidate_leaves_the_field_empty_for_a_decision(self):
        row = self.suggest_for("999999", "Entire zygomatic arch (body structure)")
        self.assertEqual(row["suggestedReplacement"], "")

    def test_codes_not_flagged_are_ignored(self):
        self.assertEqual(
            dcmterm.suggest([{"CodeValue": "78961009", "fsn": "Spleen structure",
                              "isEntireFlavour": "False"}], DCM), [])


# Real `dciodvfy -new -filename -allpffgitems` output, copied verbatim from runs
# against deliberately broken segmentations. Captured rather than generated so
# the suite pins the grammar the parser depends on: if a dicom3tools build
# changes the message format, these are what should fail.
DCIODVFY_OUTPUT = """Filename: "broken.dcm"
Warning - </PatientName(0010,0010)[1]> - Value dubious for this VR [PN] = \
<JANCT000> - Retired Person Name form
Segmentation
Error - </ContentLabel(0070,0080)> - Missing attribute for Type 1 Required - \
Module=<ContentIdentificationMacro>
Error - </SegmentSequence(0062,0002)[2]/SegmentLabel(0062,0005)> - Missing \
attribute for Type 1 Required - Module=<SegmentDescriptionMacro>
Error - </SegmentSequence(0062,0002)[2]/SegmentAlgorithmType(0062,0008)[1]> - \
Unrecognized enumerated value = <MAGIC>
Error - </ImageType(0008,0008)> - Bad attribute Value Multiplicity = <1> (2-n \
Required by Dictionary) Module=<GeneralImage>
Error - </PerFrameFunctionalGroupsSequence(5200,9230)[3]/\
SegmentIdentificationSequence(0062,000a)> - Missing attribute for Type 1 \
Required - Module=<SegmentationMacro>
Warning - </SegmentSequence(0062,0002)[1]/SegmentedPropertyTypeCodeSequence\
(0062,000f)[1]/CodingSchemeDesignator(0008,0102)[1]> - CodingSchemeDesignator \
is deprecated = <SRT>
""".replace("\\\n", "")


class TestDciodvfyParser(unittest.TestCase):
    """Issue 9. The parser is the part that has to be right - everything after
    it is aggregation."""

    def setUp(self):
        self.iod, self.findings = dciodvfy_check.parse_output(DCIODVFY_OUTPUT)
        self.by_attribute = {f["attributeName"]: f for f in self.findings}

    def test_iod_is_captured(self):
        """If dciodvfy picked the wrong IOD, every message is about the wrong
        rules - so the name it printed has to survive parsing."""
        self.assertEqual(self.iod, "Segmentation")

    def test_filename_line_is_not_a_finding(self):
        self.assertEqual(len(self.findings), 7)

    def test_type1_message_is_classified_and_located(self):
        finding = self.by_attribute["SegmentLabel"]
        self.assertEqual(finding["messageClass"], "MISSING_TYPE1")
        self.assertEqual(finding["module"], "SegmentDescriptionMacro")
        self.assertEqual(finding["attributeTag"], "0062,0005")
        self.assertEqual(finding["segmentItem"], 2)

    def test_object_level_message_has_no_segment_item(self):
        self.assertEqual(self.by_attribute["ContentLabel"]["segmentItem"], "")

    def test_per_frame_message_carries_the_frame_index(self):
        """-allpffgitems is what makes this message exist at all; the index is
        what lets it be attributed to a frame rather than to the whole file."""
        finding = self.by_attribute["SegmentIdentificationSequence"]
        self.assertEqual(finding["frameItem"], 3)
        self.assertEqual(finding["segmentItem"], "")

    def test_module_without_a_dash_separator_is_still_stripped(self):
        """Bad-VM messages put Module=<> straight after the value, with no
        " - " in front of it, unlike every other message."""
        finding = self.by_attribute["ImageType"]
        self.assertEqual(finding["module"], "GeneralImage")
        self.assertNotIn("Module=", finding["message"])
        self.assertEqual(finding["valueSeen"], "1")

    def test_enumerated_value_is_extracted(self):
        finding = self.by_attribute["SegmentAlgorithmType"]
        self.assertEqual(finding["messageClass"], "BAD_ENUMERATED_VALUE")
        self.assertEqual(finding["valueSeen"], "MAGIC")
        self.assertEqual(finding["module"], "")

    def test_deprecated_scheme_warning_is_promoted_to_medium(self):
        """dciodvfy calls it a Warning. It silently breaks every terminology
        lookup downstream, so the review does not."""
        finding = self.by_attribute["CodingSchemeDesignator"]
        self.assertEqual(finding["dciodvfySeverity"], "Warning")
        self.assertEqual(finding["severity"], "Medium")
        self.assertEqual(finding["messageClass"], "DEPRECATED_CODING_SCHEME")

    def test_nothing_is_ever_high(self):
        """A dciodvfy message IS the detection, and High is reserved for what
        cannot be detected."""
        self.assertNotIn("High", {f["severity"] for f in self.findings})

    def test_plain_warning_is_low(self):
        self.assertEqual(self.by_attribute["PatientName"]["severity"], "Low")

    def test_signature_blanks_values_so_counts_aggregate(self):
        self.assertEqual(
            dciodvfy_check.signature("Unrecognized enumerated value = <MAGIC>"),
            "Unrecognized enumerated value = <>")

    def test_unknown_message_is_kept_not_dropped(self):
        _, findings = dciodvfy_check.parse_output(
            "Segmentation\nError - </Foo(0008,0008)> - Something new entirely\n")
        self.assertEqual(findings[0]["messageClass"], "OTHER")
        self.assertEqual(findings[0]["message"], "Something new entirely")

    def test_pixel_data_length_is_classified(self):
        """The one message that is not about metadata: dciodvfy reads PixelData
        and checks its length against the declared geometry. Nothing else in
        the review would notice a truncated object."""
        _, findings = dciodvfy_check.parse_output(
            "Segmentation\nError - </PixelData(7fe0,0010)> - PixelData has "
            "incorrect value length = <98304> - expected 131072 dec\n")
        self.assertEqual(findings[0]["messageClass"], "BAD_VALUE_LENGTH")
        self.assertEqual(findings[0]["attributeName"], "PixelData")
        self.assertEqual(findings[0]["severity"], "Medium")

    def test_message_without_an_attribute_path_still_parses(self):
        _, findings = dciodvfy_check.parse_output(
            "Error - Information Object Not found\n")
        self.assertEqual(findings[0]["attributePath"], "")
        self.assertEqual(findings[0]["severity"], "Medium")


class TestDciodvfySegmentAttribution(unittest.TestCase):
    """The sequence item index dciodvfy prints is a position, not a
    SegmentNumber. On a labelmap they differ by one."""

    def test_item_index_maps_to_the_actual_segment_number(self):
        ds = instance([segment(0, "Background"), segment(1, "Liver")])
        index = dciodvfy_check.segment_index(ds)
        self.assertEqual(index[1], ("0", "Background"))
        self.assertEqual(index[2], ("1", "Liver"))

    def test_missing_segment_number_leaves_the_field_empty(self):
        ds = instance([segment(1, "A", omit=("SegmentNumber",))])
        self.assertEqual(dciodvfy_check.segment_index(ds)[1], ("", "A"))

    def test_summary_counts_distinct_objects_series_and_segments(self):
        rows = [
            {"severity": "Medium", "dciodvfySeverity": "Error",
             "messageClass": "MISSING_TYPE1", "attributeName": "SegmentLabel",
             "attributeTag": "0062,0005", "module": "SegmentDescriptionMacro",
             "messageSignature": "Missing attribute for Type 1 Required",
             "SOPInstanceUID": sop, "SeriesInstanceUID": "S1",
             "SegmentNumber": "1", "valueSeen": "", "viewer_url": ""}
            for sop in ("1.1", "1.2")
        ]
        summary = dciodvfy_check.summarise(rows)
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["instances"], 2)
        self.assertEqual(summary[0]["series"], 1)
        self.assertEqual(summary[0]["segments"], 2)


class TestRetiredCodingScheme(unittest.TestCase):
    """Issue 10. Computed, so it runs on every access path - which is the whole
    reason it is not just folded into issue 9."""

    def test_srt_designator_is_reported_with_its_sequences(self):
        rows = rows_for([instance([segment(
            1, "Liver", region=("T-62000", "Liver", "SRT"), category="",
            seg_type="")])])
        findings = seg_checks.retired_scheme(rows)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["retiredSchemes"], "SRT")
        self.assertEqual(findings[0]["replacementScheme"], "SCT")
        self.assertIn("AnatomicRegionCodeSequence=SRT:T-62000",
                      findings[0]["affectedSequences"])

    def test_sct_is_clean(self):
        rows = rows_for([instance([segment(1, "Liver",
                                           region=("10200004", "Liver"))])])
        self.assertEqual(seg_checks.retired_scheme(rows), [])

    def test_a_designator_with_no_code_value_is_not_reported(self):
        """An empty sequence is issue 4's business, not issue 10's."""
        rows = rows_for([instance([segment(1, "A")])])
        for row in rows:
            row["AnatomicRegionCodingSchemeDesignator"] = "SRT"
            row["AnatomicRegionCodeValue"] = ""
        self.assertEqual(seg_checks.retired_scheme(rows), [])

    def test_it_raises_series_severity_to_medium(self):
        rows = rows_for([instance([segment(
            1, "Liver", region=("T-62000", "Liver", "SRT"))], overlap="NO")])
        out = seg_checks.triage(rows, {}, set(), {}, {}, [])
        self.assertIn("RETIRED_CODING_SCHEME", out[0]["issues"])
        self.assertEqual(out[0]["worstSeverity"], "Medium")
        # It is a re-coding job, not a per-segment judgement call.
        self.assertEqual(out[0]["segmentsNeedingRecode"], 0)


class TestIodTagsJoinTheTriage(unittest.TestCase):
    """Issue 9 is per object; the triage list is per series."""

    def setUp(self):
        self.rows = rows_for([instance([segment(1, "A",
                                                region=("78961009", "Spleen"))],
                                       series="S1", overlap="NO")])

    def triage_with(self, messages):
        iod = defaultdict(Counter)
        for series, severity, message_class in messages:
            if message_class == "DEPRECATED_CODING_SCHEME":
                continue
            tag = "IOD_ERROR" if severity == "Error" else "IOD_WARNING"
            iod[series][tag] += 1
        return seg_checks.triage(self.rows, {}, set(), {}, {}, [], iod)

    def test_an_error_makes_the_series_medium(self):
        out = self.triage_with([("S1", "Error", "MISSING_TYPE1")])
        self.assertIn("IOD_ERROR", out[0]["issues"])
        self.assertEqual(out[0]["worstSeverity"], "Medium")

    def test_a_warning_only_makes_it_low(self):
        out = self.triage_with([("S1", "Warning", "DUBIOUS_VALUE_FOR_VR")])
        self.assertEqual(out[0]["issues"], "IOD_WARNING")
        self.assertEqual(out[0]["worstSeverity"], "Low")

    def test_without_the_validator_the_series_carries_no_iod_tag(self):
        """Silence here means "not checked", not "conformant" - the BigQuery
        and DICOMweb paths always look like this."""
        out = seg_checks.triage(self.rows, {}, set(), {}, {}, [])
        self.assertNotIn("IOD_", out[0]["issues"])

    def test_deprecated_scheme_does_not_double_count_as_an_iod_error(self):
        """It is issue 10's tag, computed from the table, so that the tag means
        the same thing on all three access paths."""
        out = self.triage_with([("S1", "Warning", "DEPRECATED_CODING_SCHEME")])
        self.assertNotIn("IOD_", out[0]["issues"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
