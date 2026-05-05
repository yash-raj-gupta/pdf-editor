"""PDF inspection and editing with font/size/color preservation.

Strategy for "edits look original":
  1. Read each text span via PyMuPDF's structured extraction so we have the
     exact font name, size, color, flags (bold/italic), bbox and baseline
     origin point.
  2. To replace text, redact the original (which strips it from the page
     content stream, not just paints over it) and re-insert the new text at
     the same baseline using the same font, size and color.
  3. When the original font is embedded in the PDF, extract its bytes and
     re-register it on the page so the replacement uses *the same glyphs*.
     For non-embedded or subset-only fonts, fall back to the closest base14
     font (Helvetica/Times/Courier in the right weight/style).
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

import fitz  # PyMuPDF


# ---- system fallback font discovery ----------------------------------------
#
# When the original embedded subset doesn't have a glyph for some user-typed
# character, we render JUST THAT CHARACTER in a wide-coverage system font so
# the rest of the span stays in the original font. This is the difference
# between "the whole line turns into Times-Roman" and "only the M is in a
# different face."

_MAC_FONTS_DIR = Path("/System/Library/Fonts/Supplemental")
_LINUX_FONT_DIRS = (
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/dejavu"),
    Path("/usr/share/fonts/TTF"),
)


def _all_system_font_dirs() -> list[Path]:
    if sys.platform == "darwin":
        return [
            Path("/System/Library/Fonts"),
            Path("/System/Library/Fonts/Supplemental"),
            Path("/Library/Fonts"),
            Path.home() / "Library/Fonts",
        ]
    if sys.platform.startswith("linux"):
        return [
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
            Path.home() / ".fonts",
            Path.home() / ".local/share/fonts",
        ]
    if sys.platform == "win32":
        return [Path("C:/Windows/Fonts")]
    return []


def _normalize_psname(name: str) -> str:
    """Collapse names like 'Inter SemiBold', 'Inter-SemiBold', 'XYZ123+Inter-SemiBold'
    into a single comparison key. Subset prefixes are stripped."""
    if not name:
        return ""
    n = name.split("+", 1)[1] if "+" in name else name
    return n.lower().replace(" ", "").replace("-", "").replace("_", "")


class _SystemFontIndex:
    """Lazily-built index of TrueType/OpenType fonts installed on this machine.

    Lets us look up a font by its PostScript name (so a PDF saying
    'Inter-SemiBold' resolves to the user's installed Inter SemiBold) or by
    family + bold + italic flags (so 'Inter-Italic' falls back to anything
    in the Inter family if the exact face isn't installed).
    """

    def __init__(self) -> None:
        self._by_psname: dict[str, Path] = {}
        self._by_family: dict[tuple[str, bool, bool], Path] = {}
        self._built = False

    def build(self) -> None:
        if self._built:
            return
        for d in _all_system_font_dirs():
            if not d.exists():
                continue
            for ext in ("*.ttf", "*.otf"):
                for path in d.glob(ext):
                    self._index_one(path)
        self._built = True

    def _index_one(self, path: Path) -> None:
        try:
            font = fitz.Font(fontfile=str(path))
            psname = font.name or ""
        except Exception:
            return
        if not psname:
            return
        key = _normalize_psname(psname)
        # First match wins (so /System wins over user fonts for duplicate names)
        self._by_psname.setdefault(key, path)

        family, bold, italic = self._infer_style(psname)
        if family:
            fam_key = (family.lower(), bold, italic)
            self._by_family.setdefault(fam_key, path)

    # Style suffix tokens, longest first so "BoldItalic" is matched before "Bold".
    _STYLE_SUFFIXES = (
        "BoldItalic", "BoldOblique",
        "ExtraBold", "SemiBold", "DemiBold", "ExtraLight", "SemiLight",
        "Black", "Heavy", "Bold", "Italic", "Oblique",
        "Light", "Thin", "Medium", "Regular",
    )

    @staticmethod
    def _infer_style(psname: str) -> tuple[str, bool, bool]:
        """'Inter-SemiBold' -> ('Inter', True, False);
        'Helvetica-BoldOblique' -> ('Helvetica', True, True);
        'Arial Bold Italic' -> ('Arial', True, True)."""
        n = psname
        bold = any(t in n for t in
                   ("Bold", "Black", "Heavy", "ExtraBold", "SemiBold", "DemiBold"))
        italic = "Italic" in n or "Oblique" in n

        # Strip suffixes iteratively — handles "Arial Bold Italic" by stripping
        # "Italic" first, then "Bold".
        changed = True
        while changed:
            changed = False
            for suf in _SystemFontIndex._STYLE_SUFFIXES:
                for sep in ("-", " ", ""):
                    token = sep + suf
                    if n.endswith(token) and len(n) > len(token):
                        n = n[:-len(token)].rstrip(" -")
                        changed = True
                        break
                if changed:
                    break
        return n, bold, italic

    def lookup(self, font_name: str, family: str,
               bold: bool, italic: bool) -> Path | None:
        if not self._built:
            self.build()
        key = _normalize_psname(font_name)
        if key in self._by_psname:
            return self._by_psname[key]
        return self._by_family.get((family.lower(), bold, italic))


_FONT_INDEX: _SystemFontIndex | None = None


def _system_font_index() -> _SystemFontIndex:
    global _FONT_INDEX
    if _FONT_INDEX is None:
        _FONT_INDEX = _SystemFontIndex()
    return _FONT_INDEX


def _system_font_path(family: str, bold: bool, italic: bool) -> Path | None:
    """Return a system TrueType font matching the requested style, or None.

    On macOS we use the Arial / Times New Roman / Courier New family from
    /System/Library/Fonts/Supplemental — they have wider Latin coverage than
    base14 and individual TTF files are easy to load. On Linux we try DejaVu.
    """
    if sys.platform == "darwin":
        if family == "sans":
            name = ("Arial Bold Italic.ttf" if bold and italic
                    else "Arial Bold.ttf" if bold
                    else "Arial Italic.ttf" if italic
                    else "Arial.ttf")
        elif family == "serif":
            name = ("Times New Roman Bold Italic.ttf" if bold and italic
                    else "Times New Roman Bold.ttf" if bold
                    else "Times New Roman Italic.ttf" if italic
                    else "Times New Roman.ttf")
        elif family == "mono":
            name = ("Courier New Bold Italic.ttf" if bold and italic
                    else "Courier New Bold.ttf" if bold
                    else "Courier New Italic.ttf" if italic
                    else "Courier New.ttf")
        else:
            return None
        p = _MAC_FONTS_DIR / name
        return p if p.exists() else None

    if sys.platform.startswith("linux"):
        if family == "sans":
            stem = ("DejaVuSans-BoldOblique" if bold and italic
                    else "DejaVuSans-Bold" if bold
                    else "DejaVuSans-Oblique" if italic
                    else "DejaVuSans")
        elif family == "serif":
            stem = ("DejaVuSerif-BoldItalic" if bold and italic
                    else "DejaVuSerif-Bold" if bold
                    else "DejaVuSerif-Italic" if italic
                    else "DejaVuSerif")
        elif family == "mono":
            stem = ("DejaVuSansMono-BoldOblique" if bold and italic
                    else "DejaVuSansMono-Bold" if bold
                    else "DejaVuSansMono-Oblique" if italic
                    else "DejaVuSansMono")
        else:
            return None
        for d in _LINUX_FONT_DIRS:
            p = d / f"{stem}.ttf"
            if p.exists():
                return p
        return None

    return None  # other platforms: fall through to base14


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


_FONT_SUFFIXES = ("regular", "psmt", "mt", "ps")


def _normalize_font_name(name: str) -> str:
    """Collapse "Georgia Regular", "Georgia-Regular", "ABCDEF+Georgia" etc.
    into the same canonical token so name comparison is robust.
    """
    if not name:
        return ""
    n = name.split("+", 1)[1] if "+" in name else name  # strip subset prefix
    n = n.lower().replace(" ", "").replace("-", "").replace("_", "")
    for suf in _FONT_SUFFIXES:
        if n.endswith(suf) and len(n) > len(suf):
            n = n[: -len(suf)]
    return n


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


# ---- inspector -------------------------------------------------------------

class PDFInspector:
    """Read-only analysis of a PDF: text spans, fonts, colors, sizes."""

    def __init__(self, path: str | Path, password: str | None = None):
        self.path = Path(path)
        self.doc = fitz.open(self.path)
        if self.doc.needs_pass:
            if not password or not self.doc.authenticate(password):
                raise ValueError(f"PDF {self.path} is encrypted; password required")

    def __enter__(self) -> "PDFInspector":
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        self.doc.close()

    @property
    def page_count(self) -> int:
        return len(self.doc)

    def metadata(self) -> dict:
        return dict(self.doc.metadata or {})

    def iter_spans(self, page_num: int | None = None) -> Iterator[TextSpan]:
        pages = [page_num] if page_num is not None else range(len(self.doc))
        for pno in pages:
            page = self.doc[pno]
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:  # type 0 == text
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        yield TextSpan(
                            page=pno,
                            text=span.get("text", ""),
                            font=span.get("font", ""),
                            size=float(span.get("size", 0.0)),
                            color=int(span.get("color", 0)),
                            flags=int(span.get("flags", 0)),
                            bbox=tuple(span.get("bbox", (0, 0, 0, 0))),
                            origin=tuple(span.get("origin", (0, 0))),
                            ascender=float(span.get("ascender", 0.0)),
                            descender=float(span.get("descender", 0.0)),
                        )

    def fonts_used(self) -> dict[str, int]:
        c: Counter[str] = Counter()
        for s in self.iter_spans():
            c[s.font] += 1
        return dict(c.most_common())

    def colors_used(self) -> dict[str, int]:
        c: Counter[str] = Counter()
        for s in self.iter_spans():
            c[int_to_hex(s.color)] += 1
        return dict(c.most_common())

    def sizes_used(self) -> dict[float, int]:
        c: Counter[float] = Counter()
        for s in self.iter_spans():
            c[round(s.size, 2)] += 1
        return dict(sorted(c.items()))

    def embedded_fonts(self) -> list[dict]:
        out: list[dict] = []
        seen: set[int] = set()
        for pno in range(len(self.doc)):
            for f in self.doc.get_page_fonts(pno):
                xref = f[0]
                if xref in seen:
                    continue
                seen.add(xref)
                # Tuple shape: (xref, ext, type, basefont, refname, encoding)
                basefont = f[3] if len(f) > 3 else ""
                out.append({
                    "xref": xref,
                    "ext": f[1] if len(f) > 1 else "",
                    "type": f[2] if len(f) > 2 else "",
                    "basename": basefont,
                    "name": f[4] if len(f) > 4 else "",
                    "encoding": f[5] if len(f) > 5 else "",
                    "embedded": bool(f[1]) and f[1] != "n/a",
                })
        return out

    def find_text(self, query: str, *, case_sensitive: bool = True,
                  page: int | None = None) -> list[TextSpan]:
        """Search spans for `query`. Whitespace and dash variants are
        normalized so a plain-ASCII query matches text that contains NBSPs,
        soft hyphens, en-dashes etc. (which TTF round-trips often produce).
        """
        results: list[TextSpan] = []
        needle = _normalize_text(query)
        if not case_sensitive:
            needle = needle.lower()
        for span in self.iter_spans(page):
            hay = _normalize_text(span.text)
            if not case_sensitive:
                hay = hay.lower()
            if needle in hay:
                results.append(span)
        return results

    def report(self) -> dict:
        return {
            "file": str(self.path),
            "pages": self.page_count,
            "metadata": self.metadata(),
            "fonts_used": self.fonts_used(),
            "embedded_fonts": self.embedded_fonts(),
            "colors_used": self.colors_used(),
            "sizes_used": {str(k): v for k, v in self.sizes_used().items()},
        }


# ---- editor ----------------------------------------------------------------

# PostScript names of base14 fonts -> PyMuPDF aliases. PDFs that reference
# these standard fonts don't embed the font file — we use the alias directly.
_BASE14_PS_NAMES: dict[str, str] = {
    "helvetica":            "helv",
    "helvetica-bold":       "hebo",
    "helvetica-oblique":    "heit",
    "helvetica-boldoblique": "hebi",
    "times-roman":          "tiro",
    "times-bold":           "tibo",
    "times-italic":         "tiit",
    "times-bolditalic":     "tibi",
    "courier":              "cour",
    "courier-bold":         "cobo",
    "courier-oblique":      "coit",
    "courier-boldoblique":  "cobi",
    "symbol":               "symb",
    "zapfdingbats":         "zadb",
}


def _ps_name_to_base14(name: str) -> str | None:
    return _BASE14_PS_NAMES.get(name.replace(" ", "").lower())


# PyMuPDF base14 font aliases — used as fallback when no embedded font matches.
_BASE14 = {
    ("serif",  False, False): "tiro",  # Times-Roman
    ("serif",  True,  False): "tibo",  # Times-Bold
    ("serif",  False, True):  "tiit",  # Times-Italic
    ("serif",  True,  True):  "tibi",  # Times-BoldItalic
    ("sans",   False, False): "helv",  # Helvetica
    ("sans",   True,  False): "hebo",
    ("sans",   False, True):  "heit",
    ("sans",   True,  True):  "hebi",
    ("mono",   False, False): "cour",  # Courier
    ("mono",   True,  False): "cobo",
    ("mono",   False, True):  "coit",
    ("mono",   True,  True):  "cobi",
}


class PDFEditor:
    """Edit a PDF while preserving the original visual style."""

    def __init__(self, path: str | Path, password: str | None = None):
        self.path = Path(path)
        self.doc = fitz.open(self.path)
        if self.doc.needs_pass:
            if not password or not self.doc.authenticate(password):
                raise ValueError(f"PDF {self.path} is encrypted; password required")
        # Cache: (page_num, font_xref) -> alias inserted on that page.
        self._page_font_aliases: dict[tuple[int, int], str] = {}
        # Track per-page extracted font buffers to avoid extracting twice.
        self._extracted_fonts: dict[int, bytes | None] = {}
        self.warnings: list[str] = []

    def __enter__(self) -> "PDFEditor":
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        self.doc.close()

    # ---- public API ------------------------------------------------------

    def replace_text(self, find: str, replace: str, *,
                     page: int | None = None,
                     case_sensitive: bool = True,
                     max_count: int | None = None) -> int:
        """Find and replace text across the document, preserving formatting.

        Returns the number of spans modified. A span counts as one even if it
        contained multiple occurrences of `find`.
        """
        targets = self._collect_targets(find, replace, page=page,
                                        case_sensitive=case_sensitive,
                                        max_count=max_count)
        if not targets:
            return 0

        by_page: dict[int, list[tuple[dict, str]]] = defaultdict(list)
        for pno, span_dict, new_text in targets:
            by_page[pno].append((span_dict, new_text))

        total = 0
        for pno, items in by_page.items():
            self._apply_page_replacements(pno, items)
            total += len(items)
        return total

    def replace_span(self, page: int, bbox: tuple[float, float, float, float],
                     new_text: str) -> bool:
        """Replace the text in the span at `bbox` on `page` with `new_text`.

        Useful when you want to target a specific location rather than search.
        """
        span = self._find_span_by_bbox(page, bbox)
        if span is None:
            return False
        self._apply_page_replacements(page, [(span, new_text)])
        return True

    def replace_spans(
        self,
        edits: list[tuple[int, tuple[float, float, float, float], str]],
    ) -> int:
        """Apply a batch of bbox-targeted edits.

        Each edit is `(page_index, bbox, new_text)`. Edits on the same page are
        grouped so we redact and re-apply once per page rather than per edit.
        Returns the number of edits actually applied (spans we could match).
        """
        by_page: dict[int, list[tuple[dict, str]]] = defaultdict(list)
        unmatched = 0
        for page, bbox, new_text in edits:
            span = self._find_span_by_bbox(page, bbox)
            if span is None:
                unmatched += 1
                continue
            by_page[page].append((span, new_text))

        if unmatched:
            self.warnings.append(
                f"replace_spans: {unmatched} edit(s) skipped — no span at the "
                "given bbox (page may have shifted between extraction and save)"
            )

        total = 0
        for pno, items in by_page.items():
            self._apply_page_replacements(pno, items)
            total += len(items)
        return total

    def _find_span_by_bbox(self, page: int,
                           bbox: tuple[float, float, float, float],
                           tol: float = 0.5) -> dict | None:
        """Locate the span on `page` whose bbox matches `bbox` within `tol`.

        We compare each corner with a small tolerance because PyMuPDF
        sometimes round-trips bbox values with sub-pixel changes.
        """
        x0, y0, x1, y1 = bbox
        for block in self.doc[page].get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    sb = span["bbox"]
                    if (abs(sb[0] - x0) < tol and abs(sb[1] - y0) < tol
                            and abs(sb[2] - x1) < tol and abs(sb[3] - y1) < tol):
                        return span
        return None

    def save(self, output_path: str | Path) -> None:
        """Save the modified PDF, compressing and cleaning unused objects."""
        self.doc.save(str(output_path), garbage=4, deflate=True, clean=True)

    # ---- internals -------------------------------------------------------

    def _collect_targets(self, find: str, replace: str, *, page: int | None,
                         case_sensitive: bool,
                         max_count: int | None) -> list[tuple[int, dict, str]]:
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(re.escape(find), flags)
        out: list[tuple[int, dict, str]] = []
        pages = [page] if page is not None else range(len(self.doc))
        for pno in pages:
            p = self.doc[pno]
            for block in p.get_text("dict").get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = span.get("text", "")
                        # Normalize for matching so plain ASCII queries match
                        # spans that contain NBSP / soft-hyphen / en-dash etc.
                        norm = _normalize_text(text)
                        if not pattern.search(norm):
                            continue
                        new_text = pattern.sub(replace, norm)
                        out.append((pno, span, new_text))
                        if max_count and len(out) >= max_count:
                            return out
        return out

    def _apply_page_replacements(self, pno: int,
                                 items: list[tuple[dict, str]]) -> None:
        page = self.doc[pno]

        # Stage 1: redact every original bbox so the underlying text is
        # actually stripped from the content stream (not just painted over).
        for span_dict, _ in items:
            rect = fitz.Rect(span_dict["bbox"])
            try:
                page.add_redact_annot(rect, fill=(1, 1, 1))
            except Exception as e:
                self.warnings.append(f"redact failed on page {pno}: {e}")

        try:
            page.apply_redactions(images=0, graphics=0, text=0)
        except TypeError:
            page.apply_redactions()  # older PyMuPDF

        # Stage 2: re-insert each span at the original baseline origin.
        for span_dict, new_text in items:
            self._insert_replacement(page, span_dict, new_text)

    def _insert_replacement(self, page: fitz.Page, span: dict,
                            new_text: str) -> None:
        """Insert `new_text` at the span's baseline using per-character font
        fallback so the visual style stays as close to the original as
        possible.

        Strategy:
          1. Adapt typography (ASCII <-> typographic) to match the original.
          2. Resolve the original embedded font.
          3. Fast path: if every character has a glyph in the embedded font,
             do one insert_text in that font (pixel-identical to the original).
          4. Slow path: register a wide-coverage system fallback font (Arial /
             Times New Roman / Courier New on macOS, DejaVu on Linux). Walk
             the text char-by-char; group consecutive chars by which font has
             their glyph; insert each group at an advancing x position. Result
             is a mixed-font line where all the chars the original font
             supports stay in the original, and only the missing ones are in
             the fallback face.
        """
        origin = fitz.Point(*span["origin"])
        size = float(span.get("size", 11))
        color = int_to_rgb(span.get("color", 0))
        font_name = span.get("font", "")
        flags = int(span.get("flags", 0))
        original_text = span.get("text", "")

        text = _adapt_text_typography(new_text, original_text)

        emb_alias, emb_obj, basefont = self._resolve_embedded_font(page, font_name)

        # Fast path — all chars covered by the embedded font.
        if emb_alias and emb_obj and all(emb_obj.has_glyph(ord(c)) for c in text):
            try:
                page.insert_text(origin, text, fontname=emb_alias,
                                 fontsize=size, color=color, render_mode=0)
                return
            except Exception as e:
                self.warnings.append(
                    f"page {page.number}: insert_text({emb_alias!r}) failed: "
                    f"{e}; trying mixed-font path"
                )

        # Slow path — register fallback and do per-character font runs.
        fb_alias, fb_obj = self._resolve_fallback_font(page, font_name, flags)

        # If we have no embedded font at all, just dump everything in fallback.
        if emb_obj is None or emb_alias is None:
            try:
                page.insert_text(origin, text, fontname=fb_alias,
                                 fontsize=size, color=color)
            except Exception as e:
                self.warnings.append(
                    f"page {page.number}: insert_text({fb_alias!r}) failed: "
                    f"{e}; using helv as last resort"
                )
                page.insert_text(origin, text, fontname="helv",
                                 fontsize=size, color=color)
            self.warnings.append(
                f"page {page.number}: original font {font_name!r} not "
                f"embedded; rendered span in fallback {fb_alias!r}"
            )
            return

        # Group consecutive characters by which font carries their glyph.
        runs: list[tuple[str, fitz.Font | None, str]] = []
        for c in text:
            in_emb = emb_obj.has_glyph(ord(c))
            if in_emb:
                cur_alias, cur_obj = emb_alias, emb_obj
            else:
                cur_alias, cur_obj = fb_alias, fb_obj
            if runs and runs[-1][0] == cur_alias:
                runs[-1] = (cur_alias, cur_obj, runs[-1][2] + c)
            else:
                runs.append((cur_alias, cur_obj, c))

        x = origin.x
        fb_chars = 0
        missing: list[str] = []
        for alias, obj, segment in runs:
            try:
                page.insert_text((x, origin.y), segment, fontname=alias,
                                 fontsize=size, color=color)
            except Exception as e:
                self.warnings.append(
                    f"page {page.number}: insert_text({alias!r}, {segment!r}) "
                    f"failed: {e}"
                )
            # Advance x by the segment's width in this font.
            try:
                if obj is not None:
                    w = obj.text_length(segment, fontsize=size)
                else:
                    w = fitz.get_text_length(segment, fontname=alias,
                                             fontsize=size)
            except Exception:
                w = len(segment) * size * 0.5  # rough fallback
            x += w

            if alias != emb_alias:
                fb_chars += len(segment)
                if obj is not None:
                    missing.extend(c for c in segment if not obj.has_glyph(ord(c)))

        if fb_chars:
            self.warnings.append(
                f"page {page.number}: {fb_chars} of {len(text)} char(s) used "
                f"fallback {fb_alias!r} (embedded subset of {basefont!r} "
                f"lacked them)"
            )
        if missing:
            self.warnings.append(
                f"page {page.number}: characters {sorted(set(missing))!r} "
                f"are missing even from the fallback font — they will render "
                f"as .notdef glyphs"
            )

    # ---- font resolution -------------------------------------------------

    def _resolve_embedded_font(
        self, page: fitz.Page, font_name: str,
    ) -> tuple[str | None, "fitz.Font | None", str]:
        """Return (alias_on_page, font_object, basefont_name) for the original
        font of this span.

        Three cases:
          1. Font is embedded as TTF/OTF — extract its bytes, register on the
             page, return that alias. Used by typical real-world PDFs.
          2. Font is one of the base14 PDF standards (Helvetica, Times,
             Courier) referenced by name only — no embedding needed; PyMuPDF
             knows how to use them. Use the base14 alias directly.
          3. Otherwise — return Nones, caller will use the fallback path.
        """
        info = self._get_embedded_font_info(page, font_name)
        if info is not None:
            xref, buf, basefont = info
            try:
                font_obj = fitz.Font(fontbuffer=buf)
            except Exception as e:
                self.warnings.append(
                    f"page {page.number}: failed to load embedded font "
                    f"{basefont!r}: {e}"
                )
                return None, None, basefont
            alias = self._register_embedded_font(page, xref, buf)
            return alias, font_obj, basefont

        b14_alias = _ps_name_to_base14(font_name)
        if b14_alias is not None:
            try:
                font_obj = fitz.Font(fontname=b14_alias)
            except Exception:
                font_obj = None
            return b14_alias, font_obj, font_name

        return None, None, ""

    def _resolve_fallback_font(
        self, page: fitz.Page, font_name: str, flags: int,
    ) -> tuple[str, "fitz.Font | None"]:
        """Register and return (alias, font_object) for the fallback font.

        Resolution order:
          1. Look up the original font's PostScript name in the system font
             index. If we find it (e.g. PDF says "Inter-SemiBold" and the
             user has Inter SemiBold installed) use *that* font — visual
             match for the missing characters.
          2. Look up by family + bold + italic in the system index ("Inter"
             with bold=True, italic=False) — close visual match.
          3. Use a generic system TTF picked from the family (Arial Bold for
             sans-bold, etc.) — different face but full glyph coverage.
          4. Last resort: base14 (Helvetica/Times/Courier).
        """
        family, bold, italic = self._classify_font(font_name, flags)

        # 1 & 2: system index lookup
        path = _system_font_index().lookup(font_name, family, bold, italic)
        # 3: generic family-based system font
        if path is None:
            path = _system_font_path(family, bold, italic)

        if path is not None and path.exists():
            cache_key = (page.number, str(path))
            if cache_key in self._page_font_aliases:
                alias = self._page_font_aliases[cache_key]
            else:
                alias = f"sys{abs(hash(str(path))) & 0xfffff:05x}"
                try:
                    page.insert_font(fontname=alias, fontfile=str(path))
                    self._page_font_aliases[cache_key] = alias
                except Exception as e:
                    self.warnings.append(
                        f"page {page.number}: failed to register {path.name}: {e}"
                    )
                    alias = None
            if alias is not None:
                try:
                    obj = fitz.Font(fontfile=str(path))
                except Exception:
                    obj = None
                return alias, obj

        # 4: base14
        alias = _BASE14[(family, bold, italic)]
        try:
            obj = fitz.Font(fontname=alias)
        except Exception:
            obj = None
        return alias, obj

    @staticmethod
    def _classify_font(font_name: str, flags: int) -> tuple[str, bool, bool]:
        n = font_name.lower()
        bold = bool(flags & FLAG_BOLD) or any(
            t in n for t in ("bold", "black", "heavy"))
        italic = bool(flags & FLAG_ITALIC) or "italic" in n or "oblique" in n
        if bool(flags & FLAG_MONOSPACED) or any(
                t in n for t in ("mono", "cour", "consolas")):
            family = "mono"
        elif bool(flags & FLAG_SERIFED) or any(
                t in n for t in ("times", "serif", "georgia", "garamond")):
            family = "serif"
        else:
            family = "sans"
        return family, bold, italic

    def _get_embedded_font_info(
        self, page: fitz.Page, font_name: str,
    ) -> tuple[int, bytes, str] | None:
        """Find an embedded font matching `font_name`. Returns
        (xref, font_buffer, basefont_name) or None.
        """
        target = _normalize_font_name(font_name)
        for f in self.doc.get_page_fonts(page.number):
            xref = f[0]
            basefont = f[3] if len(f) > 3 else ""
            if _normalize_font_name(basefont) != target:
                continue
            if xref not in self._extracted_fonts:
                buf: bytes | None = None
                try:
                    extracted = self.doc.extract_font(xref)
                    if extracted and len(extracted) >= 4:
                        buf = extracted[3] or None
                except Exception:
                    buf = None
                self._extracted_fonts[xref] = buf
            buf = self._extracted_fonts[xref]
            if buf:
                return xref, buf, basefont
            return None
        return None

    def _register_embedded_font(
        self, page: fitz.Page, xref: int, buf: bytes,
    ) -> str | None:
        """Register an extracted font on the page (cached) and return its alias."""
        cache_key = (page.number, xref)
        if cache_key in self._page_font_aliases:
            return self._page_font_aliases[cache_key]
        alias = f"emb_{xref}"
        try:
            page.insert_font(fontname=alias, fontbuffer=buf)
            self._page_font_aliases[cache_key] = alias
            return alias
        except Exception as e:
            self.warnings.append(
                f"page {page.number}: failed to register font xref={xref}: {e}"
            )
            return None

