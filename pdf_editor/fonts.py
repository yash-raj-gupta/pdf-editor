"""Font discovery, system font index, and font-name normalization."""

from __future__ import annotations

import sys
from pathlib import Path

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


# Project-local fonts dir — drop TTFs here to make them available as
# fallback faces (e.g. Inter for Stripe PDFs). Takes precedence over system
# fonts because it sits at the front of the scan order.
_PROJECT_FONTS_DIR = Path(__file__).resolve().parent / "fonts"


def _all_system_font_dirs() -> list[Path]:
    dirs: list[Path] = [_PROJECT_FONTS_DIR]
    if sys.platform == "darwin":
        dirs += [
            Path("/System/Library/Fonts"),
            Path("/System/Library/Fonts/Supplemental"),
            Path("/Library/Fonts"),
            Path.home() / "Library/Fonts",
        ]
    elif sys.platform.startswith("linux"):
        dirs += [
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
            Path.home() / ".fonts",
            Path.home() / ".local/share/fonts",
        ]
    elif sys.platform == "win32":
        dirs += [Path("C:/Windows/Fonts")]
    return dirs


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


# PyMuPDF base14 font aliases — used as last-resort fallback.
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

