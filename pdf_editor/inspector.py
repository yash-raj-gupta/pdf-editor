"""Read-only PDF inspection."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Iterator

import fitz  # PyMuPDF

from .types import TextSpan, _normalize_text, int_to_hex


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
