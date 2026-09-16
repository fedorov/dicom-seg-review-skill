#!/usr/bin/env python3
"""Write a small, deliberately imperfect SEG delivery to smoke-test the review.

Nothing here is real data, and nothing here is a fixture the tests need - the
suite builds its datasets in memory. This exists so that an installation can
be checked end to end, and so that every output the skill produces can be
seen once on a delivery whose defects are known in advance.

  python scripts/make_fixture.py /tmp/seg-fixture
  python scripts/seg_attributes.py --files /tmp/seg-fixture --resolve-referenced \\
      -o /tmp/seg-fixture/seg_attributes.csv
  python scripts/seg_checks.py /tmp/seg-fixture/seg_attributes.csv --outdir /tmp/seg-fixture/findings

Five files, chosen so that most checks have one thing to find and one to pass:

  seg_liver.dcm       BINARY, two segments coded in the TYPE sequence with no
                      AnatomicRegionSequence (the common, conformant shape);
                      Rows x Columns not a multiple of 8 (issue 11's
                      bit-packing trap); one all-zero frame kept (issue 11);
                      both segments the same recommended colour (issue 13);
                      AUTOMATIC with a bare name and no identification
                      sequence (issue 14); uncompressed (issue 12). Its Frame
                      of Reference matches ct_a.dcm (issue 17 passes).
  seg_bowel.dcm       the same type code with CodeMeaning "Large bowel"
                      (issue 2); category Anatomical Structure over a Neoplasm
                      type (issue 15); segments numbered 1 and 3 (issue 16);
                      SRT designator on the category (issue 10); Frame of
                      Reference differing from ct_b.dcm (issue 17); and a 2 mm
                      grid over a 1 mm CT (issue 18 - conformant, documented).
  seg_labelmap.dcm    ... and no segmented series IDC or the directory holds,
                      so issue 18 reports the absence too.
  seg_labelmap.dcm    LABELMAP with a Background segment 0, referencing a
                      series that is not in the directory (issue 17, dangling).
  ct_a.dcm, ct_b.dcm  one slice each of the two referenced CT series, so
                      --resolve-referenced has something to find.

Needs pydicom. Nothing else.
"""

import argparse
import sys
from pathlib import Path

try:
    import pydicom
    from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
    from pydicom.sequence import Sequence
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid
except ImportError as exc:  # pragma: no cover - environment dependent
    sys.exit(f"make_fixture.py needs pydicom:\n  pip install 'pydicom>=3.0'\n({exc})")

ROOT = "1.2.826.0.1.3680043.8.498.99"   # a fixed prefix so the UIDs are recognisable
# UIDs are numeric; the names below become the object number in each UID.
OBJECTS = {"ct_a": 1, "ct_b": 2, "ct_c": 3, "seg_liver": 11, "seg_bowel": 12,
           "seg_labelmap": 13}
CT_IMAGE = "1.2.840.10008.5.1.4.1.1.2"
SEGMENTATION = "1.2.840.10008.5.1.4.1.1.66.4"
LABELMAP_SEGMENTATION = "1.2.840.10008.5.1.4.1.1.66.7"
ROWS, COLUMNS, SLICES = 10, 10, 3   # 100 bits per frame: frames start mid-byte

PATIENT = {"PatientID": "FIXTURE-001", "PatientName": "Fixture^Segmentation",
           "PatientBirthDate": "", "PatientSex": ""}


def uid(suffix):
    """`suffix` is dotted numbers, with object names replaced by their number."""
    parts = [str(OBJECTS.get(part, part)) for part in str(suffix).split(".")]
    return f"{ROOT}.{'.'.join(parts)}"


def code(value, meaning, scheme="SCT"):
    item = Dataset()
    item.CodeValue = value
    item.CodingSchemeDesignator = scheme
    item.CodeMeaning = meaning
    return item


def file_meta(sop_class, sop_instance):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = sop_class
    meta.MediaStorageSOPInstanceUID = sop_instance
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = uid("0.1")
    meta.ImplementationVersionName = "SEG-REVIEW-FIX"   # SH: 16 characters at most
    return meta


