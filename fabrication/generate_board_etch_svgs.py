"""Generate 1:1 SVG versions of the mirrored glass etch masks.

Run from the repository root with reportlab and fonttools installed. The SVGs
use black vector paths only; empty space is clear glass. On dark squares, the
letter outlines are true holes in the black square path.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from generate_board_etch_pdfs import (
    BOARD_MM,
    FONT_PATH,
    LABEL_INSET_MM,
    LAYOUTS,
    MM_TO_PT,
    PITCH_MM,
    OutlineFont,
)


OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output" / "svg"
LARGE_LABEL_CAP_HEIGHT_MM = 6.5


@dataclass(frozen=True)
class BorderSpec:
    suffix: str
    gap_mm: float
    width_mm: float


# The perimeter parking centers sit on the grid edge. The thin border stays
# outside their 18 mm footprints. Widen the other border around the thin
# border's original centerline, preserving its placement on the pane.
MINIMAL_BORDER = BorderSpec("minimal_border", 11.0, 0.75)
HALF_SQUARE_BORDER = BorderSpec(
    "half_square_border",
    MINIMAL_BORDER.gap_mm + (MINIMAL_BORDER.width_mm - PITCH_MM / 2) / 2,
    PITCH_MM / 2,
)


def number(value: float) -> str:
    """Enough precision for fabrication without bulky SVG path data."""
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def dark_square_path(x_mm: float, y_mm: float, page_x_mm: float, page_y_mm: float) -> str:
    left = page_x_mm - x_mm - PITCH_MM
    top = page_y_mm - y_mm - PITCH_MM
    right = left + PITCH_MM
    bottom = top + PITCH_MM
    return (
        f"M {number(left)} {number(top)} "
        f"L {number(right)} {number(top)} "
        f"L {number(right)} {number(bottom)} "
        f"L {number(left)} {number(bottom)} Z"
    )


def rectangle_path(left: float, top: float, width: float, height: float) -> str:
    right = left + width
    bottom = top + height
    return (
        f"M {number(left)} {number(top)} "
        f"L {number(right)} {number(top)} "
        f"L {number(right)} {number(bottom)} "
        f"L {number(left)} {number(bottom)} Z"
    )


def border_path(layout, border: BorderSpec) -> str:
    """A filled ring, so the shop sees an etched area rather than a cut stroke."""
    board_top = layout.page_y_mm - layout.board_y_mm - BOARD_MM
    inner_left = layout.board_x_mm - border.gap_mm
    inner_top = board_top - border.gap_mm
    inner_size = BOARD_MM + 2 * border.gap_mm
    outer_left = inner_left - border.width_mm
    outer_top = inner_top - border.width_mm
    outer_size = inner_size + 2 * border.width_mm
    if min(outer_left, outer_top) < 0 or outer_left + outer_size > layout.page_x_mm or outer_top + outer_size > layout.page_y_mm:
        raise ValueError("border extends beyond the glass pane")
    return (
        rectangle_path(outer_left, outer_top, outer_size, outer_size)
        + " "
        + rectangle_path(inner_left, inner_top, inner_size, inner_size)
    )


def label_path(
    font: OutlineFont,
    value: str,
    x_mm: float,
    y_mm: float,
    page_x_mm: float,
    page_y_mm: float,
    *,
    opposite_side: bool = False,
) -> str:
    commands = font.commands(value)
    all_points = [point for _, points in commands for point in points]
    min_x = min(x for x, _ in all_points)
    min_y = min(y for _, y in all_points)
    origin_x = x_mm + LABEL_INSET_MM
    origin_y = y_mm + LABEL_INSET_MM
    scale_mm = font.scale / MM_TO_PT

    def transform(point: tuple[float, float]) -> str:
        x, y = point
        source_x = origin_x + (x - min_x) * scale_mm
        source_y = origin_y + (y - min_y) * scale_mm
        if opposite_side:
            # The same coordinate near the diagonal corner reads upright to
            # the player seated at the opposite side of the board.
            source_x = 2 * x_mm + PITCH_MM - source_x
            source_y = 2 * y_mm + PITCH_MM - source_y
        svg_x = page_x_mm - source_x
        svg_y = page_y_mm - source_y
        return f"{number(svg_x)} {number(svg_y)}"

    parts = []
    for operation, points in commands:
        if operation == "moveTo":
            parts.append(f"M {transform(points[0])}")
        elif operation == "lineTo":
            parts.append(f"L {transform(points[0])}")
        elif operation == "curveTo":
            parts.append("C " + " ".join(transform(point) for point in points))
        elif operation in {"closePath", "endPath"}:
            parts.append("Z")
        else:
            raise ValueError(f"unexpected font outline operation: {operation}")
    return " ".join(parts)


def generate(
    layout,
    font: OutlineFont,
    *,
    border: BorderSpec | None = None,
    variant_suffix: str = "",
    both_sides_labels: bool = False,
) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix_parts = [part for part in (border.suffix if border else "", variant_suffix) if part]
    suffix = "_" + "_".join(suffix_parts) + ".svg" if suffix_parts else ".svg"
    destination = OUTPUT_DIR / layout.filename.replace(".pdf", suffix)
    width = number(layout.page_x_mm)
    height = number(layout.page_y_mm)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}mm" height="{height}mm" viewBox="0 0 {width} {height}">',
        "  <title>Chess table underside etch mask</title>",
        "  <desc>Full size. Black means frost; white or empty space means clear glass. Already mirrored for underside etching.</desc>",
    ]
    labels = set()
    dark_count = 0
    for rank in range(8):
        for file in range(8):
            square = f"{chr(ord('a') + file)}{rank + 1}"
            labels.add(square)
            x_mm = layout.board_x_mm + file * PITCH_MM
            y_mm = layout.board_y_mm + rank * PITCH_MM
            dark = (file + rank) % 2 == 0
            parts = []
            if dark:
                parts.append(dark_square_path(x_mm, y_mm, layout.page_x_mm, layout.page_y_mm))
                dark_count += 1
            parts.append(label_path(font, square, x_mm, y_mm, layout.page_x_mm, layout.page_y_mm))
            if both_sides_labels:
                parts.append(
                    label_path(
                        font, square, x_mm, y_mm, layout.page_x_mm, layout.page_y_mm,
                        opposite_side=True,
                    )
                )
            path = " ".join(parts)
            lines.append(
                f'  <path id="square-{square}" d="{path}" fill="#000000" fill-rule="evenodd" stroke="none"/>'
            )
    assert len(labels) == 64 and dark_count == 32
    if border:
        lines.append(
            f'  <path id="board-border" d="{border_path(layout, border)}" fill="#000000" fill-rule="evenodd" stroke="none"/>'
        )
    lines.append("</svg>")
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    border_choice = parser.add_mutually_exclusive_group()
    border_choice.add_argument("--full-pane-border", action="store_true", help="write only the full-pane mask with a minimal border")
    border_choice.add_argument("--full-pane-half-border", action="store_true", help="write only the full-pane mask with a half-square-width border")
    border_choice.add_argument("--full-pane-large-labels", action="store_true", help="write only the half-square-border full-pane mask with larger square labels")
    border_choice.add_argument("--full-pane-both-sides", action="store_true", help="write only the full-pane mask with large square labels readable from both sides")
    args = parser.parse_args()
    font = OutlineFont(FONT_PATH)
    if args.full_pane_both_sides:
        large_font = OutlineFont(FONT_PATH, cap_height_mm=LARGE_LABEL_CAP_HEIGHT_MM)
        print(
            generate(
                LAYOUTS[1], large_font, border=HALF_SQUARE_BORDER,
                variant_suffix="large_labels_both_sides", both_sides_labels=True,
            )
        )
    elif args.full_pane_large_labels:
        large_font = OutlineFont(FONT_PATH, cap_height_mm=LARGE_LABEL_CAP_HEIGHT_MM)
        print(generate(LAYOUTS[1], large_font, border=HALF_SQUARE_BORDER, variant_suffix="large_labels"))
    elif args.full_pane_half_border:
        print(generate(LAYOUTS[1], font, border=HALF_SQUARE_BORDER))
    elif args.full_pane_border:
        print(generate(LAYOUTS[1], font, border=MINIMAL_BORDER))
    else:
        for layout in LAYOUTS:
            print(generate(layout, font))


if __name__ == "__main__":
    main()
