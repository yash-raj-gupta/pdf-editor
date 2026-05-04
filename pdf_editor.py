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
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

import fitz  # PyMuPDF


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
        origin = fitz.Point(*span["origin"])
        size = float(span.get("size", 11))
        color = int_to_rgb(span.get("color", 0))
        font_name = span.get("font", "")
        flags = int(span.get("flags", 0))

        fontname_to_use, used_embedded = self._resolve_font(page, font_name, flags)

        try:
            page.insert_text(
                origin,
                new_text,
                fontname=fontname_to_use,
                fontsize=size,
                color=color,
                render_mode=0,
            )
        except Exception as e:
            self.warnings.append(
                f"insert_text failed with {fontname_to_use!r} on page "
                f"{page.number}: {e}; falling back to helv"
            )
            page.insert_text(origin, new_text, fontname="helv",
                             fontsize=size, color=color)

        if not used_embedded:
            self.warnings.append(
                f"page {page.number}: original font {font_name!r} not embedded "
                f"or not extractable; replaced with base14 alias "
                f"{fontname_to_use!r} (metrics may differ)"
            )

    def _resolve_font(self, page: fitz.Page, font_name: str,
                      flags: int) -> tuple[str, bool]:
        """Return (alias_for_insert_text, used_original_embedded_font)."""
        target = _normalize_font_name(font_name)
        page_idx = page.number

        # 1. Try to reuse / re-embed the original font from this PDF.
        for f in self.doc.get_page_fonts(page_idx):
            xref, ext, ftype, basefont = f[0], f[1], f[2], f[3]
            if _normalize_font_name(basefont) != target:
                continue

            cache_key = (page_idx, xref)
            if cache_key in self._page_font_aliases:
                return self._page_font_aliases[cache_key], True

            if xref not in self._extracted_fonts:
                buf = None
                try:
                    extracted = self.doc.extract_font(xref)
                    # extract_font -> (basename, ext, type, content)
                    if extracted and len(extracted) >= 4:
                        buf = extracted[3] or None
                except Exception:
                    buf = None
                self._extracted_fonts[xref] = buf
            buf = self._extracted_fonts[xref]

            if buf:
                alias = f"emb_{xref}"
                try:
                    page.insert_font(fontname=alias, fontbuffer=buf)
                    self._page_font_aliases[cache_key] = alias
                    return alias, True
                except Exception as e:
                    self.warnings.append(
                        f"page {page_idx}: failed to re-embed {basefont!r}: {e}"
                    )
            break  # we found the matching font; fall through to base14

        # 2. Fall back to closest base14 PDF font.
        return self._fallback_base14(font_name, flags), False

    @staticmethod
    def _fallback_base14(font_name: str, flags: int) -> str:
        n = font_name.lower()
        is_bold = bool(flags & FLAG_BOLD) or any(
            t in n for t in ("bold", "black", "heavy"))
        is_italic = bool(flags & FLAG_ITALIC) or "italic" in n or "oblique" in n

        if bool(flags & FLAG_MONOSPACED) or any(
                t in n for t in ("mono", "cour", "consolas")):
            family = "mono"
        elif bool(flags & FLAG_SERIFED) or any(
                t in n for t in ("times", "serif", "georgia", "garamond")):
            family = "serif"
        else:
            family = "sans"
        return _BASE14[(family, is_bold, is_italic)]
