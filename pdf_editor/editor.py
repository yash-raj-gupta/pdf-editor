"""PDF editing — replace_text, replace_spans, add_text, save."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import fitz  # PyMuPDF

from .types import (
    FLAG_BOLD, FLAG_ITALIC, FLAG_MONOSPACED, FLAG_SERIFED,
    _adapt_text_typography, _normalize_text,
    int_to_rgb,
)
from .fonts import (
    _BASE14, _normalize_font_name, _ps_name_to_base14,
    _system_font_index, _system_font_path,
)


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

    def add_text(
        self,
        page: int,
        point: tuple[float, float],
        text: str,
        *,
        font_name: str = "",
        fontsize: float = 11.0,
        color: tuple[float, float, float] = (0.0, 0.0, 0.0),
        bold: bool = False,
        italic: bool = False,
        redact_bbox: tuple[float, float, float, float] | None = None,
    ) -> None:
        """Insert new text at `point` on `page` (point is the baseline-left).

        Uses the same font-resolution + per-character fallback as replace_text,
        so the text renders cleanly even if some characters aren't in any
        single font on the system.

        When `redact_bbox` is given, the rectangle is whitened first via the
        redaction mechanism — this is what lets edits on OCR'd spans (whose
        original content is part of a scanned image) cover the underlying
        image cleanly before drawing the new text on top.
        """
        page_obj = self.doc[page]
        flags = (FLAG_BOLD if bold else 0) | (FLAG_ITALIC if italic else 0)

        if redact_bbox is not None:
            try:
                page_obj.add_redact_annot(fitz.Rect(redact_bbox), fill=(1, 1, 1))
                try:
                    page_obj.apply_redactions(images=2, graphics=0, text=0)
                except TypeError:
                    page_obj.apply_redactions()
            except Exception as e:
                self.warnings.append(
                    f"page {page}: redact for add_text failed: {e}"
                )

        # Try to use the named font if it's embedded already (rare for new text
        # — usually user-added text is in a fresh face) or pick a system font.
        emb_alias, emb_obj, _ = self._resolve_embedded_font(page_obj, font_name)
        fb_alias, fb_obj = self._resolve_fallback_font(page_obj, font_name, flags)

        # Fast path — embedded font has every glyph
        if emb_alias and emb_obj and all(emb_obj.has_glyph(ord(c)) for c in text):
            page_obj.insert_text(fitz.Point(*point), text, fontname=emb_alias,
                                 fontsize=fontsize, color=color)
            return

        # If no embedded match, just use fallback for the whole string.
        if emb_obj is None or emb_alias is None:
            page_obj.insert_text(fitz.Point(*point), text, fontname=fb_alias,
                                 fontsize=fontsize, color=color)
            return

        # Mixed: per-character runs.
        runs: list[tuple[str, fitz.Font | None, str]] = []
        for c in text:
            cur = (emb_alias, emb_obj) if emb_obj.has_glyph(ord(c)) else (fb_alias, fb_obj)
            if runs and runs[-1][0] == cur[0]:
                runs[-1] = (cur[0], cur[1], runs[-1][2] + c)
            else:
                runs.append((cur[0], cur[1], c))

        x = float(point[0])
        y = float(point[1])
        for alias, obj, segment in runs:
            page_obj.insert_text((x, y), segment, fontname=alias,
                                 fontsize=fontsize, color=color)
            try:
                if obj is not None:
                    w = obj.text_length(segment, fontsize=fontsize)
                else:
                    w = fitz.get_text_length(segment, fontname=alias,
                                             fontsize=fontsize)
            except Exception:
                w = len(segment) * fontsize * 0.5
            x += w

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
        bbox = span.get("bbox", (0, 0, 0, 0))
        bbox_width = max(1.0, float(bbox[2]) - float(bbox[0]))

        emb_alias, emb_obj, basefont = self._resolve_embedded_font(page, font_name)

        # Fast path — all chars covered by the embedded font.
        if emb_alias and emb_obj and all(emb_obj.has_glyph(ord(c)) for c in text):
            fitted = self._fit_size(text, emb_obj, size, bbox_width)
            if abs(fitted - size) > 0.01:
                self.warnings.append(
                    f"page {page.number}: shrunk {size:.1f}→{fitted:.1f}pt to "
                    f"fit replacement {text!r} in {bbox_width:.1f}pt-wide bbox"
                )
            try:
                page.insert_text(origin, text, fontname=emb_alias,
                                 fontsize=fitted, color=color, render_mode=0)
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
            fitted = self._fit_size(text, fb_obj, size, bbox_width)
            try:
                page.insert_text(origin, text, fontname=fb_alias,
                                 fontsize=fitted, color=color)
            except Exception as e:
                self.warnings.append(
                    f"page {page.number}: insert_text({fb_alias!r}) failed: "
                    f"{e}; using helv as last resort"
                )
                page.insert_text(origin, text, fontname="helv",
                                 fontsize=fitted, color=color)
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

        # Auto-fit: measure total width of all runs and shrink uniformly.
        fitted = self._fit_size_runs(runs, size, bbox_width)
        if abs(fitted - size) > 0.01:
            self.warnings.append(
                f"page {page.number}: shrunk {size:.1f}→{fitted:.1f}pt to fit "
                f"replacement {text!r} in {bbox_width:.1f}pt-wide bbox"
            )

        x = origin.x
        fb_chars = 0
        missing: list[str] = []
        for alias, obj, segment in runs:
            try:
                page.insert_text((x, origin.y), segment, fontname=alias,
                                 fontsize=fitted, color=color)
            except Exception as e:
                self.warnings.append(
                    f"page {page.number}: insert_text({alias!r}, {segment!r}) "
                    f"failed: {e}"
                )
            # Advance x by the segment's width in this font at the fitted size.
            try:
                if obj is not None:
                    w = obj.text_length(segment, fontsize=fitted)
                else:
                    w = fitz.get_text_length(segment, fontname=alias,
                                             fontsize=fitted)
            except Exception:
                w = len(segment) * fitted * 0.5  # rough fallback
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

    # ---- size auto-fit ---------------------------------------------------

    # Don't shrink unless the overflow is meaningful — a few pt of slack
    # avoids "0042 -> 0099" style renames triggering invisible 0.05pt shrinks
    # that visually look identical but break exact-size assertions.
    _FIT_OVERFLOW_TOLERANCE = 1.5  # PDF points

    @staticmethod
    def _fit_size(text: str, font_obj: "fitz.Font | None", original_size: float,
                  max_width: float, min_ratio: float = 0.7) -> float:
        """Pick a font size that makes `text` fit in `max_width`. Won't shrink
        more than `min_ratio` of the original (default 70%) — beyond that the
        visual hit from a tiny font is worse than the overflow."""
        if not font_obj or max_width <= 0:
            return original_size
        try:
            natural = font_obj.text_length(text, fontsize=original_size)
        except Exception:
            return original_size
        if natural <= max_width + PDFEditor._FIT_OVERFLOW_TOLERANCE or natural == 0:
            return original_size
        target = original_size * (max_width / natural)
        return target if target >= original_size * min_ratio else original_size

    @staticmethod
    def _fit_size_runs(runs: list[tuple[str, "fitz.Font | None", str]],
                       original_size: float, max_width: float,
                       min_ratio: float = 0.7) -> float:
        """Same as _fit_size but sums widths across mixed-font runs."""
        if max_width <= 0:
            return original_size
        natural = 0.0
        for alias, obj, segment in runs:
            try:
                if obj is not None:
                    natural += obj.text_length(segment, fontsize=original_size)
                else:
                    natural += fitz.get_text_length(segment, fontname=alias,
                                                    fontsize=original_size)
            except Exception:
                natural += len(segment) * original_size * 0.5
        if natural <= max_width + PDFEditor._FIT_OVERFLOW_TOLERANCE or natural == 0:
            return original_size
        target = original_size * (max_width / natural)
        return target if target >= original_size * min_ratio else original_size

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

