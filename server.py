"""Flask web UI for the PDF editor.

Dev:    uv run python server.py
Prod:   ./run-prod.sh   (gunicorn, see Procfile)

Required env vars in production:
  PDF_EDITOR_PASSWORD     shared password for the login screen
  PDF_EDITOR_SECRET_KEY   any 32+ random bytes; signs session cookies

Optional env vars:
  PORT                       gunicorn bind port (default 5050)
  PDF_SESSION_TTL_SECONDS    delete sessions older than this (default 3600)
  PDF_MAX_SESSIONS_PER_IP    upload-rate cap (default 5)
"""

from __future__ import annotations

import logging
import os
import time
import uuid

import fitz
from flask import (Flask, Response, abort, g, jsonify, render_template,
                   request, send_file)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import auth
import payload
import sessions
from pdf_editor import PDFEditor, PDFInspector


log = logging.getLogger("pdf-editor")
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)


def _rid() -> str:
    """Short request ID stashed on flask.g; safe to call outside request ctx."""
    try:
        return getattr(g, "request_id", "-")
    except RuntimeError:
        return "-"


# ---- configuration ---------------------------------------------------------

# A real prod deploy MUST set both of these. The dev fallback exists only so
# `python server.py` works locally without env wrangling. We refuse to start
# in prod if the secret is left at the default — anyone could mint cookies.
_DEV_SECRET = "dev-only-key-change-me-in-prod-CHANGE-THIS"
SECRET_KEY = os.environ.get("PDF_EDITOR_SECRET_KEY") or _DEV_SECRET

# Anything that suggests "we are running in prod" trips the safety check.
_LOOKS_LIKE_PROD = bool(
    os.environ.get("PDF_EDITOR_SECURE_COOKIE")
    or os.environ.get("FLY_APP_NAME")           # set by Fly.io
    or os.environ.get("RAILWAY_ENVIRONMENT")    # Railway
    or os.environ.get("RENDER")                 # Render
    or os.environ.get("DYNO")                   # Heroku-style
)
if _LOOKS_LIKE_PROD and SECRET_KEY == _DEV_SECRET:
    raise SystemExit(
        "FATAL: PDF_EDITOR_SECRET_KEY must be set in production. "
        "Generate with: openssl rand -hex 32"
    )
if _LOOKS_LIKE_PROD and not os.environ.get("PDF_EDITOR_PASSWORD"):
    raise SystemExit(
        "FATAL: PDF_EDITOR_PASSWORD must be set in production "
        "(running auth-less in production would expose every uploaded PDF)."
    )

SESSION_TTL = int(os.environ.get("PDF_SESSION_TTL_SECONDS") or 3600)
MAX_SESSIONS_PER_IP = int(os.environ.get("PDF_MAX_SESSIONS_PER_IP") or 5)
MAX_PAGES_PER_PDF = int(os.environ.get("PDF_MAX_PAGES") or 500)
PREVIEW_DPI = 144  # 2x of PDF's 72 DPI baseline -> retina-friendly preview


# ---- app + extensions ------------------------------------------------------

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB cap
app.config["SECRET_KEY"] = SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
if os.environ.get("PDF_EDITOR_SECURE_COOKIE"):
    app.config["SESSION_COOKIE_SECURE"] = True

# Per-endpoint rate limits — preview-edited is the expensive one; uploads
# create disk state so we cap them per IP/minute too.
limiter = Limiter(
    get_remote_address, app=app,
    default_limits=["200 per minute"],
    storage_uri="memory://",
)


# Wire auth + session lifecycle.
auth.register(app, limiter)
sessions.sweep_old(SESSION_TTL)            # one immediate pass at startup
sessions.start_sweeper(SESSION_TTL)        # then every 5 min


# ---- request-id middleware -------------------------------------------------

@app.before_request
def _attach_request_id():
    g.request_id = uuid.uuid4().hex[:8]
    g.request_started_at = time.time()


