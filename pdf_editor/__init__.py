"""PDF inspection and editing with font/size/color preservation.

Strategy for "edits look original":
  1. Read each text span via PyMuPDF's structured extraction so we have the
     exact font name, size, color, flags (bold/italic), bbox and baseline
     origin point.
  2. To replace text, redact the original (which strips it from the page
     content stream, not just paints over it) and re-insert the new text at
     the same baseline using the original font when the embedded subset
     covers all the new characters; otherwise fall back per-character.
  3. System font index (~/Library/Fonts, /System/Library/Fonts, project's
     fonts/) maps the PDF's font name (e.g. "Inter-SemiBold") to a local
     TTF when available, so fallbacks visually match the identified font
     family rather than always landing in Arial.
"""

from __future__ import annotations

from .types import (
    TextSpan,
    int_to_rgb, int_to_hex,
    FLAG_SUPERSCRIPT, FLAG_ITALIC, FLAG_SERIFED, FLAG_MONOSPACED, FLAG_BOLD,
    _adapt_text_typography, _normalize_text, _GLYPH_EQUIV_CLASSES,
)
from .fonts import (
    _SystemFontIndex, _system_font_index, _system_font_path,
    _normalize_psname, _normalize_font_name,
    _ps_name_to_base14, _BASE14, _BASE14_PS_NAMES,
    _all_system_font_dirs, _PROJECT_FONTS_DIR,
)
from .inspector import PDFInspector
from .editor import PDFEditor

__all__ = [
    # public
    "TextSpan", "PDFInspector", "PDFEditor",
    "int_to_rgb", "int_to_hex",
    # flags
    "FLAG_SUPERSCRIPT", "FLAG_ITALIC", "FLAG_SERIFED",
    "FLAG_MONOSPACED", "FLAG_BOLD",
    # internal helpers exposed for tests
    "_adapt_text_typography", "_normalize_text",
    "_normalize_font_name", "_normalize_psname",
    "_SystemFontIndex", "_system_font_index", "_system_font_path",
    "_ps_name_to_base14", "_BASE14", "_BASE14_PS_NAMES",
    "_all_system_font_dirs", "_PROJECT_FONTS_DIR",
    "_GLYPH_EQUIV_CLASSES",
]