def common(ds, sop_class, sop_instance, study, series, modality, description,
           frame_of_reference):
    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = sop_instance
    for keyword, value in PATIENT.items():
        setattr(ds, keyword, value)
    ds.StudyInstanceUID = study
    ds.StudyID = "1"
    ds.StudyDate = "20260101"
    ds.StudyTime = "120000"
    ds.AccessionNumber = ""
    ds.ReferringPhysicianName = ""
    ds.SeriesInstanceUID = series
    ds.SeriesNumber = 1
    ds.SeriesDescription = description
    ds.Modality = modality
    ds.FrameOfReferenceUID = frame_of_reference
    ds.PositionReferenceIndicator = ""
    ds.Manufacturer = "dicom-seg-review fixture"
    ds.ManufacturerModelName = "make_fixture.py"
    ds.SoftwareVersions = "1.4.0"
    ds.DeviceSerialNumber = "0"
    ds.InstanceNumber = 1
    ds.ContentDate = "20260101"
    ds.ContentTime = "120000"
    ds.PixelSpacing = [1.0, 1.0]
    ds.SliceThickness = 1.0
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.Rows = ROWS
    ds.Columns = COLUMNS
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"


def write_ct(directory, name, study, series, frame_of_reference):
    """One CT slice, enough for --resolve-referenced to find the series."""
    sop_instance = uid(f"1.{name}")
    ds = FileDataset(str(directory / f"{name}.dcm"), {}, file_meta=file_meta(CT_IMAGE, sop_instance),
                     preamble=b"\0" * 128)
    common(ds, CT_IMAGE, sop_instance, study, series, "CT", f"Fixture CT {name}",
           frame_of_reference)
    ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
    ds.ImagePositionPatient = [0, 0, 0]
    ds.KVP = 120
    ds.BitsAllocated = 16
    ds.BitsStored = 12
    ds.HighBit = 11
    ds.PixelRepresentation = 0
    ds.RescaleIntercept = -1024
    ds.RescaleSlope = 1
    ds.PixelData = bytes(ROWS * COLUMNS * 2)
    ds.save_as(str(directory / f"{name}.dcm"), enforce_file_format=True)
    return sop_instance


def pack_bits(frames):
    """BINARY pixel data: frames concatenated bit by bit, LSB first, no padding
    between frames - PS3.5 8.1, which is what makes byte-slicing wrong."""
    bits = [bit for frame in frames for bit in frame]
    bits += [0] * (-len(bits) % 8)
    out = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for j, bit in enumerate(bits[i:i + 8]):
            byte |= (bit & 1) << j
        out.append(byte)
    return bytes(out)