@app.after_request
def _log_request(resp):
    try:
        elapsed_ms = int((time.time() - g.request_started_at) * 1000)
    except Exception:
        elapsed_ms = -1
    log.info("[%s] %s %s -> %s (%d ms)",
             _rid(), request.method, request.path, resp.status_code, elapsed_ms)
    return resp


@app.errorhandler(Exception)
def _on_unhandled(e):
    log.exception("[%s] unhandled exception on %s %s", _rid(), request.method, request.path)
    # Werkzeug HTTPExceptions already carry the right status — bubble those.
    if hasattr(e, "code") and isinstance(getattr(e, "code", None), int):
        return jsonify({"error": getattr(e, "description", str(e))}), e.code
    return jsonify({
        "error": "internal server error",
        "request_id": _rid(),
    }), 500


@app.get("/favicon.ico")
def favicon_ico():
    """Serve the SVG favicon for /favicon.ico requests. Browsers that don't
    understand SVG favicons (very few left) will get the 302 to the SVG file
    via Flask's static handler; rasterized PNG variants are linked from the
    template head for those that explicitly want them."""
    return app.send_static_file("favicon.svg")


@app.get("/robots.txt")
def robots_txt():
    """Tell crawlers what to index. The app is auth-gated so only the login
    page is public; everything else is noise to a search engine."""
    body = (
        "User-agent: *\n"
        "Allow: /login\n"
        "Allow: /static/\n"
        "Disallow: /upload\n"
        "Disallow: /save/\n"
        "Disallow: /preview/\n"
        "Disallow: /preview-edited/\n"
        "Disallow: /download/\n"
        "Disallow: /thumb/\n"
        "Disallow: /ocr/\n"
        "Disallow: /sessions/\n"
        "Disallow: /healthz\n"
        f"\nSitemap: {request.url_root}sitemap.xml\n"
    )
    return Response(body, mimetype="text/plain")


@app.get("/sitemap.xml")
def sitemap_xml():
    """Minimal sitemap — the app is auth-gated, so only /login is public.
    Including it explicitly lets search engines treat /login as the
    canonical entry point rather than guessing."""
    base = request.url_root.rstrip("/")
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f'  <url><loc>{base}/login</loc>'
        f'<changefreq>monthly</changefreq><priority>1.0</priority></url>\n'
        '</urlset>\n'
    )
    return Response(body, mimetype="application/xml")


@app.get("/healthz")
def healthz():
    """Liveness probe — no auth, no I/O."""
    return jsonify({"ok": True})


@app.get("/")
def index():
    """Landing page for visitors, editor for signed-in users.

    `/` is in auth.PUBLIC_PATHS so the route handler runs for unauthed
    visitors — they see the marketing landing, with a Sign-in CTA that
    takes them to /login. Anyone already signed in goes straight to the
    editor. In DEV_NOAUTH mode (no password configured), the editor is
    always shown.
    """
    from flask import session
    if auth.DEV_NOAUTH or session.get("authed"):
        return render_template("index.html")
    return render_template("landing.html")


