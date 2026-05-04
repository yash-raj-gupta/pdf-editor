"""Realistic test: build a PDF with truly embedded TrueType fonts (Georgia
family from the system), then edit it and verify the editor extracts and
re-uses the embedded font for the replacement so it looks identical.

This exercises the path that matters for real-world PDFs — base14 fallback
is fine for synthetic samples but most PDFs in the wild have subset
TrueType fonts embedded.
"""

from __future__ import annotations

from pathlib import Path

import fitz

from pdf_editor import PDFEditor, PDFInspector


HERE = Path(__file__).resolve().parent
SAMPLE = HERE / "sample_embedded.pdf"
EDITED = HERE / "sample_embedded_edited.pdf"

GEORGIA          = Path("/System/Library/Fonts/Supplemental/Georgia.ttf")
GEORGIA_BOLD     = Path("/System/Library/Fonts/Supplemental/Georgia Bold.ttf")
GEORGIA_ITALIC   = Path("/System/Library/Fonts/Supplemental/Georgia Italic.ttf")


def build() -> None:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    # Register and use Georgia variants, which forces them to be embedded.
    page.insert_font(fontname="geor", fontfile=str(GEORGIA))
    page.insert_font(fontname="geob", fontfile=str(GEORGIA_BOLD))
    page.insert_font(fontname="geoi", fontfile=str(GEORGIA_ITALIC))

    page.insert_text((72, 110), "Invoice #INV-2026-0042",
                     fontname="geob", fontsize=22, color=(0.05, 0.10, 0.30))
    page.insert_text((72, 150), "Customer: Sharma Electronics",
                     fontname="geor", fontsize=13, color=(0, 0, 0))
    page.insert_text((72, 175), "Date: 04 May 2026",
                     fontname="geoi", fontsize=12, color=(0.30, 0.30, 0.30))
    page.insert_text((72, 230), "Subtotal:  INR 48500",
                     fontname="geor", fontsize=13, color=(0, 0, 0))
    page.insert_text((72, 252), "GST 18%:   INR 8730",
                     fontname="geor", fontsize=13, color=(0, 0, 0))
    page.insert_text((72, 274), "Total:     INR 57230",
                     fontname="geob", fontsize=13, color=(0.55, 0.05, 0.05))

    doc.save(SAMPLE)
    doc.close()


def main() -> int:
    if not GEORGIA.exists():
        print(f"skipping: {GEORGIA} not found on this system")
        return 0

    print(f"[1] building embedded-font sample: {SAMPLE.name}")
    build()

    print("\n[2] inspecting (looking for embedded font entries)")
    with PDFInspector(SAMPLE) as ins:
        embedded = ins.embedded_fonts()
        for f in embedded:
            print(f"  xref={f['xref']:<3} ext={f['ext']:<5} type={f['type']:<10} "
                  f"{'EMBEDDED' if f['embedded'] else 'ref-only':<10} {f['basename']}")
        truly_embedded = [f for f in embedded if f["embedded"]]
        print(f"  {len(truly_embedded)} truly embedded font(s) detected")

    edits = [
        ("INV-2026-0042", "INV-2026-0099"),
        ("Sharma Electronics", "Patel Hardware"),
        ("48500", "62400"),
        ("8730",  "11232"),
        ("57230", "73632"),
        ("04 May 2026", "07 May 2026"),
    ]

    print(f"\n[3] applying {len(edits)} edits -> {EDITED.name}")
    with PDFEditor(SAMPLE) as ed:
        total = 0
        for find, replace in edits:
            n = ed.replace_text(find, replace)
            print(f"  {find!r:<22} -> {replace!r:<22}  ({n} span(s))")
            total += n
        ed.save(EDITED)
        print(f"  total spans modified: {total}")
        if ed.warnings:
            print(f"\n  warnings ({len(ed.warnings)}):")
            for w in ed.warnings:
                print(f"    - {w}")
        else:
            print("  no warnings — every span re-used its original embedded font")

    print("\n[4] re-inspecting edited PDF")
    fails = 0
    with PDFInspector(EDITED) as ins:
        for s in ins.iter_spans():
            print(f"  {s.text!r:<32}  font={s.font:<28} size={s.size}  color={s.color_hex}")

        checks = [
            ("INV-2026-0099", "Georgia", 22.0),
            ("Patel Hardware", "Georgia", 13.0),
            ("62400", "Georgia", 13.0),
            ("73632", "Georgia", 13.0),
        ]
        print("\nverifying replacements use the original Georgia family:")
        for needle, font_substr, exp_size in checks:
            hits = ins.find_text(needle)
            if not hits:
                print(f"  FAIL  {needle!r} not found")
                fails += 1
                continue
            s = hits[0]
            font_ok = font_substr.lower() in s.font.lower()
            size_ok = abs(s.size - exp_size) < 0.01
            mark = "OK  " if (font_ok and size_ok) else "FAIL"
            if not (font_ok and size_ok):
                fails += 1
            print(f"  {mark} {needle!r:<18} font={s.font:<28} size={s.size}")

    print(f"\n[5] done. Sample={SAMPLE.name}, Edited={EDITED.name}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