def disc(cx, cy, radius):
    """A filled disc as a row-major list of bits."""
    return [1 if (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2 else 0
            for y in range(ROWS) for x in range(COLUMNS)]


def segment_item(number, label, category, seg_type, algorithm_type="MANUAL",
                 algorithm_name=None, color=None):
    item = Dataset()
    item.SegmentNumber = number
    item.SegmentLabel = label
    item.SegmentAlgorithmType = algorithm_type
    if algorithm_name:
        item.SegmentAlgorithmName = algorithm_name
    item.SegmentedPropertyCategoryCodeSequence = Sequence([category])
    item.SegmentedPropertyTypeCodeSequence = Sequence([seg_type])
    if color:
        item.RecommendedDisplayCIELabValue = list(color)
    return item


def functional_groups(ds, frames, referenced_ct, referenced_ct_class=CT_IMAGE,
                      spacing=1.0):
    """Shared and per-frame groups for `frames`, a list of (segment number, slice index).

    `spacing` sets the in-plane pixel spacing, the slice thickness and the
    spacing between slices together. The CT slices are written at 1.0, so
    anything else is a segmentation sampled on a grid of its own - issue 18,
    which is a documented property and not a defect.
    """
    shared = Dataset()
    measures = Dataset()
    measures.PixelSpacing = [spacing, spacing]
    measures.SliceThickness = spacing
    measures.SpacingBetweenSlices = spacing
    shared.PixelMeasuresSequence = Sequence([measures])
    orientation = Dataset()
    orientation.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    shared.PlaneOrientationSequence = Sequence([orientation])
    ds.SharedFunctionalGroupsSequence = Sequence([shared])

    organization = Dataset()
    organization.DimensionOrganizationUID = uid(f"3.{ds.SOPInstanceUID[-6:]}")
    ds.DimensionOrganizationSequence = Sequence([organization])
    by_segment = Dataset()
    by_segment.DimensionOrganizationUID = organization.DimensionOrganizationUID
    by_segment.DimensionIndexPointer = 0x0062000B          # ReferencedSegmentNumber
    by_segment.FunctionalGroupPointer = 0x0062000A         # SegmentIdentificationSequence
    by_segment.DimensionDescriptionLabel = "Segment"
    by_position = Dataset()
    by_position.DimensionOrganizationUID = organization.DimensionOrganizationUID
    by_position.DimensionIndexPointer = 0x00200032         # ImagePositionPatient
    by_position.FunctionalGroupPointer = 0x00209113        # PlanePositionSequence
    by_position.DimensionDescriptionLabel = "Position"
    ds.DimensionIndexSequence = Sequence([by_segment, by_position])

    per_frame = []
    for segment_number, slice_index in frames:
        item = Dataset()
        content = Dataset()
        content.DimensionIndexValues = [segment_number, slice_index + 1]
        item.FrameContentSequence = Sequence([content])
        position = Dataset()
        position.ImagePositionPatient = [0, 0, float(slice_index)]
        item.PlanePositionSequence = Sequence([position])
        identification = Dataset()
        identification.ReferencedSegmentNumber = segment_number
        item.SegmentIdentificationSequence = Sequence([identification])
        derivation = Dataset()
        derivation.DerivationCodeSequence = Sequence([code("113076", "Segmentation", "DCM")])
        source = Dataset()
        source.ReferencedSOPClassUID = referenced_ct_class
        source.ReferencedSOPInstanceUID = referenced_ct
        source.PurposeOfReferenceCodeSequence = Sequence(
            [code("121322", "Source image for image processing operation", "DCM")])
        derivation.SourceImageSequence = Sequence([source])
        item.DerivationImageSequence = Sequence([derivation])
        per_frame.append(item)
    ds.PerFrameFunctionalGroupsSequence = Sequence(per_frame)
    ds.NumberOfFrames = len(frames)


def referenced_series(ds, series, referenced_ct, referenced_ct_class=CT_IMAGE):
    ref_series = Dataset()
    ref_series.SeriesInstanceUID = series
    ref_instance = Dataset()
    ref_instance.ReferencedSOPClassUID = referenced_ct_class
    ref_instance.ReferencedSOPInstanceUID = referenced_ct
    ref_series.ReferencedInstanceSequence = Sequence([ref_instance])
    ds.ReferencedSeriesSequence = Sequence([ref_series])


def new_seg(directory, name, sop_class, study, frame_of_reference, description):
    sop_instance = uid(f"2.{name}")
    ds = FileDataset(str(directory / f"{name}.dcm"), {},
                     file_meta=file_meta(sop_class, sop_instance), preamble=b"\0" * 128)
    common(ds, sop_class, sop_instance, study, uid(f"4.{name}"), "SEG", description,
           frame_of_reference)
    ds.ImageType = ["DERIVED", "PRIMARY"]
    ds.ContentLabel = name.upper()
    ds.ContentDescription = description
    ds.ContentCreatorName = "Fixture"
    ds.LossyImageCompression = "00"
    return ds


def write_binary_seg(directory, name, study, frame_of_reference, ct_series, ct_instance,
                     segments, frames_per_segment, description, colour=None,
                     algorithm_type="MANUAL", algorithm_name=None, overlap="NO",
                     spacing=1.0):
    """`segments` is a list of (number, label, category, type) where category and
    type are (value, meaning) or (value, meaning, scheme) tuples."""
    ds = new_seg(directory, name, SEGMENTATION, study, frame_of_reference, description)
    ds.SegmentationType = "BINARY"
    ds.SegmentsOverlap = overlap
    ds.BitsAllocated = 1
    ds.BitsStored = 1
    ds.HighBit = 0
    ds.PixelRepresentation = 0
    ds.SegmentSequence = Sequence([
        segment_item(number, label, code(*category), code(*seg_type),
                     algorithm_type, algorithm_name, colour)
        for number, label, category, seg_type in segments])
    frames, bits = [], []
    for number, _, _, _ in segments:
        for slice_index, frame in enumerate(frames_per_segment[number]):
            frames.append((number, slice_index))
            bits.append(frame)
    functional_groups(ds, frames, ct_instance, spacing=spacing)
    referenced_series(ds, ct_series, ct_instance)
    ds.PixelData = pack_bits(bits)
    ds.save_as(str(directory / f"{name}.dcm"), enforce_file_format=True)


def write_labelmap_seg(directory, name, study, frame_of_reference, ct_series, ct_instance):
    ds = new_seg(directory, name, LABELMAP_SEGMENTATION, study, frame_of_reference,
                 "Fixture labelmap")
    ds.SegmentationType = "LABELMAP"
    ds.SegmentsOverlap = "NO"
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelPaddingValue = 0
    anatomical = ("91723000", "Anatomical Structure")
    ds.SegmentSequence = Sequence([
        segment_item(0, "Background", code(*anatomical), code("125012", "Background", "DCM")),
        segment_item(1, "Spleen", code(*anatomical), code("78961009", "Spleen"),
                     "AUTOMATIC", "TotalSegmentator", (33000, 38000, 28000)),
        segment_item(2, "Kidney", code(*anatomical), code("64033007", "Kidney"),
                     "AUTOMATIC", "TotalSegmentator", (40000, 30000, 36000)),
    ])
    frames = [(1, slice_index) for slice_index in range(SLICES)]
    functional_groups(ds, frames, ct_instance)
    # One frame per slice; the Segment Identification of a LABELMAP frame is
    # not a single segment, so drop it and leave the dimension by position.
    for item in ds.PerFrameFunctionalGroupsSequence:
        del item.SegmentIdentificationSequence
        item.FrameContentSequence[0].DimensionIndexValues = [
            item.FrameContentSequence[0].DimensionIndexValues[1]]
    ds.DimensionIndexSequence = Sequence([ds.DimensionIndexSequence[1]])
    referenced_series(ds, ct_series, ct_instance)
    pixels = bytearray()
    for slice_index in range(SLICES):
        spleen = disc(3, 5, 2)
        kidney = disc(7, 5, 1)
        pixels += bytes(1 if s else (2 if k else 0) for s, k in zip(spleen, kidney))
    ds.PixelData = bytes(pixels)
    ds.save_as(str(directory / f"{name}.dcm"), enforce_file_format=True)


def main():
    parser = argparse.ArgumentParser(description="Write a small synthetic SEG delivery.")
    parser.add_argument("directory", help="where to write the files (created if missing)")
    args = parser.parse_args()
    directory = Path(args.directory)
    directory.mkdir(parents=True, exist_ok=True)

    study = uid("5.1")
    frame_a, frame_b = uid("6.1"), uid("6.2")
    ct_a_series, ct_b_series = uid("7.1"), uid("7.2")
    ct_a = write_ct(directory, "ct_a", study, ct_a_series, frame_a)
    ct_b = write_ct(directory, "ct_b", study, ct_b_series, frame_b)

    anatomical = ("91723000", "Anatomical Structure")
    red = (35580, 53665, 50856)   # rgb(255, 0, 0) - the same colour on both segments

    # Liver and portal vein, both typed, no region. The vein's last frame is
    # deliberately empty and kept.
    write_binary_seg(
        directory, "seg_liver", study, frame_a, ct_a_series, ct_a,
        segments=[(1, "Liver", anatomical, ("10200004", "Liver")),
                  (2, "Portal vein", anatomical, ("32764006", "Portal vein"))],
        frames_per_segment={1: [disc(4, 4, 3) for _ in range(SLICES)],
                            2: [disc(6, 6, 1), disc(6, 6, 1), [0] * (ROWS * COLUMNS)]},
        description="Fixture liver, typed anatomy, no region",
        colour=red, algorithm_type="AUTOMATIC", algorithm_name="TotalSegmentator")

    # The same code as "Large bowel"; a lesion type under Anatomical Structure;
    # numbers 1 and 3; SRT on one category; Frame of Reference not ct_b's.
    write_binary_seg(
        directory, "seg_bowel", study, uid("6.9"), ct_b_series, ct_b,
        segments=[(1, "Large bowel", ("T-D0050", "Tissue", "SRT"),
                   ("10200004", "Large bowel")),
                  (3, "Tumor", anatomical, ("108369006", "Neoplasm"))],
        frames_per_segment={1: [disc(5, 5, 4) for _ in range(SLICES)],
                            3: [disc(5, 5, 1) for _ in range(SLICES)]},
        description="Fixture bowel: ambiguous code, category/type contradiction",
        overlap="UNDEFINED", spacing=2.0)

    # A labelmap referencing a series that is not here.
    write_labelmap_seg(directory, "seg_labelmap", study, frame_a, uid("7.ct_c"), uid("1.ct_c"))

    for path in sorted(directory.glob("*.dcm")):
        ds = pydicom.dcmread(str(path), stop_before_pixels=True)
        print(f"  {path.name:<18} {ds.Modality}  {getattr(ds, 'SegmentationType', ''):<8} "
              f"{len(getattr(ds, 'SegmentSequence', [])):>2} segments  "
              f"{getattr(ds, 'NumberOfFrames', ''):>3} frames", file=sys.stderr)
    print(f"\nWrote the fixture to {directory}. Next:\n"
          f"  python scripts/seg_attributes.py --files {directory} --resolve-referenced "
          f"-o {directory}/seg_attributes.csv\n"
          f"  python scripts/seg_checks.py {directory}/seg_attributes.csv "
          f"--outdir {directory}/findings", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