@app.post("/upload")
@limiter.limit("10 per minute")
def upload():
    file = request.files.get("pdf")
    if not file or not file.filename or not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please upload a PDF file."}), 400

    ip = get_remote_address()
    if sessions.count_for_ip(ip) >= MAX_SESSIONS_PER_IP:
        return jsonify({
            "error": (f"Too many active sessions ({MAX_SESSIONS_PER_IP}). "
                      "Wait a few minutes for old ones to expire, or click "
                      "'Open another PDF' which clears the current one.")
        }), 429

    sid = uuid.uuid4().hex
    sdir = sessions.SESSIONS_DIR / sid
    sdir.mkdir()
    pdf_path = sdir / "original.pdf"
    file.save(pdf_path)

    # Magic-byte check — anyone can name a file `*.pdf`. We're about to feed
    # it to fitz which is a large C library; reject obvious non-PDFs early.
    try:
        with pdf_path.open("rb") as f:
            head = f.read(5)
    except OSError:
        head = b""
    if not head.startswith(b"%PDF-"):
        sessions.delete(sid)
        return jsonify({
            "error": "That file does not look like a PDF (missing %PDF- header)."
        }), 400

    try:
        with PDFInspector(pdf_path) as ins:
            if ins.page_count > MAX_PAGES_PER_PDF:
                sessions.delete(sid)
                return jsonify({
                    "error": (f"PDF has {ins.page_count} pages — limit is "
                              f"{MAX_PAGES_PER_PDF}. Split the file before uploading.")
                }), 413
            pages = []
            for pno in range(ins.page_count):
                page = ins.doc[pno]
                spans = [s.to_dict() for s in ins.iter_spans(pno)]
                pages.append({
                    "page": pno,
                    "width": float(page.rect.width),
                    "height": float(page.rect.height),
                    "spans": spans,
                })
    except Exception as e:
        log.warning("[%s] could not read PDF: %s", _rid(), e)
        sessions.delete(sid)
        return jsonify({"error": f"Could not read PDF: {e}"}), 400

    sessions.track(sid, ip)
    log.info("[%s] new session %s (%d page%s, ip=%s)",
             _rid(), sid, len(pages), "" if len(pages) == 1 else "s", ip)
    return jsonify({
        "session": sid,
        "filename": file.filename,
        "pages": pages,
        "preview_dpi": PREVIEW_DPI,
    })


@app.get("/preview/<sid>/<int:page>.png")
def preview(sid: str, page: int):
    sdir = sessions.session_dir(sid)
    doc = fitz.open(sdir / "original.pdf")
    if page < 0 or page >= len(doc):
        doc.close()
        abort(404, "page out of range")
    pix = doc[page].get_pixmap(dpi=PREVIEW_DPI, alpha=False)
    png = pix.tobytes("png")
    doc.close()
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


@app.post("/ocr/<sid>/<int:page>")
@limiter.limit("5 per minute")
def ocr_page(sid: str, page: int):
    """Run OCR on a page (typically used when the page is a scanned image
    with no extractable text). Returns spans in the same shape as upload's
    page.spans so the UI can render them as editable items.

    Requires tesseract — `brew install tesseract` on macOS,
    `apt install tesseract-ocr` on Debian/Ubuntu.
    """
    sdir = sessions.session_dir(sid)
    doc = fitz.open(sdir / "original.pdf")
    if page < 0 or page >= len(doc):
        doc.close()
        abort(404, "page out of range")

    try:
        tp = doc[page].get_textpage_ocr(language="eng", dpi=200, full=True)
    except RuntimeError as e:
        doc.close()
        # Tesseract not found / not usable
        return jsonify({
            "error": ("OCR is not available on this server. Install tesseract: "
                      "`brew install tesseract` on macOS, "
                      "`apt install tesseract-ocr` on Debian/Ubuntu, "
                      f"then restart the server. ({e})")
        }), 503
    except Exception as e:
        doc.close()
        return jsonify({"error": f"OCR failed: {e}"}), 500

    spans = []
    for block in doc[page].get_text("dict", textpage=tp).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                spans.append({
                    "page": page,
                    "text": span.get("text", ""),
                    "font": "OCR",
                    "size": float(span.get("size", 11)),
                    "color": int(span.get("color", 0)),
                    "color_hex": "#000000",
                    "flags": int(span.get("flags", 0)),
                    "bbox": list(span.get("bbox", [0, 0, 0, 0])),
                    "origin": list(span.get("origin", [0, 0])),
                    "ascender": float(span.get("ascender", 0.0)),
                    "descender": float(span.get("descender", 0.0)),
                    "style": "ocr",
                    "is_ocr": True,
                })
    doc.close()
    return jsonify({"spans": spans})


