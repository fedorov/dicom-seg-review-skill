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

import csv
import json
import sys
import unittest
import unittest.mock
import urllib.error
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from pydicom.dataset import Dataset  # noqa: E402

import cielab  # noqa: E402
import dciodvfy_check  # noqa: E402
import dcmterm  # noqa: E402
import lookup_codes  # noqa: E402
import seg_attributes  # noqa: E402
import seg_checks  # noqa: E402
import seg_encoding  # noqa: E402
import seg_geometry  # noqa: E402


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
        self.assertIn(("AnatomicRegion", "91394001"), ambiguous)
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
        self.assertIn(("AnatomicRegion", "12003004"), cosmetic)

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


# ---------------------------------------------------------------------------
# Issue 13 - CIELab. The scaling is PS3.3 C.10.7.1.1; the RGB leg inverts
# PixelMed's sRGB -> D50 -> Lab, which is what dcmqi and most SEG writers do.
# ---------------------------------------------------------------------------

# sRGB -> CIELab (D50 PCS) computed independently, so a change to cielab.py
# that breaks the round trip fails here rather than quietly shifting colours.
SRGB_TO_LAB_D50 = {
    (255, 255, 255): (65535, 32896, 32896),
    (0, 0, 0): (0, 32896, 32896),
    (255, 0, 0): (35580, 53665, 50856),
    (0, 255, 0): (57552, 12519, 53710),
    (0, 0, 255): (19377, 50449, 4104),
    (128, 128, 128): (35117, 32896, 32896),
}


class TestCIELab(unittest.TestCase):
    def test_stored_values_render_to_the_expected_srgb(self):
        for rgb, stored in SRGB_TO_LAB_D50.items():
            self.assertEqual(cielab.to_rgb(stored), rgb, msg=str(stored))

    def test_scaling_follows_c_10_7_1_1(self):
        """L* 0..100 over the full range; a*/b* offset so 0x8080 is zero."""
        lightness, a_star, b_star = cielab.to_lab((65535, 32896, 32896))
        self.assertAlmostEqual(lightness, 100.0, places=3)
        self.assertAlmostEqual(a_star, 0.0, places=1)
        self.assertAlmostEqual(b_star, 0.0, places=1)
        self.assertAlmostEqual(cielab.to_lab((0, 0, 0))[1], -128.0, places=3)
        self.assertAlmostEqual(cielab.to_lab((0, 65535, 0))[1], 127.0, places=3)

    def test_parse_accepts_every_way_the_triplet_is_written(self):
        for text in ("1/2/3", "1\\2\\3", "[1, 2, 3]", "1 2 3"):
            self.assertEqual(cielab.parse(text), (1, 2, 3), msg=text)
        self.assertEqual(cielab.parse([1, 2, 3]), (1, 2, 3))

    def test_absent_is_none_but_malformed_raises(self):
        """Absence is the normal case for a Type 3 attribute and is a finding
        of its own; a present-but-wrong value is a different finding."""
        self.assertIsNone(cielab.parse(""))
        self.assertIsNone(cielab.parse(None))
        with self.assertRaises(ValueError):
            cielab.parse("1/2")
        with self.assertRaises(ValueError):
            cielab.parse("1/2/3/4")
        with self.assertRaises(ValueError):
            cielab.parse("1/2/99999")

    def test_delta_e_is_in_lab_units(self):
        red = SRGB_TO_LAB_D50[(255, 0, 0)]
        green = SRGB_TO_LAB_D50[(0, 255, 0)]
        self.assertEqual(cielab.delta_e(red, red), 0.0)
        self.assertGreater(cielab.delta_e(red, green), 100)

    def test_swatch_is_self_contained_svg(self):
        svg = cielab.swatch_svg(SRGB_TO_LAB_D50[(255, 0, 0)])
        self.assertIn('xmlns="http://www.w3.org/2000/svg"', svg)
        self.assertIn('fill="#ff0000"', svg)
        # Without a stroke a white swatch is invisible on a white page.
        self.assertIn("stroke=", svg)


class TestRecommendedColor(unittest.TestCase):
    RED = "35580/53665/50856"
    NEAR_RED = "35600/53600/50800"
    GREEN = "57552/12519/53710"

    def rows(self, *colours, **kwargs):
        """One object, one segment per colour, each a different structure."""
        same_structure = kwargs.get("same_structure", False)
        rows = []
        for index, colour in enumerate(colours, start=1):
            rows.append({
                "PatientID": "P1", "StudyInstanceUID": "3.1",
                "SeriesInstanceUID": "2.1", "SOPInstanceUID": "1.1",
                "SegmentNumber": str(index), "SegmentLabel": f"Seg {index}",
                "SegmentedPropertyTypeCodingSchemeDesignator": "SCT",
                "SegmentedPropertyTypeCodeValue": "4147007"
                if same_structure else f"100000{index}",
                "SegmentedPropertyTypeCodeMeaning": "Mass",
                "AnatomicRegionCodingSchemeDesignator": "SCT",
                "AnatomicRegionCodeValue": "10200004",
                "AnatomicRegionCodeMeaning": "Liver",
                "RecommendedDisplayCIELabValue": colour,
                "SegmentationType": kwargs.get("segmentation_type", "BINARY"),
                "PhotometricInterpretation": kwargs.get(
                    "photometric", "MONOCHROME2"),
                "SeriesDescription": "SEG", "viewer_url": "",
            })
        return rows

    def issues(self, findings):
        return {f["colorIssue"] for f in findings}

    def test_same_colour_different_structures_is_a_duplicate(self):
        findings, _ = seg_checks.recommended_color(self.rows(self.RED, self.RED))
        self.assertIn("DUPLICATE_IN_OBJECT", self.issues(findings))

    def test_same_colour_same_structure_is_not_a_finding(self):
        """The point of a colour convention. Reporting it would flag every
        consistent delivery in existence."""
        findings, _ = seg_checks.recommended_color(
            self.rows(self.RED, self.RED, same_structure=True))
        self.assertNotIn("DUPLICATE_IN_OBJECT", self.issues(findings))
        self.assertNotIn("CONFUSABLE_IN_OBJECT", self.issues(findings))

    def test_near_colours_are_confusable_below_the_threshold_only(self):
        rows = self.rows(self.RED, self.NEAR_RED)
        findings, _ = seg_checks.recommended_color(rows, threshold=10.0)
        self.assertIn("CONFUSABLE_IN_OBJECT", self.issues(findings))
        findings, _ = seg_checks.recommended_color(rows, threshold=0.001)
        self.assertNotIn("CONFUSABLE_IN_OBJECT", self.issues(findings))

    def test_distinct_colours_are_clean(self):
        findings, palette = seg_checks.recommended_color(
            self.rows(self.RED, self.GREEN))
        self.assertEqual(self.issues(findings), set())
        self.assertEqual(len(palette), 2)

    def test_absent_colour_is_reported_but_not_as_a_collision(self):
        findings, palette = seg_checks.recommended_color(self.rows("", ""))
        self.assertEqual(self.issues(findings), {"ABSENT"})
        self.assertEqual(palette, [])

    def test_malformed_colour_is_separated_from_absent(self):
        findings, _ = seg_checks.recommended_color(self.rows("1/2"))
        self.assertEqual(self.issues(findings), {"MALFORMED"})

    def test_palette_colour_labelmap_must_not_carry_the_attribute(self):
        """PS3.3 C.8.20.2: the palette already carries the colour."""
        findings, _ = seg_checks.recommended_color(
            self.rows(self.RED, segmentation_type="LABELMAP",
                      photometric="PALETTE COLOR"))
        self.assertIn("NOT_PERMITTED", self.issues(findings))

    def test_one_structure_two_colours_across_the_batch(self):
        rows = self.rows(self.RED, same_structure=True)
        other = self.rows(self.GREEN, same_structure=True)
        other[0]["SOPInstanceUID"] = "1.2"
        other[0]["SeriesInstanceUID"] = "2.2"
        findings, _ = seg_checks.recommended_color(rows + other)
        self.assertIn("INCONSISTENT_ACROSS_BATCH", self.issues(findings))

    def test_collisions_are_one_row_per_segment_not_per_pair(self):
        """A three-way collision is one recolouring job, not three."""
        findings, _ = seg_checks.recommended_color(
            self.rows(self.RED, self.RED, self.RED))
        collisions = [f for f in findings
                      if f["colorIssue"] == "DUPLICATE_IN_OBJECT"]
        self.assertEqual(len(collisions), 3)
        self.assertEqual(collisions[0]["collidesWithSegments"], "2;3")

    def test_palette_counts_structures_and_marks_sharing(self):
        _, palette = seg_checks.recommended_color(self.rows(self.RED, self.RED))
        self.assertEqual(len(palette), 1)
        self.assertEqual(palette[0]["segments"], 2)
        self.assertEqual(palette[0]["distinctStructures"], 2)
        self.assertEqual(palette[0]["sharedWithinObject"], "True")
        self.assertEqual(palette[0]["hex"], "#ff0000")


