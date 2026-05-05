"""Pure data types and helpers used across the package — no fitz imports."""

from __future__ import annotations

from dataclasses import asdict, dataclass


# ---- color helpers ---------------------------------------------------------

def int_to_rgb(c: int | None) -> tuple[float, float, float]:
    """Convert a 0xRRGGBB integer to a (r, g, b) tuple in the 0..1 range."""
    if c is None:
        return (0.0, 0.0, 0.0)
    return (((c >> 16) & 0xFF) / 255, ((c >> 8) & 0xFF) / 255, (c & 0xFF) / 255)


def int_to_hex(c: int | None) -> str:
    if c is None:
        return "#000000"
    return f"#{c & 0xFFFFFF:06x}"

# ---- font flag helpers (PDF text flags from PyMuPDF) -----------------------

FLAG_SUPERSCRIPT = 1 << 0
FLAG_ITALIC      = 1 << 1
FLAG_SERIFED     = 1 << 2
FLAG_MONOSPACED  = 1 << 3
FLAG_BOLD        = 1 << 4
# ---- text and font-name normalization --------------------------------------

# PyMuPDF, when round-tripping text through embedded TrueType fonts, often
# substitutes regular ASCII whitespace/dashes with their typographic variants.
# We normalize for find/replace so a user's plain ASCII query still matches.
_TEXT_NORMALIZE = str.maketrans({
    "\xa0":   " ",   # NBSP
    "\xad":   "-",   # soft hyphen
    "‐": "-",   # hyphen
    "‑": "-",   # non-breaking hyphen
    "‒": "-",   # figure dash
    "–": "-",   # en dash
    "—": "-",   # em dash
    " ": " ",   # thin space
    " ": " ",   # hair space
    " ": " ",   # narrow NBSP
})


def _normalize_text(text: str) -> str:
    return text.translate(_TEXT_NORMALIZE)
# Typographic variants that PDF fonts commonly include in place of ASCII chars.
# When we replace text in a PDF that uses an embedded *subset* font, the subset
# only contains glyphs originally used; if the original used U+2010 hyphen, the
# subset has no glyph for U+002D ASCII hyphen, so inserting "-" renders as a
# blank .notdef box. We work around this by mirroring the original span's
# typography in the new text — any character present in the original is by
# definition present in the subset.
_GLYPH_EQUIV_CLASSES: tuple[tuple[str, ...], ...] = (
    ("-", "‐", "‑", "‒", "–", "—", "−"),
    (" ", " ", " ", " ", " ", " ", " "),
    ("'", "’", "‘", "ʼ"),
    ('"', "”", "“", "‟"),
)


def _adapt_text_typography(new_text: str, original_text: str) -> str:
    """Mirror the original span's typography in `new_text`.

    For each equivalence class in _GLYPH_EQUIV_CLASSES, find which variant
    the original uses and remap every other variant in `new_text` to that
    one. This keeps replacements inside the embedded subset font's actual
    glyph set, which is the difference between a clean edit and a blank
    .notdef box. Works in both directions: ASCII -> typographic when the
    original used typographic, and typographic -> ASCII when it didn't.
    """
    if not original_text:
        return new_text
    subs: dict[str, str] = {}
    for variants in _GLYPH_EQUIV_CLASSES:
        chosen = next((v for v in variants if v in original_text), None)
        if chosen is None:
            continue
        for v in variants:
            if v != chosen:
                subs[v] = chosen
    if not subs:
        return new_text
    return "".join(subs.get(c, c) for c in new_text)
@dataclass
class TextSpan:
    """One run of text with consistent formatting on a single page."""
    page: int
    text: str
    font: str
    size: float
    color: int
    flags: int
    bbox: tuple[float, float, float, float]
    origin: tuple[float, float]
    ascender: float = 0.0
    descender: float = 0.0

    @property
    def color_hex(self) -> str:
        return int_to_hex(self.color)

    @property
    def color_rgb(self) -> tuple[float, float, float]:
        return int_to_rgb(self.color)

    @property
    def is_bold(self) -> bool:
        if self.flags & FLAG_BOLD:
            return True
        n = self.font.lower()
        return any(t in n for t in ("bold", "black", "heavy"))

    @property
    def is_italic(self) -> bool:
        if self.flags & FLAG_ITALIC:
            return True
        n = self.font.lower()
        return "italic" in n or "oblique" in n

    @property
    def is_monospaced(self) -> bool:
        if self.flags & FLAG_MONOSPACED:
            return True
        n = self.font.lower()
        return any(t in n for t in ("mono", "courier", "consolas"))

    @property
    def is_serif(self) -> bool:
        if self.flags & FLAG_SERIFED:
            return True
        n = self.font.lower()
        return any(t in n for t in ("times", "serif", "georgia", "garamond"))

    def style_summary(self) -> str:
        bits = []
        if self.is_bold: bits.append("bold")
        if self.is_italic: bits.append("italic")
        if self.is_monospaced: bits.append("mono")
        elif self.is_serif: bits.append("serif")
        return ", ".join(bits) if bits else "regular"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["color_hex"] = self.color_hex
        d["style"] = self.style_summary()
        return d