@app.get("/thumb/<sid>/<int:page>.png")
def thumb(sid: str, page: int):
    """Tiny rendering of a page for the sidebar thumbnails strip."""
    sdir = sessions.session_dir(sid)
    doc = fitz.open(sdir / "original.pdf")
    if page < 0 or page >= len(doc):
        doc.close()
        abort(404, "page out of range")
    # 36 DPI = exactly 50% the natural PDF size; ~150px tall page is enough.
    pix = doc[page].get_pixmap(dpi=36, alpha=False)
    png = pix.tobytes("png")
    doc.close()
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=300"})



@app.post("/save/<sid>")
@limiter.limit("20 per minute")
def save(sid: str):
    sdir = sessions.session_dir(sid)
    body = request.get_json(silent=True) or {}
    raw_edits  = body.get("edits")  or []
    raw_adds   = body.get("adds")   or []
    raw_images = body.get("images") or []
    if not raw_edits and not raw_adds and not raw_images:
        return jsonify({"error": "no edits to apply"}), 400

    try:
        edit_tuples   = payload.parse_edits(raw_edits)
        add_dicts     = payload.parse_adds(raw_adds)
        image_inserts = payload.parse_image_inserts(raw_images)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    out_path = sdir / "edited.pdf"
    try:
        with PDFEditor(sdir / "original.pdf") as ed:
            applied = payload.apply_to_editor(ed, edit_tuples, add_dicts, image_inserts)
            ed.save(out_path)
            warnings = list(ed.warnings)
    except Exception as e:
        log.exception("[%s] save failed for session %s", _rid(), sid)
        return jsonify({
            "error": f"Save failed while applying edits: {e}",
            "request_id": _rid(),
        }), 500

    return jsonify({
        "applied": applied,
        "requested": len(edit_tuples) + len(add_dicts) + len(image_inserts),
        "warnings": warnings,
        "download_url": f"/download/{sid}",
    })


@app.post("/preview-edited/<sid>/<int:page>.png")
@limiter.limit("60 per minute")
def preview_edited(sid: str, page: int):
    """Apply pending edits in memory and return a PNG of the requested page.

    The frontend hits this whenever the user types so they can see the result
    without downloading. We do NOT save edited.pdf — this is a preview only.
    """
    sdir = sessions.session_dir(sid)
    body = request.get_json(silent=True) or {}
    raw_edits  = body.get("edits")  or []
    raw_adds   = body.get("adds")   or []
    raw_images = body.get("images") or []

    try:
        edit_tuples   = payload.parse_edits(raw_edits)
        add_dicts     = payload.parse_adds(raw_adds)
        image_inserts = payload.parse_image_inserts(raw_images)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        with PDFEditor(sdir / "original.pdf") as ed:
            payload.apply_to_editor(ed, edit_tuples, add_dicts, image_inserts)
            if page < 0 or page >= len(ed.doc):
                return jsonify({"error": "page out of range"}), 404
            pix = ed.doc[page].get_pixmap(dpi=PREVIEW_DPI, alpha=False)
            png = pix.tobytes("png")
    except Exception as e:
        log.warning("[%s] preview-edited failed for session %s: %s",
                    _rid(), sid, e)
        return jsonify({"error": f"Preview failed: {e}",
                        "request_id": _rid()}), 500

    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/download/<sid>")
def download(sid: str):
    sdir = sessions.session_dir(sid)
    edited = sdir / "edited.pdf"
    if not edited.exists():
        abort(404, "no edited PDF for this session yet")
    return send_file(edited, as_attachment=True,
                     download_name="edited.pdf",
                     mimetype="application/pdf")


@app.post("/sessions/<sid>")
def session_delete(sid: str):
    """Delete a session early (called when user clicks 'Open another PDF')."""
    sessions.delete(sid)
    return jsonify({"deleted": True})






if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
