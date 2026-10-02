"""A3 notebook PDF rendering with Arabic shaping and explicit mixed-script runs."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A3
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer

from config import FONT_DIR
from utils import is_arabic, prepare_for_reportlab, split_visual_runs


def _first_existing(*paths: str | Path) -> str | None:
    for path in paths:
        if Path(path).is_file():
            return str(path)
    return None


def _register_font(name: str, candidates: tuple[str | Path, ...]) -> bool:
    path = _first_existing(*candidates)
    if not path:
        return False
    try:
        pdfmetrics.registerFont(TTFont(name, path))
        return True
    except Exception:
        return False


_AR = _register_font("Amiri", (
    FONT_DIR / "Amiri-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
    "/usr/share/fonts/opentype/fonts-hosny-amiri/Amiri-Regular.ttf",
))
_AR_BOLD = _register_font("Amiri-Bold", (
    FONT_DIR / "Amiri-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
))
_EN = _register_font("Kalam", (
    FONT_DIR / "Kalam-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
))
_EN_BOLD = _register_font("Kalam-Bold", (
    FONT_DIR / "Kalam-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
))

if not (_AR and _EN):
    raise RuntimeError("No usable Arabic and Latin fonts are available")
pdfmetrics.registerFontFamily(
    "Amiri", normal="Amiri", bold="Amiri-Bold" if _AR_BOLD else "Amiri"
)
pdfmetrics.registerFontFamily(
    "Kalam", normal="Kalam", bold="Kalam-Bold" if _EN_BOLD else "Kalam"
)

PAGE_W, PAGE_H = A3
MARGIN = 65
LINE_HEIGHT = 25
INK_COLOR = colors.HexColor("#1a237e")
GOLD_COLOR = colors.HexColor("#c9a227")
GOLD_LIGHT = colors.HexColor("#e6c766")
INNER_FRAME_INSET = 48
HEADER_GAP = 24
DATE_RIGHT_X = PAGE_W - MARGIN - 45
DATE_BASELINE_Y = PAGE_H - INNER_FRAME_INSET - HEADER_GAP


def _draw_frame(canvas) -> None:
    canvas.setStrokeColor(GOLD_COLOR)
    canvas.setLineWidth(6)
    canvas.rect(30, 30, PAGE_W - 60, PAGE_H - 60)
    canvas.setLineWidth(2)
    canvas.rect(48, 48, PAGE_W - 96, PAGE_H - 96)
    canvas.setStrokeColor(GOLD_LIGHT)
    canvas.setLineWidth(3)
    corner = 35
    for x, y, dx, dy in (
        (60, 60, 1, 1), (PAGE_W - 60, 60, -1, 1),
        (60, PAGE_H - 60, 1, -1), (PAGE_W - 60, PAGE_H - 60, -1, -1),
    ):
        canvas.line(x, y, x + corner * dx, y)
        canvas.line(x, y, x, y + corner * dy)
        canvas.setFillColor(GOLD_COLOR)
        canvas.circle(x + 10 * dx, y + 10 * dy, 5, fill=1)


def _draw_footer(canvas, doc) -> None:
    canvas.saveState()
    _draw_frame(canvas)
    canvas.setFont("Kalam", 18)
    canvas.setFillColor(GOLD_COLOR)
    canvas.drawRightString(
        DATE_RIGHT_X, DATE_BASELINE_Y,
        datetime.now(timezone.utc).strftime("%Y/%m/%d"),
    )
    canvas.setFont("Kalam", 16)
    canvas.drawCentredString(PAGE_W / 2, 55, f"- {canvas.getPageNumber()} -")
    canvas.restoreState()


class ArabicFlowable(Flowable):
    """Visual-order Arabic line flowable with an explicit Latin font fallback."""

    def __init__(self, text: str, style: ParagraphStyle, max_width: float,
                 prepared_lines: list[str] | None = None,
                 space_before: float | None = None,
                 space_after: float | None = None):
        super().__init__()
        self.style = style
        self.max_width = max_width
        self.lines = prepared_lines if prepared_lines is not None else prepare_for_reportlab(
            text, max_width, style.fontName, style.fontSize
        ).split("\n")
        self.leading = style.leading
        self.spaceBefore = style.spaceBefore if space_before is None else space_before
        self.spaceAfter = style.spaceAfter if space_after is None else space_after

    def wrap(self, availWidth, availHeight):
        self.width = min(availWidth, self.max_width)
        self.height = len(self.lines) * self.leading + self.spaceBefore + self.spaceAfter
        return self.width, self.height

    def split(self, availWidth, availHeight):
        """Split already-shaped visual lines so large paragraphs span pages."""
        capacity = int(max(0, availHeight - self.spaceBefore - self.spaceAfter)
                       // self.leading)
        if capacity <= 0 or capacity >= len(self.lines):
            return []
        first = ArabicFlowable(
            "", self.style, self.max_width,
            prepared_lines=self.lines[:capacity],
            space_before=self.spaceBefore, space_after=0,
        )
        remainder = ArabicFlowable(
            "", self.style, self.max_width,
            prepared_lines=self.lines[capacity:],
            space_before=0, space_after=self.spaceAfter,
        )
        return [first, remainder]

    def draw(self):
        canvas = self.canv
        canvas.saveState()
        y = self.height - self.spaceBefore - self.style.fontSize
        for line in self.lines:
            measured = []
            total_width = 0.0
            for kind, run in split_visual_runs(line):
                font = self.style.fontName if kind == "ar" else "Kalam"
                width = pdfmetrics.stringWidth(run, font, self.style.fontSize)
                measured.append((font, run, width))
                total_width += width
            x = self.width - total_width
            canvas.setFillColor(self.style.textColor)
            for font, run, width in measured:
                canvas.setFont(font, self.style.fontSize)
                canvas.drawString(x, y, run)
                x += width
            y -= self.leading
        canvas.restoreState()


def create_pdf(text: str, output: str | Path) -> str:
    """Create a PDF at ``output`` and return its path; input is never logged."""
    output = str(output)
    doc = SimpleDocTemplate(
        output, pagesize=A3, leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=130, bottomMargin=90,
    )
    body_ar = ParagraphStyle(
        "AR", fontName="Amiri", fontSize=20, leading=LINE_HEIGHT,
        textColor=INK_COLOR, alignment=TA_RIGHT, spaceAfter=6,
    )
    body_en = ParagraphStyle(
        "EN", fontName="Kalam", fontSize=20, leading=LINE_HEIGHT,
        textColor=INK_COLOR, alignment=TA_LEFT, spaceAfter=6,
    )
    title_ar = ParagraphStyle(
        "TitleAR", fontName="Amiri-Bold" if _AR_BOLD else "Amiri",
        fontSize=32, leading=44.8, textColor=INK_COLOR,
        alignment=TA_RIGHT, spaceBefore=12, spaceAfter=14,
    )
    title_en = ParagraphStyle(
        "TitleEN", fontName="Kalam-Bold" if _EN_BOLD else "Kalam",
        fontSize=34, leading=47.6, textColor=INK_COLOR,
        alignment=TA_LEFT, spaceBefore=12, spaceAfter=14,
    )
    story = []
    for raw_para in text.splitlines():
        if not raw_para.strip():
            story.append(Spacer(1, LINE_HEIGHT * 0.5))
            continue
        para = raw_para.strip()
        is_title = para.startswith("#")
        if is_title:
            para = para.lstrip("#").strip()
        arabic = is_arabic(para)
        style = (title_ar if arabic else title_en) if is_title else (body_ar if arabic else body_en)
        if arabic:
            story.append(ArabicFlowable(para, style, doc.width))
        else:
            escaped = para.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            story.append(Paragraph(escaped, style))
    doc.build(story, onFirstPage=_draw_footer, onLaterPages=_draw_footer)
    return output