# ---------------------------------------------------------------------------
# Issue 14 - does a non-MANUAL segment say what made it?
# ---------------------------------------------------------------------------


class TestAlgorithmIdentification(unittest.TestCase):
    def row(self, **overrides):
        row = {
            "PatientID": "P1", "StudyInstanceUID": "3.1",
            "SeriesInstanceUID": "2.1", "SOPInstanceUID": "1.1",
            "SegmentNumber": "1", "SegmentLabel": "Tumour",
            "SegmentAlgorithmType": "AUTOMATIC",
            "SegmentAlgorithmName": "nnU-Net",
            "hasAlgorithmIdentification": "True",
            "AlgorithmName": "nnU-Net", "AlgorithmVersion": "2.4.1",
            "AlgorithmSource": "Acme", "AlgorithmNameCodeMeaning": "",
            "ManufacturerModelName": "dcmqi", "SoftwareVersions": "1.3.4",
            "SeriesDescription": "SEG", "viewer_url": "",
        }
        row.update(overrides)
        return row

    def issues(self, **overrides):
        findings = seg_checks.algorithm_identification([self.row(**overrides)])
        return findings[0]["algorithmIssues"].split("; ") if findings else []

    def test_a_fully_identified_automatic_segment_is_clean(self):
        self.assertEqual(self.issues(), [])

    def test_manual_segments_are_required_to_say_nothing(self):
        """Type 1C bites only where SegmentAlgorithmType is not MANUAL."""
        self.assertEqual(
            self.issues(SegmentAlgorithmType="MANUAL", SegmentAlgorithmName="",
                        hasAlgorithmIdentification="False"),
            [])

    def test_missing_name_on_an_automatic_segment_is_the_1c_violation(self):
        self.assertIn("NAME_MISSING", self.issues(SegmentAlgorithmName=""))

    def test_semiautomatic_is_also_covered(self):
        self.assertIn("NAME_MISSING",
                      self.issues(SegmentAlgorithmType="SEMIAUTOMATIC",
                                  SegmentAlgorithmName=""))

    def test_a_name_that_identifies_nothing(self):
        for name in ("unknown", "N/A", "  AI  ", "segmentation", "dcmqi"):
            self.assertIn("NAME_UNINFORMATIVE",
                          self.issues(SegmentAlgorithmName=name), msg=name)

    def test_a_real_name_is_not_uninformative(self):
        for name in ("nnU-Net", "TotalSegmentator v2", "MONAI Auto3DSeg"):
            self.assertNotIn("NAME_UNINFORMATIVE",
                             self.issues(SegmentAlgorithmName=name), msg=name)

    def test_no_identification_sequence_means_no_version(self):
        issues = self.issues(hasAlgorithmIdentification="False",
                             AlgorithmVersion="")
        self.assertIn("NO_IDENTIFICATION", issues)
        self.assertNotIn("NO_VERSION", issues)

    def test_sequence_present_but_version_empty(self):
        issues = self.issues(AlgorithmVersion="")
        self.assertIn("NO_VERSION", issues)
        self.assertNotIn("NO_IDENTIFICATION", issues)

    def test_the_two_tags_split_conformance_from_provenance(self):
        """Built through the extractor, so the triage list is fed exactly the
        columns a real run would give it."""
        no_name = segment(1, "A")
        versioned = segment(2, "B")
        versioned.SegmentAlgorithmName = "nnU-Net"
        identification = Dataset()
        identification.AlgorithmName = "nnU-Net"
        identification.AlgorithmVersion = ""
        versioned.SegmentationAlgorithmIdentificationSequence = [identification]
        rows = rows_for([instance([no_name, versioned])])

        findings = seg_checks.algorithm_identification(rows)
        reasons = {f["SegmentNumber"]: f["algorithmIssues"] for f in findings}
        self.assertIn("NAME_MISSING", reasons["1"])
        self.assertIn("NO_VERSION", reasons["2"])

        out = seg_checks.triage(rows, {}, set(), {}, {}, [],
                                algorithms=findings)
        self.assertIn("ALGORITHM_NAME_MISSING", out[0]["issues"])
        self.assertIn("ALGORITHM_UNIDENTIFIED", out[0]["issues"])
        self.assertEqual(out[0]["worstSeverity"], "Medium")


# ---------------------------------------------------------------------------
# Issue 11 - empty frames. The bit-packing rule is PS3.5 8.1: BINARY frames in
# Native Format are NOT padded, so a frame can start mid-byte.
# ---------------------------------------------------------------------------


