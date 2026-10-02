"""أدوات تنظيف ومعالجة النص العربي والإنجليزي والمختلط.

هناك مساران مختلفان عمدًا:

* ``prepare_for_reportlab`` يلف النص المنطقي ثم يعيده كسطور مرئية للرسم
  المباشر؛ لا يُمرر الناتج إلى Paragraph.
* ``fix_mixed_text`` مخصص للنص الذي سيُرسم مباشرة على canvas، ولذلك يطبق
  Bidi بنفسه.
"""
from __future__ import annotations

import re
import unicodedata
from reportlab.pdfbase.pdfmetrics import stringWidth

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    _ARABIC_OK = True
except ImportError:  # يسمح للمشروع بالعمل مع تنظيف محافظ عند غياب التبعيات
    arabic_reshaper = None
    get_display = None
    _ARABIC_OK = False

TATWEEL = "\u0640"
_LRM = "\u200e"

# عزل runs التي يجب أن تبقى LTR عند الرسم المباشر.  لا نستخدم هذا التعبير
# في مسار Paragraph لأن عزلها يدويًا هناك يسبب معالجة مزدوجة.
_LTR_RUN_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"((?:https?://[^\s)\]]+"
    r"|\d+(?:[.,/-]\d+)*(?:%)?"
    r"|[A-Za-z0-9]+(?:[._:/?&=#%+@-][A-Za-z0-9]+)*))"
    r"(?![A-Za-z0-9])"
)


def _is_arabic_char(ch: str) -> bool:
    cp = ord(ch)
    return (
        (0x0600 <= cp <= 0x06FF)
        or (0x0750 <= cp <= 0x077F)
        or (0x08A0 <= cp <= 0x08FF)
        or (0xFB50 <= cp <= 0xFDFF)
        or (0xFE70 <= cp <= 0xFEFF)
    )


def _clean_arabic(text: str) -> str:
    """أزل محارف التحكم التي تترك آثارًا خفية في اتجاه النص."""
    text = text.replace(TATWEEL, "")
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", text)
    # الحركات لا تؤثر في الحالة المطلوبة، وإزالتها تجعل fallback مستقرًا.
    return re.sub(r"[\u064b-\u065f]", "", text)


def _reshape(text: str) -> str:
    """reshape مرة واحدة على النص المنطقي كاملًا.

    لا نقسم النص حسب نوع المحرف: التقسيم السابق كان يترك الأرقام والرموز
    داخل segment عربي، ثم يمرر الناتج إلى bidi، ما يغير مواضع 6 وK وB والمدى.
    arabic_reshaper آمن مع اللاتينية والأرقام ويصل الحروف العربية حيث يلزم.
    """
    cleaned = _clean_arabic(text)
    if not _ARABIC_OK:
        return cleaned
    return arabic_reshaper.reshape(cleaned)


def _isolate_ltr_runs(text: str) -> str:
    return _LTR_RUN_RE.sub(lambda m: f"{_LRM}{m.group(1)}{_LRM}", text)


def fix_mixed_text(text: str) -> str:
    """جهّز نصًا للرسم المباشر على ``canvas`` (reshape + bidi مرة واحدة)."""
    cleaned = _clean_arabic(text)
    if not _ARABIC_OK:
        return cleaned
    try:
        # LRM مؤقت، ثم إزالته بعد get_display حتى لا يظهر في النص المرسوم.
        # محارف Unicode الخاصة بالمدى والنسبة لا تنزاح إلى طرف السياق العربي.
        cleaned = re.sub(r"(?<=\d)-(?=\d)", "\u2010", cleaned)
        cleaned = cleaned.replace("%", "٪")
        isolated = _isolate_ltr_runs(cleaned)
        # لا نستخدم _reshape هنا: _clean_arabic داخله يزيل LRM قبل أن يراه
        # bidi، فتنعكس النسب والمدى والتواريخ داخل الجملة العربية.
        reshaped = arabic_reshaper.reshape(isolated)
        # في السطر المرئي العربي، بداية السلسلة هي الطرف الأيسر بصريًا؛
        # لذلك تبقى علامة نهاية الجملة هناك. لا ننقلها إلى نهاية السلسلة،
        # لأن ذلك يضعها في بداية الجملة العربية على الصفحة.
        return get_display(reshaped).replace(_LRM, "")
    except Exception:
        return cleaned


def prepare_for_reportlab(text: str, max_width: float | None = None,
                          font_name: str = "Amiri", font_size: float = 20) -> str:
    """جهّز نصًا عربيًا للرسم المباشر بدون قلب ترتيب الأسطر.

    يتم تقسيم الكلمات بالترتيب المنطقي أولًا، ثم تحويل كل سطر إلى visual
    order مرة واحدة. بذلك لا يستطيع Paragraph قلب ترتيب السطور عند الالتفاف.
    القيمة المعادة تستخدم ``\\n`` كفواصل سطور، وكل سطر فيها visual order.
    """
    cleaned = _clean_arabic(text).strip()
    if not cleaned:
        return ""
    if max_width is None:
        return fix_mixed_text(cleaned)

    words = re.findall(r"\S+", cleaned)
    logical_lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        visual_candidate = fix_mixed_text(candidate)
        if current and stringWidth(visual_candidate, font_name, font_size) > max_width:
            logical_lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        logical_lines.append(current)
    # هذه السطور ستُرسم مباشرة على canvas؛ لا نضيف محارف اتجاه قد تظهر في
    # بعض الخطوط أو عند استخراج النص من PDF.
    return "\n".join(fix_mixed_text(line) for line in logical_lines)


def split_visual_runs(text: str) -> list[tuple[str, str]]:
    """قسّم سطرًا visual إلى runs عربية ولاتينية/رقمية.

    النص هنا مرّ عبر ``fix_mixed_text`` بالفعل؛ لذلك لا نعيد Bidi. المحارف
    المحايدة (المسافات والترقيم) تبقى مع الـrun السابق حتى لا تنفصل الأقواس
    أو علامات النسبة والمدى عن token الخاص بها.
    """
    runs: list[tuple[str, str]] = []
    current_kind: str | None = None
    current: list[str] = []

    def flush():
        nonlocal current, current_kind
        if current:
            runs.append((current_kind or "ar", "".join(current)))
            current = []

    for ch in text:
        if ch.isascii() and (ch.isalpha() or ch.isdigit()):
            kind = "latin"
        elif ch.isdigit() or unicodedata.category(ch).startswith("N"):
            kind = "latin"
        elif _is_arabic_char(ch):
            kind = "ar"
        else:
            # whitespace/punctuation: preserve with the current visual run.
            current.append(ch)
            continue
        if current_kind is not None and kind != current_kind:
            flush()
        current_kind = kind
        current.append(ch)
    flush()
    return runs


def is_arabic(text: str) -> bool:
    return any(unicodedata.bidirectional(ch) in ("R", "AL", "AN") for ch in text)


def line_direction(text: str) -> str:
    for ch in text:
        bidi = unicodedata.bidirectional(ch)
        if bidi in ("R", "AL"):
            return "rtl"
        if bidi == "L":
            return "ltr"
    return "rtl"


def detect_language(text: str) -> str:
    has_arabic = any(unicodedata.bidirectional(ch) in ("R", "AL", "AN") for ch in text)
    has_latin = any(unicodedata.bidirectional(ch) == "L" for ch in text)
    if has_arabic and has_latin:
        return "mixed"
    if has_arabic:
        return "arabic"
    return "english"
