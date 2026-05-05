"""Validation and application of edit/add/image-insert payloads.

The save and preview-edited endpoints share the same payload shape:
    {"edits":  [...], "adds": [...], "images": [...]}

This module turns the raw JSON into typed Python data and then dispatches
each item to the right `PDFEditor` method.
"""

from __future__ import annotations

import base64
from typing import Any

import fitz  # PyMuPDF

from pdf_editor import PDFEditor


# Inserted images are sent as base64 in JSON. 8 MB after decode is plenty
# for signatures and logos and bounded enough that one upload can't fill
# disk on its own.
_MAX_IMAGE_BYTES = 8 * 1024 * 1024


def parse_edits(raw_edits: Any) -> list[tuple[int, tuple[float, float, float, float], str]]:
    """Validate and convert the JSON edits payload to typed tuples."""
    out: list[tuple[int, tuple[float, float, float, float], str]] = []
    for e in raw_edits or []:
        try:
            page = int(e["page"])
            bbox = tuple(float(x) for x in e["bbox"])
            new_text = str(e["new_text"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed edit {e!r}: {exc}") from None
        if len(bbox) != 4:
            raise ValueError(f"bbox must have 4 numbers: {e!r}")
        out.append((page, bbox, new_text))
    return out


def parse_adds(raw_adds: Any) -> list[dict]:
    """Validate the new-text additions payload."""
    out: list[dict] = []
    for a in raw_adds or []:
        try:
            page = int(a["page"])
            x = float(a["x"])
            y = float(a["y"])
            text = str(a["text"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed add {a!r}: {exc}") from None
        redact = a.get("redact_bbox")
        if redact is not None:
            try:
                redact = tuple(float(v) for v in redact)
                if len(redact) != 4:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError(
                    f"redact_bbox must be 4 numbers in {a!r}") from None
        out.append({
            "page": page, "x": x, "y": y, "text": text,
            "fontsize":   float(a.get("fontsize") or 11.0),
            "color":      tuple(a.get("color") or (0.0, 0.0, 0.0)),
            "bold":       bool(a.get("bold")),
            "italic":     bool(a.get("italic")),
            "font_name":  str(a.get("font_name") or ""),
            "redact_bbox": redact,
        })
    return out


def parse_image_inserts(raw: Any) -> list[dict]:
    """Validate the image-insert payload."""
    out: list[dict] = []
    for ii in raw or []:
        try:
            page = int(ii["page"])
            x = float(ii["x"])
            y = float(ii["y"])
            w = float(ii["width"])
            h = float(ii["height"])
            b64 = str(ii["image_b64"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed image insert {ii!r}: {exc}") from None
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception as exc:
            raise ValueError(f"image_b64 not valid base64: {exc}") from None
        if len(data) > _MAX_IMAGE_BYTES:
            raise ValueError(
                f"inserted image larger than {_MAX_IMAGE_BYTES // (1024*1024)} MB")
        out.append({"page": page, "rect": (x, y, x + w, y + h), "data": data})
    return out


def apply_to_editor(
    ed: PDFEditor,
    edit_tuples: list[tuple[int, tuple[float, float, float, float], str]],
    add_dicts: list[dict],
    image_inserts: list[dict] | None = None,
) -> int:
    """Apply edits, adds, and image inserts to a PDFEditor. Returns count
    successfully applied. Failures are recorded in `ed.warnings` rather than
    raising, so one bad input doesn't lose the rest of the user's work."""
    applied = 0
    if edit_tuples:
        applied += ed.replace_spans(edit_tuples)
    for a in add_dicts:
        try:
            ed.add_text(
                a["page"], (a["x"], a["y"]), a["text"],
                font_name=a["font_name"], fontsize=a["fontsize"],
                color=a["color"], bold=a["bold"], italic=a["italic"],
                redact_bbox=a.get("redact_bbox"),
            )
            applied += 1
        except Exception as e:
            ed.warnings.append(f"add_text at page {a['page']} failed: {e}")
    for ii in image_inserts or []:
        try:
            page_obj = ed.doc[ii["page"]]
            page_obj.insert_image(fitz.Rect(*ii["rect"]), stream=ii["data"])
            applied += 1
        except Exception as e:
            ed.warnings.append(
                f"insert_image at page {ii['page']} failed: {e}")
    return applied