def pack(bits):
    """Pixel bits -> DICOM's packing: first pixel in the least significant bit."""
    data = bytearray((len(bits) + 7) // 8)
    for index, bit in enumerate(bits):
        if bit:
            data[index // 8] |= 1 << (index % 8)
    return bytes(data)


class TestEmptyFrames(unittest.TestCase):
    def test_byte_aligned_binary_frames(self):
        data = pack([0] * 64 + [0] * 32 + [1] + [0] * 31 + [0] * 64)
        verdicts = [seg_encoding.frame_is_empty(data, i, 8, 8, 1)
                    for i in range(3)]
        self.assertEqual(verdicts, [True, False, True])

    def test_frames_that_start_mid_byte(self):
        """3x3 = 9 bits a frame, so frame 1 begins in the middle of byte 1. A
        byte-sliced implementation reads frame 0's pixels here and calls an
        empty frame full."""
        bits = [0] * 27
        bits[9 + 4] = 1
        data = pack(bits)
        verdicts = [seg_encoding.frame_is_empty(data, i, 3, 3, 1)
                    for i in range(3)]
        self.assertEqual(verdicts, [True, False, True])

    def test_a_set_pixel_in_the_last_frame_is_seen(self):
        bits = [0] * 27
        bits[26] = 1
        data = pack(bits)
        self.assertFalse(seg_encoding.frame_is_empty(data, 2, 3, 3, 1))

    def test_multi_byte_frames(self):
        data = bytes(64) + bytes([0, 3] + [0] * 62) + bytes(64)
        verdicts = [seg_encoding.frame_is_empty(data, i, 8, 8, 8)
                    for i in range(3)]
        self.assertEqual(verdicts, [True, False, True])

    def test_short_pixel_data_raises_rather_than_reading_past_the_end(self):
        """NumberOfFrames is a claim; the pixel data is the measurement."""
        with self.assertRaises(ValueError):
            seg_encoding.frame_is_empty(bytes(8), 3, 8, 8, 1)

    def test_encapsulated_frames_are_byte_aligned(self):
        """PS3.5 A.4.13: one frame, one fragment - so unlike the native case a
        frame starts on a byte and the bits past the last pixel are not ours."""
        payload = pack([0] * 9 + [1] * 7)  # pixels 9..15 are padding, not data
        self.assertTrue(seg_encoding.payload_is_empty(payload, 3, 3, 1))
        self.assertFalse(
            seg_encoding.payload_is_empty(pack([0] * 8 + [1]), 3, 3, 1))

    def test_labelmap_values_are_about_values_not_frames(self):
        """One LABELMAP frame carries every segment, so "segment 3 is empty"
        means the value 3 appears nowhere."""
        self.assertEqual(seg_encoding.labelmap_values(bytes([0, 1, 1, 4]), 8),
                         {0, 1, 4})
        self.assertEqual(
            seg_encoding.labelmap_values(b"\x01\x00\x02\x00", 16), {1, 2})


class TestEncapsulatedPixelData(unittest.TestCase):
    def encapsulate(self, fragments):
        import struct
        out = struct.pack("<HHI", 0xFFFE, 0xE000, 0)  # empty Basic Offset Table
        for fragment in fragments:
            out += struct.pack("<HHI", 0xFFFE, 0xE000, len(fragment)) + fragment
        return out + struct.pack("<HHI", 0xFFFE, 0xE0DD, 0)

    def test_the_basic_offset_table_is_not_a_frame(self):
        data = self.encapsulate([b"ab", b"cd"])
        self.assertEqual(seg_encoding.split_fragments(data), [b"ab", b"cd"])

    def test_raw_deflate_and_zlib_wrapped_both_inflate(self):
        """PS3.5 A.4.13 says RFC1951, i.e. raw. Writers that wrap it exist, and
        being strict would report a decode failure on readable frames."""
        import zlib
        payload = b"\x00" * 32
        self.assertEqual(seg_encoding.inflate(zlib.compress(payload)[2:-4]),
                         payload)
        self.assertEqual(seg_encoding.inflate(zlib.compress(payload)), payload)

    def test_a_non_item_tag_is_an_error_not_a_silent_truncation(self):
        import struct
        with self.assertRaises(ValueError):
            seg_encoding.split_fragments(
                struct.pack("<HHI", 0x0008, 0x0018, 2) + b"ab")


class TestEncodingTags(unittest.TestCase):
    def tags_for(self, **overrides):
        import csv as csv_module
        import tempfile
        row = {
            "SeriesInstanceUID": "2.1", "isLossy": "False",
            "isCompressed": "True", "emptyFrames": "0", "emptySegmentCount": "0",
        }
        row.update(overrides)
        with tempfile.NamedTemporaryFile(
                "w", suffix=".csv", delete=False, newline="") as handle:
            writer = csv_module.DictWriter(handle, fieldnames=list(row))
            writer.writeheader()
            writer.writerow(row)
            path = handle.name
        return seg_checks.encoding_tags(path)["2.1"]

    def test_lossy_is_taken_from_the_transfer_syntax(self):
        self.assertIn("LOSSY_COMPRESSED", self.tags_for(isLossy="True"))

    def test_lossy_image_compression_01_alone_is_not_a_finding(self):
        """PS3.3 C.8.20.2.2 requires 01 when the SOURCE images were lossy
        compressed, so an intact segmentation of a lossy CT carries it."""
        tags = self.tags_for(LossyImageCompression="01")
        self.assertNotIn("LOSSY_COMPRESSED", tags)

    def test_uncompressed_and_empty_frames(self):
        tags = self.tags_for(isCompressed="False", emptyFrames="12",
                             emptySegmentCount="1")
        self.assertEqual(
            set(tags),
            {"UNCOMPRESSED", "EMPTY_FRAMES_RETAINED", "EMPTY_SEGMENT"})

    def test_lossy_and_uncompressed_are_exclusive(self):
        tags = self.tags_for(isLossy="True", isCompressed="True")
        self.assertNotIn("UNCOMPRESSED", tags)

    def test_the_tags_reach_the_triage_list(self):
        rows = rows_for([instance([segment(1, "A")], series="2.1")])
        encoding = {"2.1": Counter({"LOSSY_COMPRESSED": 1})}
        out = seg_checks.triage(rows, {}, set(), {}, {}, [], encoding=encoding)
        self.assertIn("LOSSY_COMPRESSED", out[0]["issues"])
        self.assertEqual(out[0]["worstSeverity"], "High")

    def test_a_series_with_no_encoding_row_carries_no_encoding_tag(self):
        """Silence means "not checked", not "uncompressed" - the BigQuery and
        DICOMweb paths always look like this."""
        rows = rows_for([instance([segment(1, "A")], series="2.1")])
        out = seg_checks.triage(rows, {}, set(), {}, {}, [])
        for tag in ("LOSSY_COMPRESSED", "UNCOMPRESSED", "EMPTY_FRAMES_RETAINED",
                    "EMPTY_SEGMENT"):
            self.assertNotIn(tag, out[0]["issues"])


def type_coded(label, code, meaning, series="S1", sop="1.1",
               category=("91723000", "Anatomical Structure")):
    """A segment whose anatomy is the TYPE code and which has no
    AnatomicRegionSequence at all - the common, conformant shape."""
    seg = segment(1, label, region=None, category=None, seg_type=None)
    seg.SegmentedPropertyCategoryCodeSequence = [code_item(*category)]
    seg.SegmentedPropertyTypeCodeSequence = [code_item(code, meaning)]
    return instance([seg], series=series, sop=sop)


code_item = code  # the module-level helper, under a name segment() cannot shadow


class TestCodeSequences(unittest.TestCase):
    """The terminology checks judge the type and category sequences too. The
    anatomic region is Type 3, and on many deliveries the anatomy is the TYPE
    code with no region at all - a region-only review reports those clean."""

    def rows(self):
        return rows_for([
            type_coded("Liver", "10200004", "Liver", series="S1", sop="1.1"),
            type_coded("Bowel", "10200004", "Large bowel", series="S2", sop="1.2"),
        ])

    def test_ambiguity_is_found_in_the_type_sequence(self):
        findings, ambiguous, _ = seg_checks.ambiguous_codes(self.rows())
        self.assertIn(("SegmentedPropertyType", "10200004"), ambiguous)
        self.assertEqual({f["codeSequence"] for f in findings}, {"SegmentedPropertyType"})
        self.assertEqual({f["CodeValue"] for f in findings}, {"10200004"})

    def test_a_region_only_review_would_miss_it(self):
        _, ambiguous, _ = seg_checks.ambiguous_codes(self.rows(), ["AnatomicRegion"])
        self.assertEqual(ambiguous, {})

    def test_a_code_is_judged_per_role(self):
        """The same code as the region with one meaning and as the type with
        another is two roles, not one ambiguous code."""
        rows = rows_for([instance([segment(1, "A", region=("10200004", "Liver"),
                                           seg_type="10200004")])])
        rows[0]["SegmentedPropertyTypeCodeMeaning"] = "Liver structure"
        _, ambiguous, _ = seg_checks.ambiguous_codes(rows)
        self.assertEqual(ambiguous, {})

    def test_the_type_sequence_reaches_the_triage_list(self):
        rows = self.rows()
        _, ambiguous, cosmetic = seg_checks.ambiguous_codes(rows)
        out = {r["SeriesInstanceUID"]: r
               for r in seg_checks.triage(rows, ambiguous, cosmetic, {}, {}, [])}
        self.assertIn("CODE_AMBIGUOUS_ELSEWHERE", out["S1"]["issues"])
        # "Large bowel" sorts before "Liver", so it is the tie-broken dominant
        # meaning and S1 carries the minority reading.
        self.assertIn("CODE_MEANING_MINORITY", out["S1"]["issues"])

    def test_curated_verdict_applies_to_the_type_sequence(self):
        rows = rows_for([type_coded("Bowel", "10200004", "Large bowel")])
        verdicts = {("10200004", "SCT", "Large bowel"): "WRONG_ANATOMY"}
        out = seg_checks.triage(rows, {}, set(), {}, verdicts, [])
        self.assertEqual(out[0]["worstSeverity"], "High")
        self.assertEqual(out[0]["segmentsNeedingRecode"], 1)

    def test_a_segment_offending_in_two_sequences_is_one_job(self):
        rows = rows_for([instance([segment(1, "A", region=("10200004", "Large bowel"),
                                           seg_type="10200004")])])
        rows[0]["SegmentedPropertyTypeCodeMeaning"] = "Large bowel"
        verdicts = {("10200004", "SCT", "Large bowel"): "WRONG_ANATOMY"}
        out = seg_checks.triage(rows, {}, set(), {}, verdicts, [])
        self.assertEqual(out[0]["segmentsNeedingRecode"], 1)

    def test_entire_flavour_is_found_in_the_type_sequence(self):
        rows = rows_for([type_coded("Colon", "302508007", "Colon")])
        path = Path(__file__).resolve().parent / "_codes.csv"
        path.write_text("CodingSchemeDesignator,CodeValue,fsn,isEntireFlavour\n"
                        "SCT,302508007,Entire colon (body structure),True\n")
        self.addCleanup(path.unlink)
        findings, entire = seg_checks.entire_flavour(rows, path)
        self.assertEqual(findings[0]["codeSequence"], "SegmentedPropertyType")
        out = seg_checks.triage(rows, {}, set(), entire, {}, [])
        self.assertIn("ENTIRE_CODE_FLAVOUR", out[0]["issues"])

    def test_summary_says_which_sequence_carries_the_anatomy(self):
        summary = {s["codeSequence"]: s
                   for s in seg_checks.code_sequence_summary(self.rows())}
        self.assertEqual(summary["AnatomicRegion"]["segmentsWithCode"], 0)
        self.assertEqual(summary["SegmentedPropertyType"]["segmentsWithCode"], 2)
        self.assertEqual(summary["SegmentedPropertyType"]["distinctMeanings"], 2)


class TestReviewTable(unittest.TestCase):
    """The curated table's header is a contract: a wrong one used to join
    nothing and report nothing."""

    def write(self, text):
        path = Path(__file__).resolve().parent / "_review.csv"
        path.write_text(text)
        self.addCleanup(path.unlink)
        return path

    def test_wrong_header_is_refused_naming_the_expected_one(self):
        path = self.write("code,meaning,verdict\n10200004,Large bowel,WRONG_ANATOMY\n")
        with self.assertRaises(ValueError) as caught:
            seg_checks.load_review(path)
        self.assertIn("CodeValue", str(caught.exception))
        self.assertIn(",".join(seg_checks.REVIEW_COLUMNS), str(caught.exception))

    def test_unknown_verdict_is_refused(self):
        path = self.write("CodeValue,meaningRecorded,verdict\n10200004,Large bowel,WRONG\n")
        with self.assertRaises(ValueError):
            seg_checks.load_review(path)

    def test_verdict_without_a_sequence_applies_to_any(self):
        path = self.write(
            "CodeValue,CodingSchemeDesignator,codeSequence,meaningRecorded,verdict\n"
            "10200004,SCT,,Large bowel,WRONG_ANATOMY\n")
        rows = rows_for([type_coded("Bowel", "10200004", "Large bowel")])
        one, three = seg_checks.curated(rows, seg_checks.load_review(path))
        self.assertEqual(len(one), 1)
        self.assertEqual(one[0]["codeSequence"], "SegmentedPropertyType")
        self.assertEqual(three, [])

    def test_verdict_scoped_to_a_sequence_does_not_reach_another(self):
        path = self.write(
            "CodeValue,CodingSchemeDesignator,codeSequence,meaningRecorded,verdict\n"
            "10200004,SCT,AnatomicRegion,Large bowel,WRONG_ANATOMY\n")
        rows = rows_for([type_coded("Bowel", "10200004", "Large bowel")])
        one, _ = seg_checks.curated(rows, seg_checks.load_review(path))
        self.assertEqual(one, [])

    def test_the_shipped_template_loads(self):
        template = Path(__file__).resolve().parent.parent / "templates" / "review.csv"
        review = seg_checks.load_review(template)
        self.assertTrue(review)
        self.assertTrue(all(e["verdict"] in seg_checks.VERDICTS for e in review.values()))


class TestSegmentNumbers(unittest.TestCase):
    """Issue 16. PS3.3 C.8.20.2.4: unique within the instance; 1..n by 1 where
    the type is BINARY or FRACTIONAL."""

    def rows(self, numbers, segmentation_type):
        rows = rows_for([instance([segment(n, f"S{n}") for n in numbers])])
        for row in rows:
            row["SegmentationType"] = segmentation_type
        return rows

    def test_binary_numbered_from_one_is_clean(self):
        self.assertEqual(seg_checks.segment_numbers(self.rows([1, 2], "BINARY")), [])

    def test_a_gap_on_binary_is_not_sequential(self):
        [finding] = seg_checks.segment_numbers(self.rows([1, 3], "BINARY"))
        self.assertEqual(finding["segmentNumberProblem"], "NOT_SEQUENTIAL")
        self.assertEqual(finding["segmentNumbers"], "1/3")

    def test_zero_on_binary_is_the_defect(self):
        [finding] = seg_checks.segment_numbers(self.rows([0, 1], "BINARY"))
        self.assertEqual(finding["segmentNumberProblem"], "NOT_SEQUENTIAL")

    def test_labelmap_background_and_gaps_are_permitted(self):
        self.assertEqual(seg_checks.segment_numbers(self.rows([0, 2, 7], "LABELMAP")), [])

    def test_duplicate_is_reported_whatever_the_type(self):
        [finding] = seg_checks.segment_numbers(self.rows([1, 1], "LABELMAP"))
        self.assertEqual(finding["segmentNumberProblem"], "DUPLICATE")
        self.assertEqual(finding["duplicateNumbers"], "1")

    def test_tags_reach_the_series_as_medium(self):
        rows = self.rows([1, 3], "FRACTIONAL")
        numbering = seg_checks.segment_numbers(rows)
        out = seg_checks.triage(rows, {}, set(), {}, {}, [], numbering=numbering)
        self.assertIn("SEGMENT_NUMBER_NOT_SEQUENTIAL", out[0]["issues"])
        self.assertEqual(out[0]["worstSeverity"], "Medium")


class TestFrameOfReference(unittest.TestCase):
    """Issue 17. PS3.3 A.51.1: same Frame of Reference as referenced images
    that define one. Silence where the reference was never resolved."""

    def rows(self, own, theirs, found, referenced="R1"):
        rows = rows_for([instance([segment(1, "A")])])
        rows[0].update({"FrameOfReferenceUID": own,
                        "referencedSeriesInstanceUID": referenced,
                        "referencedFrameOfReferenceUID": theirs,
                        "referencedSeriesFound": found})
        return rows

    def test_mismatch_is_reported(self):
        [finding] = seg_checks.frame_of_reference(self.rows("F1", "F2", "True"))
        self.assertEqual(finding["frameOfReferenceProblem"], "FRAME_OF_REFERENCE_MISMATCH")

    def test_match_is_clean(self):
        self.assertEqual(seg_checks.frame_of_reference(self.rows("F1", "F1", "True")), [])

    def test_unresolved_reference_is_silence_not_a_pass(self):
        self.assertEqual(seg_checks.frame_of_reference(self.rows("F1", "", "")), [])

    def test_a_series_not_found_is_the_dangling_reference(self):
        [finding] = seg_checks.frame_of_reference(self.rows("F1", "", "False"))
        self.assertEqual(finding["frameOfReferenceProblem"], "REFERENCED_SERIES_MISSING")

    def test_bigquery_lowercase_booleans_are_read(self):
        [finding] = seg_checks.frame_of_reference(self.rows("F1", "", "false"))
        self.assertEqual(finding["frameOfReferenceProblem"], "REFERENCED_SERIES_MISSING")

    def test_tags_reach_the_series(self):
        rows = self.rows("F1", "F2", "True")
        out = seg_checks.triage(rows, {}, set(), {}, {}, [],
                                frames=seg_checks.frame_of_reference(rows))
        self.assertIn("FRAME_OF_REFERENCE_MISMATCH", out[0]["issues"])
        self.assertEqual(out[0]["worstSeverity"], "Medium")

    def test_extractor_records_found_only_when_a_lookup_was_attempted(self):
        ds = instance([segment(1, "A")])
        ds.FrameOfReferenceUID = "F1"
        ref = Dataset()
        ref.SeriesInstanceUID = "R1"
        ds.ReferencedSeriesSequence = [ref]
        not_attempted = seg_attributes.extract_instance(ds)[0]
        self.assertEqual(not_attempted["referencedSeriesFound"], "")
        self.assertEqual(not_attempted["referencedSeriesCount"], "1")
        absent = seg_attributes.extract_instance(ds, referenced={})[0]
        self.assertEqual(absent["referencedSeriesFound"], "False")
        present = seg_attributes.extract_instance(
            ds, referenced={"R1": {"FrameOfReferenceUID": "F2"}})[0]
        self.assertEqual(present["referencedSeriesFound"], "True")
        self.assertEqual(present["referencedFrameOfReferenceUID"], "F2")


class TestGeometryExtraction(unittest.TestCase):
    """Issue 18, the segmentation's half. Pixel Measures and Plane Orientation
    are legal in either functional groups macro, and an object whose frames
    disagree has no one grid."""

    def measures(self, spacing=(1.0, 1.0), thickness=2.0, between=2.0):
        item = Dataset()
        item.PixelSpacing = list(spacing)
        item.SliceThickness = thickness
        item.SpacingBetweenSlices = between
        return item

    def orientation(self, cosines=(1, 0, 0, 0, 1, 0)):
        item = Dataset()
        item.ImageOrientationPatient = list(cosines)
        return item

    def group(self, **kwargs):
        item = Dataset()
        if "measures" in kwargs:
            item.PixelMeasuresSequence = [kwargs["measures"]]
        if "orientation" in kwargs:
            item.PlaneOrientationSequence = [kwargs["orientation"]]
        return item

    def seg(self, shared=None, frames=None, rows=64, columns=64):
        ds = instance([segment(1, "A")])
        ds.Rows = rows
        ds.Columns = columns
        if shared is not None:
            ds.SharedFunctionalGroupsSequence = [shared]
        if frames is not None:
            ds.PerFrameFunctionalGroupsSequence = frames
        return ds

    def test_shared_groups_are_read(self):
        ds = self.seg(self.group(measures=self.measures(),
                                 orientation=self.orientation()))
        [row] = seg_attributes.extract_instance(ds)
        self.assertEqual(row["geometrySource"], "SHARED")
        self.assertEqual(row["Rows"], "64")
        self.assertEqual(row["PixelSpacing"], "1.0/1.0")
        self.assertEqual(row["SpacingBetweenSlices"], "2.0")
        self.assertEqual(row["ImageOrientationPatient"],
                         "1.0/0.0/0.0/0.0/1.0/0.0")

    def test_per_frame_groups_are_the_fallback(self):
        frame = self.group(measures=self.measures(), orientation=self.orientation())
        [row] = seg_attributes.extract_instance(self.seg(frames=[frame, frame]))
        self.assertEqual(row["geometrySource"], "PER_FRAME_UNIFORM")
        self.assertEqual(row["PixelSpacing"], "1.0/1.0")

    def test_frames_that_disagree_leave_the_value_empty(self):
        a = self.group(measures=self.measures(spacing=(1.0, 1.0)))
        b = self.group(measures=self.measures(spacing=(2.0, 2.0)))
        [row] = seg_attributes.extract_instance(self.seg(frames=[a, b]))
        self.assertEqual(row["geometrySource"], "PER_FRAME_VARYING")
        self.assertEqual(row["PixelSpacing"], "")

    def test_macros_may_be_split_between_the_two(self):
        """Shared Pixel Measures, per-frame Plane Orientation. Weakest wins."""
        frame = self.group(orientation=self.orientation())
        ds = self.seg(self.group(measures=self.measures()), frames=[frame, frame])
        [row] = seg_attributes.extract_instance(ds)
        self.assertEqual(row["PixelSpacing"], "1.0/1.0")
        self.assertEqual(row["ImageOrientationPatient"],
                         "1.0/0.0/0.0/0.0/1.0/0.0")
        self.assertEqual(row["geometrySource"], "PER_FRAME_UNIFORM")

    def test_no_functional_groups_is_absent_not_a_crash(self):
        [row] = seg_attributes.extract_instance(self.seg())
        self.assertEqual(row["geometrySource"], "ABSENT")
        self.assertEqual(row["ImageOrientationPatient"], "")


class TestSourceImageReferenceLevel(unittest.TestCase):
    """Issue 18. ReferencedSeriesSequence is the only SERIES-level link a SEG
    has; the instance-level ones are conformant and unfollowable."""

    def level(self, ds):
        return seg_attributes.extract_instance(ds)[0]["sourceImageReferenceLevel"]

    def test_referenced_series_is_the_series_level_link(self):
        ds = instance([segment(1, "A")])
        ref = Dataset()
        ref.SeriesInstanceUID = "R1"
        ds.ReferencedSeriesSequence = [ref]
        self.assertEqual(self.level(ds), "SERIES")

    def test_source_image_sequence_is_instance_only(self):
        ds = instance([segment(1, "A")])
        source = Dataset()
        source.ReferencedSOPInstanceUID = "1.2.3"
        ds.SourceImageSequence = [source]
        self.assertEqual(self.level(ds), "INSTANCE_ONLY")

    def test_per_frame_derivation_is_instance_only(self):
        ds = instance([segment(1, "A")])
        source = Dataset()
        source.ReferencedSOPInstanceUID = "1.2.3"
        derivation = Dataset()
        derivation.SourceImageSequence = [source]
        frame = Dataset()
        frame.DerivationImageSequence = [derivation]
        ds.PerFrameFunctionalGroupsSequence = [frame]
        self.assertEqual(self.level(ds), "INSTANCE_ONLY")

    def test_nothing_at_all_is_none(self):
        self.assertEqual(self.level(instance([segment(1, "A")])), "NONE")


class TestGeometryComparison(unittest.TestCase):
    """Issue 18's comparison. Nothing here is a conformance verdict, and
    "not compared" must never read as a match."""

    SEG = {
        "Rows": "64", "Columns": "64",
        "PixelSpacing": "1.0/1.0", "SliceThickness": "2.0",
        "SpacingBetweenSlices": "2.0",
        "ImageOrientationPatient": "1/0/0/0/1/0",
        "geometrySource": "SHARED",
        "sourceImageReferenceLevel": "SERIES",
    }
    SOURCE = {
        "Rows": 64, "Columns": 64, "PixelSpacing": [1.0, 1.0],
        "SliceThickness": 2.0, "SpacingBetweenSlices": 2.0,
        "ImageOrientationPatient": [1, 0, 0, 0, 1, 0],
    }

    def compare(self, seg=None, source=None):
        return seg_geometry.compare(
            seg={**self.SEG, **(seg or {})},
            source=None if source is False else {**self.SOURCE, **(source or {})},
            resolved_by="local_files", in_idc="True",
            spacing_tolerance=1e-3, orientation_tolerance=0.1)

    def observations(self, **kwargs):
        return set(self.compare(**kwargs)["geometryObservation"].split("; "))

    def test_identical_grids_match(self):
        self.assertEqual(self.observations(), {"GEOMETRY_MATCHES"})

    def test_grid_size_difference_is_reported(self):
        self.assertIn("GRID_SIZE_DIFFERS", self.observations(source={"Rows": 128}))

    def test_pixel_spacing_difference_is_reported(self):
        self.assertIn("PIXEL_SPACING_DIFFERS",
                      self.observations(source={"PixelSpacing": [0.5, 0.5]}))

    def test_ds_rounding_is_not_a_mismatch(self):
        """7.031003e-01 against 0.7031 is one spacing written twice. An exact
        comparison would report a batch of false mismatches."""
        observations = self.observations(
            seg={"PixelSpacing": "7.031003e-01/7.031003e-01"},
            source={"PixelSpacing": [0.7031, 0.7031]})
        self.assertNotIn("PIXEL_SPACING_DIFFERS", observations)

    def test_orientation_is_compared_as_an_angle(self):
        observations = self.observations(
            seg={"ImageOrientationPatient":
                 "1.000000e+000/-2.038648e-010/0/0/1/0"})
        self.assertNotIn("ORIENTATION_DIFFERS", observations)

    def test_in_plane_rotation_is_caught(self):
        """A slice normal alone would miss this: same plane, different axes."""
        observations = self.observations(
            source={"ImageOrientationPatient": [0, 1, 0, -1, 0, 0]})
        self.assertIn("ORIENTATION_DIFFERS", observations)

    def test_absent_source_orientation_is_not_a_match(self):
        observations = self.observations(source={"ImageOrientationPatient": []})
        self.assertIn("ORIENTATION_NOT_COMPARED", observations)
        self.assertIn("GEOMETRY_MATCHES_PARTIAL", observations)
        self.assertNotIn("GEOMETRY_MATCHES", observations)

    def test_slice_thickness_does_not_stand_in_for_spacing(self):
        observations = self.observations(source={"SpacingBetweenSlices": None})
        self.assertIn("SLICE_SPACING_NOT_COMPARED", observations)
        self.assertNotIn("SLICE_SPACING_DIFFERS", observations)

    def test_unresolved_source_is_reported_not_passed(self):
        finding = seg_geometry.compare(
            seg=self.SEG, source=None, resolved_by="", in_idc="False",
            spacing_tolerance=1e-3, orientation_tolerance=0.1)
        self.assertEqual(finding["geometryObservation"], "SOURCE_NOT_IN_IDC")

    def test_per_frame_varying_segmentation_has_no_grid(self):
        observations = self.observations(
            seg={"geometrySource": "PER_FRAME_VARYING", "PixelSpacing": "",
                 "ImageOrientationPatient": "", "SpacingBetweenSlices": "",
                 "SliceThickness": ""})
        self.assertIn("SEG_GEOMETRY_PER_FRAME", observations)

    def test_only_differences_become_triage_tags(self):
        """The NOT_COMPARED observations must raise nothing."""
        for observation in ("ORIENTATION_NOT_COMPARED", "GEOMETRY_MATCHES",
                            "GEOMETRY_MATCHES_PARTIAL",
                            "SOURCE_GEOMETRY_UNAVAILABLE"):
            self.assertNotIn(observation, seg_geometry.TAGS)
        self.assertEqual(seg_geometry.TAGS["GRID_SIZE_DIFFERS"],
                         "GEOMETRY_DIFFERS")

    def test_every_observation_has_a_place_in_the_order(self):
        self.assertEqual(
            set(seg_geometry.TAGS) - set(seg_geometry.OBSERVATION_ORDER), set())

    def test_tags_reach_the_series_at_low(self):
        rows = rows_for([instance([segment(1, "A")])])
        geometry = {rows[0]["SeriesInstanceUID"]: Counter({"GEOMETRY_DIFFERS": 1})}
        [out] = seg_checks.triage(rows, {}, set(), {}, {}, [], geometry=geometry)
        self.assertIn("GEOMETRY_DIFFERS", out["issues"])
        self.assertEqual(out["worstSeverity"], "Low")


class TestSliceSpacingMeasurement(unittest.TestCase):
    """Issue 18. The spacing is measured from the positions, because the order
    instances arrive in is not the order they are stacked in."""

    AXIAL = [1, 0, 0, 0, 1, 0]

    def test_shuffled_positions_still_give_the_spacing(self):
        positions = [[0, 0, z] for z in (6.0, 0.0, 4.0, 2.0)]
        spacing, regular = seg_geometry.slice_spacing(self.AXIAL, positions)
        self.assertAlmostEqual(spacing, 2.0)
        self.assertTrue(regular)

    def test_a_gap_makes_the_series_irregular(self):
        positions = [[0, 0, z] for z in (0.0, 2.0, 4.0, 10.0)]
        _, regular = seg_geometry.slice_spacing(self.AXIAL, positions)
        self.assertFalse(regular)

    def test_oblique_orientation_projects_onto_its_own_normal(self):
        import math
        half = math.sqrt(0.5)
        orientation = [1, 0, 0, 0, half, half]
        positions = [[0, -half * d, half * d] for d in (0.0, 3.0, 6.0)]
        spacing, regular = seg_geometry.slice_spacing(orientation, positions)
        self.assertAlmostEqual(spacing, 3.0)
        self.assertTrue(regular)

    def test_one_slice_cannot_be_measured(self):
        self.assertEqual(
            seg_geometry.slice_spacing(self.AXIAL, [[0, 0, 0]]), (None, None))


class TestPropertyContextGroups(unittest.TestCase):
    """Issue 15, against a stub of dcmterms' three tables: CID 7150 whose
    Anatomical Structure row points at CID 7192, which includes CID 7154."""

    GROUPS = {
        7150: {"name": "Segmentation Property Category", "includes": []},
        7151: {"name": "Segmentation Property Type", "includes": [7192, 7194]},
        7192: {"name": "Anatomical Structure Segmentation Property Type",
               "includes": [7154]},
        7194: {"name": "Morphologically Abnormal Structure Segmentation Property Type",
               "includes": [7159]},
        7154: {"name": "Abdominal Segmentation Type", "includes": []},
        7159: {"name": "Lesion Segmentation Type", "includes": []},
    }
    DIRECT = {
        7150: {("SCT", "91723000"), ("SCT", "49755003")},
        7192: {("SCT", "91806002")},
        7154: {("SCT", "10200004")},
        7159: {("SCT", "108369006")},
    }
    LINKS = {(7150, "SCT", "91723000"): 7192, (7150, "SCT", "49755003"): 7194}

    def check(self, category, type_code, category_scheme="SCT", type_scheme="SCT"):
        pairs = {(category_scheme, category, "cat", type_scheme, type_code, "type"):
                 {"segments": 1, "series": {"s"}}}
        return dcmterm.property_check(pairs, self.GROUPS, self.DIRECT, self.LINKS)[0]

    def test_membership_follows_includes_transitively(self):
        members = dcmterm.cid_members(7151, self.GROUPS, self.DIRECT)
        self.assertIn(("SCT", "10200004"), members)    # 7151 -> 7192 -> 7154
        self.assertIn(("SCT", "108369006"), members)   # 7151 -> 7194 -> 7159
        self.assertNotIn(("SCT", "91723000"), members)  # a category, not a type

    def test_a_cycle_in_the_includes_terminates(self):
        groups = {1: {"name": "a", "includes": [2]}, 2: {"name": "b", "includes": [1]}}
        self.assertEqual(dcmterm.cid_members(1, groups, {2: {("X", "1")}}), {("X", "1")})

    def test_liver_under_anatomical_structure_is_clean(self):
        row = self.check("91723000", "10200004")
        self.assertEqual(row["propertyIssue"], "")
        self.assertEqual(row["typeInCategoryCid"], "True")
        self.assertEqual(row["categoryTypeCid"], 7192)

    def test_a_lesion_type_under_anatomical_structure_contradicts_the_category(self):
        row = self.check("91723000", "108369006")
        self.assertEqual(row["propertyIssue"], "TYPE_OUTSIDE_CATEGORY")
        self.assertEqual(row["typeInCid7151"], "True")

    def test_an_unlisted_type_is_low_not_a_contradiction(self):
        row = self.check("91723000", "999999")
        self.assertEqual(row["propertyIssue"], "TYPE_NOT_IN_CID")

    def test_an_unlisted_category_is_reported(self):
        self.assertIn("CATEGORY_NOT_IN_CID", self.check("123037004", "10200004")["propertyIssue"])

    def test_private_scheme_is_not_judged(self):
        row = self.check("91723000", "WIDGET", type_scheme="99LOCAL")
        self.assertEqual(row["propertyIssue"], "")
        self.assertIn("private", row["note"])

    def test_retired_scheme_defers_to_issue_10(self):
        row = self.check("T-D0050", "10200004", category_scheme="SRT")
        self.assertEqual(row["propertyIssue"], "")
        self.assertIn("issue 10", row["note"])

    def test_tags_reach_the_triage_list(self):
        rows = rows_for([type_coded("Tumor", "108369006", "Neoplasm")])
        path = Path(__file__).resolve().parent / "_issue15.csv"
        with open(path, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=dcmterm.PROPERTY_COLUMNS)
            writer.writeheader()
            writer.writerow(self.check("91723000", "108369006"))
        self.addCleanup(path.unlink)
        properties = seg_checks.property_issues(path)
        out = seg_checks.triage(rows, {}, set(), {}, {}, [], properties=properties)
        self.assertIn("TYPE_OUTSIDE_CATEGORY", out[0]["issues"])
        self.assertEqual(out[0]["worstSeverity"], "Medium")


class TestDciodvfyBuildGuard(unittest.TestCase):
    """A dciodvfy build that rejects -allpffgitems validates nothing. That must
    never read as "no messages"."""

    USAGE = ("-allpffgitems: unrecognized option\n"
             "Usage: dciodvfy [-input-nolengthtoend] [-filename] [inputfile] <inputfile\n")

    def test_the_rejected_option_is_recognised(self):
        self.assertEqual(dciodvfy_check.unsupported_option(self.USAGE), "-allpffgitems")
        self.assertEqual(dciodvfy_check.unsupported_option(DCIODVFY_OUTPUT), "")

    def test_a_fake_old_build_is_a_failure_not_a_clean_object(self):
        fake = Path(__file__).resolve().parent / "_old_dciodvfy.sh"
        fake.write_text("#!/bin/sh\nprintf '%s\\n' '-allpffgitems: unrecognized option' "
                        "'Usage: dciodvfy [-filename] [inputfile]' >&2\n")
        fake.chmod(0o755)
        self.addCleanup(fake.unlink)
        text, failure = dciodvfy_check.run_one(str(fake), "x.dcm", 30)
        self.assertIsNone(text)
        self.assertIn("does not know -allpffgitems", failure)
        with self.assertRaises(SystemExit):
            dciodvfy_check.require_current_build(str(fake), "x.dcm", 30)

    def test_provenance_does_not_lend_the_pip_version_to_a_foreign_binary(self):
        line = dciodvfy_check.tool_provenance("/somewhere/else/dciodvfy")
        self.assertIn("build unknown", line)

    def test_segment_number_ordering_message_has_a_class(self):
        """dciodvfy sees the item order that a table cannot; its message is
        issue 16's, not OTHER. Text as a dicom3tools 20260901 build prints it."""
        self.assertEqual(
            dciodvfy_check.classify(
                "SegmentNumber not monotonically increasing from one by one - have "
                "SegmentSequence item number 2 with SegmentNumber 3"),
            "SEGMENT_NUMBER_NOT_SEQUENTIAL")


class TestMultiColumnCollection(unittest.TestCase):
    """lookup_codes.py and dcmterm.py coverage read all three sequences; a code
    used in two of them is one lookup that says where it was used."""

    def table(self):
        path = Path(__file__).resolve().parent / "_multi.csv"
        path.write_text(
            "SeriesInstanceUID,isBackgroundSegment,"
            "AnatomicRegionCodingSchemeDesignator,AnatomicRegionCodeValue,"
            "AnatomicRegionCodeMeaning,"
            "SegmentedPropertyTypeCodingSchemeDesignator,SegmentedPropertyTypeCodeValue,"
            "SegmentedPropertyTypeCodeMeaning\n"
            "s1,False,SCT,10200004,Liver,SCT,10200004,Liver\n"
            "s1,false,,,,SCT,78961009,Spleen\n"
            "s1,true,,,,SCT,10200004,Background\n")
        self.addCleanup(path.unlink)
        return path

    def test_lookup_collect_merges_across_sequences(self):
        codes = lookup_codes.collect(self.table(), lookup_codes.DEFAULT_COLUMNS)
        self.assertEqual(set(codes), {("SCT", "10200004"), ("SCT", "78961009")})
        self.assertEqual(codes[("SCT", "10200004")]["sequences"],
                         {"AnatomicRegion", "SegmentedPropertyType"})
        self.assertEqual(codes[("SCT", "10200004")]["segments"], 2)

    def test_dcmterm_batch_codes_records_sequences_and_skips_lowercase_background(self):
        codes = dcmterm.batch_codes(self.table(), dcmterm.DEFAULT_COLUMNS)
        self.assertEqual(codes[("SCT", "78961009")]["sequences"], {"SegmentedPropertyType"})
        self.assertNotIn("Background", codes[("SCT", "10200004")]["meanings"])

    def test_coverage_names_the_sequences_and_cids(self):
        codes = dcmterm.batch_codes(self.table(), dcmterm.DEFAULT_COLUMNS)
        cids = {("SCT", "10200004"): [(7154, "Abdominal Segmentation Type", "Liver")]}
        row = {r["CodeValue"]: r for r in dcmterm.coverage(codes, DCM, cids)}["10200004"]
        self.assertEqual(row["codeSequences"], "AnatomicRegion; SegmentedPropertyType")
        self.assertEqual(row["cids"], "7154")


class TestTerminologyServers(unittest.TestCase):
    """tx.fhir.org and OLS4 answer different questions, and lookup_codes.py must
    not let the fast one speak for the slow one.

    OLS4 serves SNOMED CT International as the inferred OWL, which carries ACTIVE
    concepts only, so a code it cannot find is usually retired - the one thing a
    reader would read "not found" as ruling out. No network here: the OLS4 payload
    is a real response for 110634007, trimmed to the fields the parser reads.
    """

    OLS4_HIT = {
        "_embedded": {"terms": [{
            "iri": "http://snomed.info/id/110634007",
            "label": "Right uterine adnexa",
            "annotation": {
                "alternative label": ["Structure of right uterine adnexa"],
                "preferred label": ["Right uterine adnexa"],
            },
            "synonyms": [],
            "is_obsolete": False,
            "obo_id": "SNOMED:110634007",
        }]}
    }

    def fake_urlopen(self, payload, status=None):
        """urlopen returning `payload`, or raising HTTPError(status)."""
        class Response:
            def __enter__(inner):
                return inner

            def __exit__(inner, *exc):
                return False

            def read(inner):
                return json.dumps(payload).encode()

        def opener(url, timeout=None):
            if status is not None:
                raise urllib.error.HTTPError(url, status, "no", {}, None)
            return Response()

        return opener

    def patch(self, name, value):
        original = getattr(lookup_codes, name)
        setattr(lookup_codes, name, value)
        self.addCleanup(setattr, lookup_codes, name, original)

    def test_ols4_returns_every_term_it_carries(self):
        """The FSN stem is among OLS4's terms but nothing marks which one it is -
        its `label` here is the preferred term, not the FSN."""
        with unittest.mock.patch.object(
                lookup_codes.urllib.request, "urlopen",
                self.fake_urlopen(self.OLS4_HIT)):
            found, label, terms = lookup_codes.lookup_ols4("110634007")
        self.assertTrue(found)
        self.assertEqual(label, "Right uterine adnexa")
        self.assertEqual(terms,
                         ("Right uterine adnexa", "Structure of right uterine adnexa"))

    def test_ols4_404_is_not_found_rather_than_an_error(self):
        with unittest.mock.patch.object(
                lookup_codes.urllib.request, "urlopen",
                self.fake_urlopen(None, status=404)):
            self.assertEqual(lookup_codes.lookup_ols4("99999999"), (False, "", ()))

    def test_ols4_miss_is_unresolved_never_missing(self):
        """The distinction the whole second source turns on: OLS4 holds no retired
        concept, so its silence is not evidence the code does not exist."""
        self.patch("lookup_ols4", lambda code, timeout=30: (False, "", ()))
        result = lookup_codes.resolve_ols4(["125074003"], workers=1)["125074003"]
        self.assertEqual(result["found"], "UNRESOLVED")
        self.assertEqual(result["active"], "")
        self.assertIn("retired", result["note"])

    def test_ols4_never_claims_a_code_is_active(self):
        self.patch("lookup_ols4",
                   lambda code, timeout=30: (True, "Liver structure", ("Liver structure",)))
        result = lookup_codes.resolve_ols4(["10200004"], workers=1)["10200004"]
        self.assertEqual(result["found"], "True")
        self.assertEqual(result["active"], "")
        self.assertEqual(result["source"], lookup_codes.OLS4_NAME)

    def test_fhir_separates_retired_from_nonexistent(self):
        answers = {
            "125074003": (True, "Hereford cattle superbreed (organism)", False),
            "99999999": (False, "", True),
            "10200004": (True, "Liver structure (body structure)", True),
        }
        self.patch("lookup_fhir",
                   lambda code, system=None, timeout=15: answers[code])
        results, fallback = lookup_codes.resolve_fhir(list(answers), delay=0)
        self.assertIsNone(fallback)
        self.assertEqual(results["125074003"]["status"], "retired")
        self.assertEqual(results["125074003"]["active"], "False")
        self.assertEqual(results["99999999"]["status"], "missing")
        # A code that does not exist has no activity status to report.
        self.assertEqual(results["99999999"]["active"], "")
        self.assertEqual(results["10200004"]["status"], "ok")

    def test_fhir_outage_finishes_on_ols4_and_says_so(self):
        """An outage should cost the `active` column, not the whole review - and
        every row it costs must name OLS4 as its source."""
        def down(code, system=None, timeout=15):
            raise urllib.error.URLError("connection refused")

        self.patch("lookup_fhir", down)
        self.patch("lookup_ols4",
                   lambda code, timeout=30: (True, f"term {code}", (f"term {code}",)))
        self.patch("time", _NoSleep())
        codes = [str(n) for n in range(6)]
        results, answered = lookup_codes.resolve_fhir(codes, delay=0)
        self.assertEqual(answered, 0)
        self.assertEqual(len(results), len(codes))
        self.assertEqual({r["source"] for r in results.values()},
                         {lookup_codes.OLS4_NAME})
        self.assertEqual({r["active"] for r in results.values()}, {""})

    def test_fallback_counts_what_tx_actually_answered(self):
        """The number in the warning is codes answered, not codes attempted - a
        reader uses it to decide how much of the run to repeat."""
        answers = {"a": (True, "A (body structure)", True)}

        def flaky(code, system=None, timeout=15):
            if code in answers:
                return answers[code]
            raise urllib.error.URLError("connection refused")

        self.patch("lookup_fhir", flaky)
        self.patch("lookup_ols4", lambda code, timeout=30: (True, code, (code,)))
        self.patch("time", _NoSleep())
        results, answered = lookup_codes.resolve_fhir(
            ["x", "a", "y", "z", "w"], delay=0)
        self.assertEqual(answered, 1)
        self.assertEqual(results["a"]["source"], lookup_codes.FHIR_NAME)
        # The isolated early failure is re-resolved, not left as an ERROR row.
        self.assertEqual(results["x"]["source"], lookup_codes.OLS4_NAME)
        self.assertEqual(results["x"]["found"], "True")

    def test_entire_flavour_reads_every_term(self):
        """On tx.fhir.org the term set is the one FSN; on OLS4 an "Entire X" can
        hide behind a synonymous label, so the test is over all of them."""
        self.assertTrue(lookup_codes.is_entire_flavour(("Entire colon (body structure)",)))
        self.assertTrue(lookup_codes.is_entire_flavour(("Colon", "Entire colon")))
        self.assertFalse(lookup_codes.is_entire_flavour(("Colon structure", "Colon")))
        self.assertFalse(lookup_codes.is_entire_flavour(()))

    def test_source_column_is_written_for_every_resolved_row(self):
        """`reviewSource` in the review table has to be defensible per code, which
        means codes.csv has to record which server answered."""
        self.assertIn("source", lookup_codes.COLUMNS)
        self.assertIn("terms", lookup_codes.COLUMNS)


class _NoSleep:
    """Stands in for the `time` module so the outage test does not wait."""

    def sleep(self, seconds):
        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
