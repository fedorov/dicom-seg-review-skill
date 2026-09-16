#!/usr/bin/env python3
"""CIELab as DICOM stores it: parse it, compare two of them, draw a swatch.

RecommendedDisplayCIELabValue (0062,000D) is three unsigned shorts, not a
colour name and not RGB. PS3.3 C.10.7.1.1 defines the scaling:

    L*    0x0000 -> 0.0     0xFFFF -> 100.0
    a*,b* 0x0000 -> -128.0  0x8080 -> 0.0    0xFFFF -> 127.0

Two conversions come out of that, and they are NOT equally trustworthy:

  * `delta_e` compares two stored colours in L*a*b* itself. It depends on
    nothing but the scaling above, so a "these two segments are the same
    colour" finding is exact.
  * `to_rgb` renders one for a human. That needs a white point, and the
    Standard's only hint is the note "this is the same form of encoding as
    used for the PCS in ICC Profiles" - the ICC PCS is D50, and PixelMed's
    ColorUtilities (which is what dcmqi and most SEG writers follow) converts
    sRGB D65 -> XYZ -> D50 -> Lab on the way in. This module inverts that:
    Lab D50 -> XYZ D50 -> Bradford -> D65 -> sRGB.

    Writers disagree at the margins - dcmqi has carried two implementations of
    its own, and DCMTK's IODCIELabUtil is a third - so treat a rendered swatch
    as an approximation of what the producer meant, never as evidence. Every
    finding in issue 13 is decided in Lab; only the preview goes through here.

Standard library only.

  python scripts/cielab.py 65535/32896/32896      # -> L*a*b*, sRGB, hex
"""

import sys

# ICC PCS white point (D50) and the sRGB one (D65), normalised to Y = 1.
D50 = (0.96422, 1.00000, 0.82521)

# Bradford chromatic adaptation, D50 -> D65 (Lindbloom).
BRADFORD_D50_TO_D65 = (
    (0.9555766, -0.0230393, 0.0631636),
    (-0.0282895, 1.0099416, 0.0210077),
    (0.0122982, -0.0204830, 1.3299098),
)

# XYZ (D65) -> linear sRGB.
XYZ_TO_SRGB = (
    (3.2404542, -1.5371385, -0.4985314),
    (-0.9692660, 1.8760108, 0.0415560),
    (0.0556434, -0.2040259, 1.0572252),
)

EPSILON = 216.0 / 24389.0
KAPPA = 24389.0 / 27.0

# Separators seen in the wild for the same VM-3 value: the per-segment table
# writes "l/a/b", a Healthcare API JSON export gives "[l, a, b]", pydicom's
# repr gives "[l, a, b]", and DICOM's own VM separator is a backslash.
_SEPARATORS = "/\\,"


