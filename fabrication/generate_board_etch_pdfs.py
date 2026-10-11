"""Generate full-size, mirrored underside etch masks for the glass chess table.

The output PDFs contain only pure black vector paths: black means etch/frost,
and white means leave the glass clear. Lettering on dark squares is a real
knockout in each filled square, rather than a white object over a black fill.

Requires reportlab and fonttools. Run from the repository root with the Codex
workspace Python (or another environment with those packages installed).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.qu2cuPen import Qu2CuPen
from fontTools.pens.recordingPen import DecomposingRecordingPen, RecordingPen
from fontTools.ttLib import TTFont
from reportlab.pdfgen import canvas
import reportlab


MM_TO_PT = 72.0 / 25.4
PITCH_MM = 31.25
BOARD_MM = 8 * PITCH_MM
PANE_X_MM = 426.0  # files a-h / plotter X
PANE_Y_MM = 300.0  # ranks 1-8 / plotter Y
LABEL_INSET_MM = 2.4
LABEL_CAP_HEIGHT_MM = 4.5
FONT_PATH = Path(reportlab.__file__).parent / "fonts" / "VeraBd.ttf"
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output" / "pdf"


@dataclass(frozen=True)
class Layout:
    filename: str
    page_x_mm: float
    page_y_mm: float
    board_x_mm: float
    board_y_mm: float


LAYOUTS = (
    Layout("chess_board_250mm_underside_etch.pdf", BOARD_MM, BOARD_MM, 0.0, 0.0),
    Layout(
        "chess_pane_426x300mm_underside_etch.pdf",
        PANE_X_MM,
        PANE_Y_MM,
        (PANE_X_MM - BOARD_MM) / 2,
        (PANE_Y_MM - BOARD_MM) / 2,
    ),
)


class OutlineFont:
    def __init__(self, path: Path, cap_height_mm: float = LABEL_CAP_HEIGHT_MM) -> None:
        self.font = TTFont(path)
        self.glyph_set = self.font.getGlyphSet()
        self.cmap = self.font.getBestCmap()
        self.advance = self.font["hmtx"].metrics
        cap_height = getattr(self.font["OS/2"], "sCapHeight", None)
        if not cap_height:
            bounds = BoundsPen(self.glyph_set)
            self.glyph_set[self.cmap[ord("H")]].draw(bounds)
            cap_height = bounds.bounds[3] - bounds.bounds[1]
        self.scale = cap_height_mm * MM_TO_PT / cap_height

    def commands(self, value: str) -> list[tuple[str, tuple[tuple[float, float], ...]]]:
        """Return flattened cubic outline commands, positioned in font units."""
        positioned = []
        cursor = 0.0
        for character in value:
            glyph_name = self.cmap[ord(character)]
            decomposed = DecomposingRecordingPen(self.glyph_set)
            self.glyph_set[glyph_name].draw(decomposed)
            cubic = RecordingPen()
            decomposed.replay(Qu2CuPen(cubic, max_err=1.0, all_cubic=True))
            for operation, points in cubic.value:
                if operation not in {"moveTo", "lineTo", "curveTo", "closePath", "endPath"}:
                    raise ValueError(f"unexpected font outline operation: {operation}")
                positioned.append((operation, tuple((x + cursor, y) for x, y in points)))
            cursor += self.advance[glyph_name][0]
        return positioned

    def append_label(self, pdf_path, value: str, x_mm: float, y_mm: float) -> None:
        commands = self.commands(value)
        all_points = [point for _, points in commands for point in points]
        min_x = min(x for x, _ in all_points)
        min_y = min(y for _, y in all_points)
        max_x = max(x for x, _ in all_points)
        max_y = max(y for _, y in all_points)
        if (max_x - min_x) * self.scale > 12 * MM_TO_PT:
            raise ValueError(f"label {value} is unexpectedly wide")
        if (max_y - min_y) * self.scale > 8 * MM_TO_PT:
            raise ValueError(f"label {value} is unexpectedly tall")

        origin_x = (x_mm + LABEL_INSET_MM) * MM_TO_PT
        origin_y = (y_mm + LABEL_INSET_MM) * MM_TO_PT

        def transform(point: tuple[float, float]) -> tuple[float, float]:
            x, y = point
            return origin_x + (x - min_x) * self.scale, origin_y + (y - min_y) * self.scale

        for operation, points in commands:
            if operation == "moveTo":
                pdf_path.moveTo(*transform(points[0]))
            elif operation == "lineTo":
                pdf_path.lineTo(*transform(points[0]))
            elif operation == "curveTo":
                pdf_path.curveTo(*(coordinate for point in points for coordinate in transform(point)))
            else:
                pdf_path.close()


def generate(layout: Layout, font: OutlineFont) -> Path:
    if layout.page_x_mm < BOARD_MM or layout.page_y_mm < BOARD_MM:
        raise ValueError("page cannot be smaller than the playing grid")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    destination = OUTPUT_DIR / layout.filename
    pdf = canvas.Canvas(
        str(destination),
        pagesize=(layout.page_x_mm * MM_TO_PT, layout.page_y_mm * MM_TO_PT),
        pageCompression=1,
        pdfVersion=(1, 4),
    )
    pdf.setTitle(f"Chess table underside etch mask - {layout.page_x_mm:g} x {layout.page_y_mm:g} mm")
    pdf.setAuthor("Chess table project")
    pdf.setSubject("1:1 vector etch mask; black=frost; underside mirrored once")
    pdf.setFillGray(0)

    # Reflect all geometry, including the glyph outlines, for viewing through
    # the smooth top surface. The shop should not mirror this artwork again.
    pdf.translate(layout.page_x_mm * MM_TO_PT, 0)
    pdf.scale(-1, 1)

    labels = set()
    dark_count = 0
    for rank in range(8):
        for file in range(8):
            square = f"{chr(ord('a') + file)}{rank + 1}"
            labels.add(square)
            x_mm = layout.board_x_mm + file * PITCH_MM
            y_mm = layout.board_y_mm + rank * PITCH_MM
            dark = (file + rank) % 2 == 0  # a1 and h8 are dark
            path = pdf.beginPath()
            if dark:
                path.rect(x_mm * MM_TO_PT, y_mm * MM_TO_PT, PITCH_MM * MM_TO_PT, PITCH_MM * MM_TO_PT)
                dark_count += 1
            font.append_label(path, square, x_mm, y_mm)
            pdf.drawPath(path, stroke=0, fill=1, fillMode=0)  # even-odd knockout

    assert len(labels) == 64 and dark_count == 32
    pdf.showPage()
    pdf.save()
    return destination


def main() -> None:
    font = OutlineFont(FONT_PATH)
    for layout in LAYOUTS:
        print(generate(layout, font))


if __name__ == "__main__":
    main()
