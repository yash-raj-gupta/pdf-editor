#!/usr/bin/env python3
"""Bundle commonly-used PDF fonts into the project's `fonts/` directory.

Run this once after cloning to populate `fonts/`, or re-run later to add
new families. Idempotent: existing files are skipped. Failures are logged
but never abort the whole run.

Why these fonts: when an edit needs to render a character not present in
the original PDF's embedded subset, the editor falls back to a system
font matching the PDF's font name. Bundling these into `fonts/` means
every common PDF source — Stripe, Google, Adobe, Notion, etc. — has its
font locally available, so fallbacks visually match the original face
rather than dropping into a generic Arial.

All fonts here are SIL Open Font License or Apache 2.0 — redistributable.

Source: jsdelivr's mirror of Fontsource (which packages Google Fonts'
static TTFs). Pinned to `@latest`, so re-running picks up upstream
updates.
"""
from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = ROOT / "fonts"
FONTS_DIR.mkdir(exist_ok=True)


# Numeric CSS font-weights -> the style name we save the file under,
# matching how upstream font releases name their static TTFs.
_WEIGHT_NAMES = {
    100: "Thin",       200: "ExtraLight", 300: "Light",
    400: "",           500: "Medium",     600: "SemiBold",
    700: "Bold",       800: "ExtraBold",  900: "Black",
}


def _style_name(weight: int, italic: bool) -> str:
    base = _WEIGHT_NAMES.get(weight, str(weight))
    if weight == 400:
        return "Italic" if italic else "Regular"
    return f"{base}Italic" if italic else base


# Each entry: (Project font name, Fontsource slug, variants).
# Variants: list of (weight, italic) pairs to fetch.
#
# Keep this list short and well-justified — every line is a Latin TTF
# (~70 KB). Add a family when you actually see a PDF that uses it; don't
# pre-bundle theoreticals.
FONTS: list[tuple[str, str, list[tuple[int, bool]]]] = [
    # ---- Sans-serif (the bulk of PDFs in the wild) ----
    # Stripe, Linear, Vercel, modern SaaS receipts/invoices.
    ("Inter",          "inter",
     [(400, False), (400, True), (500, False), (500, True),
      (600, False), (600, True), (700, False), (700, True)]),
    # Google Workspace — Forms, Docs PDF exports, Gmail receipts.
    ("Roboto",         "roboto",
     [(400, False), (400, True), (500, False), (500, True),
      (700, False), (700, True)]),
    # Atlassian, lots of marketing and webapp PDFs.
    ("OpenSans",       "open-sans",
     [(400, False), (400, True), (500, False), (500, True),
      (600, False), (600, True), (700, False), (700, True)]),
    # Notion exports, lots of indie SaaS.
    ("Lato",           "lato",
     [(400, False), (400, True), (700, False), (700, True)]),
    # Adobe products' default sans (Acrobat, InDesign defaults).
    ("SourceSans3",    "source-sans-3",
     [(400, False), (400, True), (600, False), (600, True),
      (700, False), (700, True)]),
    # Wide Unicode coverage — used for international docs / mixed scripts.
    ("NotoSans",       "noto-sans",
     [(400, False), (400, True), (700, False), (700, True)]),
    # Twitter / X / Vercel / many tech docs.
    ("Manrope",        "manrope",
     [(400, False), (500, False), (600, False), (700, False)]),

    # ---- Serif (less common but real edge cases) ----
    # Adobe products, journalism PDFs.
    ("SourceSerif4",   "source-serif-4",
     [(400, False), (400, True), (700, False), (700, True)]),
    # Academic / blog PDFs (Medium uses it).
    ("Merriweather",   "merriweather",
     [(400, False), (400, True), (700, False), (700, True)]),
    # Newspapers, magazine PDFs.
    ("Lora",           "lora",
     [(400, False), (400, True), (700, False), (700, True)]),
    # Wikipedia-style PDFs.
    ("PTSerif",        "pt-serif",
     [(400, False), (400, True), (700, False), (700, True)]),

    # ---- Mono (developer-generated, technical PDFs) ----
    ("RobotoMono",     "roboto-mono",
     [(400, False), (700, False)]),
    ("JetBrainsMono",  "jetbrains-mono",
     [(400, False), (700, False)]),
    ("SourceCodePro",  "source-code-pro",
     [(400, False), (700, False)]),
    # GitHub / VS Code defaults.
    ("FiraCode",       "fira-code",
     [(400, False), (700, False)]),
]


def _download(url: str, dest: Path) -> bool:
    """Stream `url` to `dest`. Returns True on success and a sane size."""
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            if resp.status != 200:
                return False
            data = resp.read()
        # A 4xx page is usually a few hundred bytes of HTML; real fonts
        # are >10 KB. Reject anything implausibly tiny.
        if len(data) < 4096:
            return False
        dest.write_bytes(data)
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


def main() -> int:
    n_ok = n_skip = n_fail = 0
    print(f"Writing fonts to {FONTS_DIR}")

    for project_name, slug, variants in FONTS:
        for weight, italic in variants:
            style = _style_name(weight, italic)
            fname = f"{project_name}-{style}.ttf" if style else f"{project_name}.ttf"
            dest = FONTS_DIR / fname

            if dest.exists() and dest.stat().st_size >= 4096:
                n_skip += 1
                continue

            url = (f"https://cdn.jsdelivr.net/fontsource/fonts/{slug}@latest/"
                   f"latin-{weight}-{'italic' if italic else 'normal'}.ttf")
            if _download(url, dest):
                n_ok += 1
                kb = dest.stat().st_size // 1024
                print(f"  ok:   {fname:<32} ({kb} KB)")
            else:
                n_fail += 1
                dest.unlink(missing_ok=True)
                print(f"  FAIL: {fname:<32} ({slug} {weight}{'i' if italic else ''})")

    total_kb = sum(p.stat().st_size for p in FONTS_DIR.glob("*.ttf")) // 1024
    print(f"\n{n_ok} downloaded, {n_skip} skipped, {n_fail} failed.")
    print(f"fonts/ now has {sum(1 for _ in FONTS_DIR.glob('*.ttf'))} TTFs ({total_kb} KB total).")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