def parse(value):
    """Three unsigned shorts out of however the triplet was written, or None.

    Returns None for an absent value rather than raising: absence is the
    normal case (the attribute is Type 3) and is a finding of its own, not an
    error. A value that is present but not three integers in range IS an
    error, and raises - that is a malformed attribute, not an absent one.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value]
    else:
        text = str(value).strip().strip("[]()")
        if not text:
            return None
        for separator in _SEPARATORS:
            if separator in text:
                parts = text.split(separator)
                break
        else:
            parts = text.split()
    parts = [p.strip() for p in parts if p.strip() != ""]
    if not parts:
        return None
    if len(parts) != 3:
        raise ValueError(f"CIELab value needs 3 components, got {len(parts)}: {value!r}")
    triplet = tuple(int(float(p)) for p in parts)
    for component in triplet:
        if not 0 <= component <= 0xFFFF:
            raise ValueError(f"CIELab component out of range for US: {value!r}")
    return triplet


def to_lab(triplet):
    """Stored unsigned shorts -> real L*a*b*, per PS3.3 C.10.7.1.1."""
    lightness, a_star, b_star = triplet
    return (
        lightness / 65535.0 * 100.0,
        a_star / 65535.0 * 255.0 - 128.0,
        b_star / 65535.0 * 255.0 - 128.0,
    )


def from_lab(lab):
    """Real L*a*b* -> stored unsigned shorts. The inverse of `to_lab`."""
    lightness, a_star, b_star = lab
    def clamp(value):
        return max(0, min(0xFFFF, int(round(value))))
    return (
        clamp(lightness / 100.0 * 65535.0),
        clamp((a_star + 128.0) / 255.0 * 65535.0),
        clamp((b_star + 128.0) / 255.0 * 65535.0),
    )


def _f_inverse(t):
    cube = t * t * t
    return cube if cube > EPSILON else (116.0 * t - 16.0) / KAPPA


def _apply(matrix, vector):
    return tuple(sum(row[i] * vector[i] for i in range(3)) for row in matrix)


def to_rgb(triplet):
    """Stored unsigned shorts -> 8-bit sRGB, for display only.

    See the module docstring: this leg assumes the stored Lab is D50-referred,
    as the ICC PCS is. It is a preview, not evidence.
    """
    lightness, a_star, b_star = to_lab(triplet)
    fy = (lightness + 16.0) / 116.0
    fx = fy + a_star / 500.0
    fz = fy - b_star / 200.0
    xyz_d50 = (
        D50[0] * _f_inverse(fx),
        D50[1] * (
            ((lightness + 16.0) / 116.0) ** 3
            if lightness > KAPPA * EPSILON
            else lightness / KAPPA
        ),
        D50[2] * _f_inverse(fz),
    )
    linear = _apply(XYZ_TO_SRGB, _apply(BRADFORD_D50_TO_D65, xyz_d50))

    channels = []
    for value in linear:
        value = max(0.0, min(1.0, value))
        encoded = (
            12.92 * value
            if value <= 0.0031308
            else 1.055 * value ** (1.0 / 2.4) - 0.055
        )
        channels.append(int(round(max(0.0, min(1.0, encoded)) * 255.0)))
    return tuple(channels)


def to_hex(triplet):
    """Stored unsigned shorts -> "#rrggbb"."""
    return "#{:02x}{:02x}{:02x}".format(*to_rgb(triplet))


def delta_e(first, second):
    """CIE76 dE*ab between two stored triplets, in L*a*b* units.

    CIE76 rather than CIEDE2000 on purpose: it is what the numbers support.
    The comparison is between two colours a producer CHOSE, to answer "can a
    reader tell these two segments apart", and at that granularity the extra
    machinery of CIEDE2000 buys nothing a threshold does not already absorb.
    It also keeps this module stdlib-only and its result easy to check by hand.

    Rough calibration: 2.3 is the classic just-noticeable difference for two
    large flat patches side by side. Segment overlays are neither large, flat,
    nor side by side - they are small, scattered and usually drawn at partial
    opacity over grey - so the threshold that matters in a viewer is well
    above that. seg_checks.py defaults to 10 and takes --color-delta-e.
    """
    lab_first = to_lab(first)
    lab_second = to_lab(second)
    return sum((a - b) ** 2 for a, b in zip(lab_first, lab_second)) ** 0.5


def swatch_svg(triplet, size=16):
    """A standalone SVG square of this colour.

    An SVG file, rather than an inline style or a data URI, because a Markdown
    report is read on GitHub as often as anywhere else and GitHub strips the
    `style` attribute and refuses `data:` image sources. A relative <img> to a
    small SVG committed beside the report survives that, and also renders in
    VS Code, in a static site, and in a PDF export.

    The stroke matters: without it a white or near-white swatch is invisible
    against the page, which is exactly the colour worth looking at.
    """
    colour = to_hex(triplet)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" '
        f'height="{size}" viewBox="0 0 {size} {size}" role="img" '
        f'aria-label="{colour}">'
        f'<rect x="0.5" y="0.5" width="{size - 1}" height="{size - 1}" rx="3" '
        f'fill="{colour}" stroke="#00000040"/></svg>\n'
    )


def describe(triplet):
    """One line: what is stored, what it means, what it looks like."""
    lightness, a_star, b_star = to_lab(triplet)
    red, green, blue = to_rgb(triplet)
    return (
        f"{'/'.join(str(v) for v in triplet)}  ->  "
        f"L*={lightness:.1f} a*={a_star:.1f} b*={b_star:.1f}  ->  "
        f"rgb({red},{green},{blue})  {to_hex(triplet)}"
    )


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__.strip())
    for argument in sys.argv[1:]:
        triplet = parse(argument)
        if triplet is None:
            print(f"{argument!r}: empty")
            continue
        print(describe(triplet))
    return 0


if __name__ == "__main__":
    sys.exit(main())
