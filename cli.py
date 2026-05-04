"""Command-line interface for the PDF editor.

Examples
--------
    python cli.py inspect document.pdf
    python cli.py inspect document.pdf --json
    python cli.py find    document.pdf "Total revenue"
    python cli.py edit    document.pdf --find "12500" --replace "15750" -o out.pdf
    python cli.py edit    document.pdf -f "Hello" -r "Hola" -o out.pdf --page 1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pdf_editor import PDFEditor, PDFInspector


# ---- helpers ---------------------------------------------------------------

def _hr(title: str) -> None:
    print()
    print(title)
    print("-" * max(40, len(title)))


def _print_swatch(hex_color: str) -> str:
    """Return hex with a coloured ANSI block if the terminal supports it."""
    if not sys.stdout.isatty():
        return hex_color
    try:
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        return f"\x1b[48;2;{r};{g};{b}m   \x1b[0m {hex_color}"
    except Exception:
        return hex_color


# ---- commands --------------------------------------------------------------

def cmd_inspect(args: argparse.Namespace) -> int:
    with PDFInspector(args.pdf, password=args.password) as ins:
        report = ins.report()

        if args.json:
            print(json.dumps(report, indent=2, default=str))
            return 0

        print()
        print(f"  PDF: {report['file']}")
        print(f"  Pages: {report['pages']}")
        print("=" * 60)

        if any(report["metadata"].values()):
            _hr("METADATA")
            for k, v in report["metadata"].items():
                if v:
                    print(f"  {k:<14} {v}")

        _hr(f"FONTS USED  ({len(report['fonts_used'])} distinct)")
        for font, count in report["fonts_used"].items():
            print(f"  {font:<40} {count} spans")

        _hr(f"EMBEDDED FONTS  ({len(report['embedded_fonts'])} entries)")
        for f in report["embedded_fonts"]:
            mark = "embedded" if f.get("embedded") else "referenced"
            print(f"  xref={f['xref']:<5} {mark:<11} type={f['type']:<10} {f['basename']}")

        _hr(f"COLORS USED  ({len(report['colors_used'])} distinct)")
        for color, count in report["colors_used"].items():
            print(f"  {_print_swatch(color):<22} {count} spans")

        _hr(f"FONT SIZES  ({len(report['sizes_used'])} distinct)")
        for size, count in report["sizes_used"].items():
            print(f"  {size:>6}pt  {count} spans")

        print()
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    with PDFInspector(args.pdf, password=args.password) as ins:
        results = ins.find_text(args.query, case_sensitive=args.case_sensitive)
        if not results:
            print(f"No matches for {args.query!r}", file=sys.stderr)
            return 1

        print(f"\nFound {len(results)} match(es) for {args.query!r}:")
        for i, span in enumerate(results, 1):
            print(f"\n[{i}] page {span.page + 1}")
            print(f"    text   : {span.text!r}")
            print(f"    font   : {span.font}")
            print(f"    size   : {span.size}pt")
            print(f"    color  : {_print_swatch(span.color_hex)}")
            print(f"    style  : {span.style_summary()}")
            print(f"    bbox   : ({', '.join(f'{x:.1f}' for x in span.bbox)})")
            print(f"    origin : ({span.origin[0]:.1f}, {span.origin[1]:.1f})")
        print()
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    with PDFEditor(args.pdf, password=args.password) as ed:
        n = ed.replace_text(
            args.find,
            args.replace,
            page=(args.page - 1) if args.page else None,
            case_sensitive=args.case_sensitive,
            max_count=args.max_count,
        )
        if n == 0:
            print(f"No occurrences of {args.find!r} found.", file=sys.stderr)
            return 1
        ed.save(args.output)
        print(f"Replaced {n} span(s):  {args.find!r}  ->  {args.replace!r}")
        print(f"Wrote {args.output}")
        if ed.warnings and args.verbose:
            print("\nWarnings:")
            for w in ed.warnings:
                print(f"  - {w}")
        elif ed.warnings:
            print(f"({len(ed.warnings)} warnings — re-run with -v for details)")
    return 0


def cmd_replace_at(args: argparse.Namespace) -> int:
    bbox = tuple(float(x) for x in args.bbox.split(","))
    if len(bbox) != 4:
        print("--bbox must be x0,y0,x1,y1", file=sys.stderr)
        return 2
    with PDFEditor(args.pdf, password=args.password) as ed:
        ok = ed.replace_span(args.page - 1, bbox, args.text)
        if not ok:
            print("No span found inside that bbox.", file=sys.stderr)
            return 1
        ed.save(args.output)
        print(f"Replaced span at page {args.page} bbox {bbox} -> {args.text!r}")
        print(f"Wrote {args.output}")
    return 0


# ---- argparse plumbing -----------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pdf-editor",
        description="Inspect and edit PDFs while preserving font, size and color.",
    )
    p.add_argument("--password", help="PDF password if encrypted")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("inspect", help="Show fonts, colors, sizes and metadata")
    s.add_argument("pdf", type=Path)
    s.add_argument("--json", action="store_true", help="Emit a JSON report")
    s.set_defaults(func=cmd_inspect)

    s = sub.add_parser("find", help="Locate text and show its formatting")
    s.add_argument("pdf", type=Path)
    s.add_argument("query")
    s.add_argument("-i", "--ignore-case", dest="case_sensitive",
                   action="store_false", default=True)
    s.set_defaults(func=cmd_find)

    s = sub.add_parser("edit", help="Find/replace, preserving formatting")
    s.add_argument("pdf", type=Path)
    s.add_argument("-f", "--find", required=True)
    s.add_argument("-r", "--replace", required=True)
    s.add_argument("-o", "--output", required=True, type=Path)
    s.add_argument("--page", type=int, default=None,
                   help="Only operate on this page (1-indexed)")
    s.add_argument("-i", "--ignore-case", dest="case_sensitive",
                   action="store_false", default=True)
    s.add_argument("-n", "--max-count", type=int, default=None,
                   help="Maximum number of spans to modify")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_edit)

    s = sub.add_parser("replace-at",
                       help="Replace the span inside an exact bbox on a page")
    s.add_argument("pdf", type=Path)
    s.add_argument("--page", type=int, required=True, help="Page number (1-indexed)")
    s.add_argument("--bbox", required=True, help="x0,y0,x1,y1")
    s.add_argument("--text", required=True, help="New text for the span")
    s.add_argument("-o", "--output", required=True, type=Path)
    s.set_defaults(func=cmd_replace_at)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
