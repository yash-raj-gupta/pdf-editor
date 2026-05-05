"""Flask web UI for the PDF editor.

Run:  uv run python server.py
Then open http://127.0.0.1:5050 in a browser.

Endpoints:
  GET  /                       single-page UI
  POST /upload                 multipart PDF upload, returns spans + page sizes
  GET  /preview/<sid>/<n>.png  render page n of session sid as a PNG
  POST /save/<sid>             apply edits, write edited.pdf, return URL
  GET  /download/<sid>         serve edited.pdf as an attachment
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import fitz
from flask import Flask, Response, abort, jsonify, request, send_file

from pdf_editor import PDFEditor, PDFInspector

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB cap

SESSIONS_DIR = Path(tempfile.gettempdir()) / "pdf-editor-sessions"
SESSIONS_DIR.mkdir(exist_ok=True)
PREVIEW_DPI = 144  # 2x of PDF's 72 DPI baseline -> retina-friendly preview


def session_dir(sid: str) -> Path:
    """Validate the session id and return its directory or 404/400."""
    if len(sid) != 32 or any(c not in "0123456789abcdef" for c in sid):
        abort(400, "invalid session id")
    p = SESSIONS_DIR / sid
    if not p.is_dir():
        abort(404, "session not found")
    return p


@app.get("/")
def index() -> Response:
    return Response(INDEX_HTML, mimetype="text/html; charset=utf-8")


@app.post("/upload")
def upload():
    file = request.files.get("pdf")
    if not file or not file.filename or not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please upload a PDF file."}), 400

    sid = uuid.uuid4().hex
    sdir = SESSIONS_DIR / sid
    sdir.mkdir()
    pdf_path = sdir / "original.pdf"
    file.save(pdf_path)

    try:
        with PDFInspector(pdf_path) as ins:
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
        return jsonify({"error": f"Could not read PDF: {e}"}), 400

    return jsonify({
        "session": sid,
        "filename": file.filename,
        "pages": pages,
        "preview_dpi": PREVIEW_DPI,
    })


@app.get("/preview/<sid>/<int:page>.png")
def preview(sid: str, page: int):
    sdir = session_dir(sid)
    doc = fitz.open(sdir / "original.pdf")
    if page < 0 or page >= len(doc):
        doc.close()
        abort(404, "page out of range")
    pix = doc[page].get_pixmap(dpi=PREVIEW_DPI, alpha=False)
    png = pix.tobytes("png")
    doc.close()
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


def _parse_edits(raw_edits) -> list[tuple[int, tuple[float, float, float, float], str]]:
    """Validate and convert the JSON edits payload to typed tuples."""
    out: list[tuple[int, tuple[float, float, float, float], str]] = []
    for e in raw_edits:
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


@app.post("/save/<sid>")
def save(sid: str):
    sdir = session_dir(sid)
    body = request.get_json(silent=True) or {}
    raw_edits = body.get("edits") or []
    if not raw_edits:
        return jsonify({"error": "no edits to apply"}), 400

    try:
        edit_tuples = _parse_edits(raw_edits)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    out_path = sdir / "edited.pdf"
    with PDFEditor(sdir / "original.pdf") as ed:
        applied = ed.replace_spans(edit_tuples)
        ed.save(out_path)
        warnings = list(ed.warnings)

    return jsonify({
        "applied": applied,
        "requested": len(edit_tuples),
        "warnings": warnings,
        "download_url": f"/download/{sid}",
    })


@app.post("/preview-edited/<sid>/<int:page>.png")
def preview_edited(sid: str, page: int):
    """Apply pending edits in memory and return a PNG of the requested page.

    The frontend hits this whenever the user types so they can see the result
    without downloading. We do NOT save edited.pdf — this is a preview only.
    """
    sdir = session_dir(sid)
    body = request.get_json(silent=True) or {}
    raw_edits = body.get("edits") or []

    try:
        edit_tuples = _parse_edits(raw_edits)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    with PDFEditor(sdir / "original.pdf") as ed:
        if edit_tuples:
            ed.replace_spans(edit_tuples)
        if page < 0 or page >= len(ed.doc):
            return jsonify({"error": "page out of range"}), 404
        pix = ed.doc[page].get_pixmap(dpi=PREVIEW_DPI, alpha=False)
        png = pix.tobytes("png")

    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/download/<sid>")
def download(sid: str):
    sdir = session_dir(sid)
    edited = sdir / "edited.pdf"
    if not edited.exists():
        abort(404, "no edited PDF for this session yet")
    return send_file(edited, as_attachment=True,
                     download_name="edited.pdf",
                     mimetype="application/pdf")


# ----------------------------------------------------------------------------
# Single-page UI — vanilla JS, no framework, no build step.
# ----------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PDF Editor</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    color: #1d1d1f; background: #f5f5f7; display: flex; flex-direction: column;
  }
  header {
    background: white; border-bottom: 1px solid #e5e5e7;
    padding: 12px 20px; display: flex; align-items: center; gap: 14px;
    flex-shrink: 0;
  }
  header h1 { font-size: 17px; font-weight: 600; }
  header .file-info { color: #86868b; font-size: 13px; flex: 1; }

  .btn {
    background: #007aff; color: white; border: none; padding: 8px 16px;
    border-radius: 7px; font-size: 14px; cursor: pointer; font-weight: 500;
    transition: background 0.1s;
  }
  .btn:hover:not(:disabled) { background: #0066d6; }
  .btn:disabled { background: #c7c7cc; cursor: not-allowed; }
  .btn.secondary { background: #f0f0f3; color: #1d1d1f; }
  .btn.secondary:hover:not(:disabled) { background: #e5e5e7; }

  /* Upload pane */
  #upload-pane {
    flex: 1; display: flex; align-items: center; justify-content: center;
    padding: 40px;
  }
  .drop-zone {
    background: white; border: 2px dashed #c7c7cc; border-radius: 14px;
    padding: 56px 80px; text-align: center; max-width: 520px;
    transition: all 0.15s; cursor: pointer;
  }
  .drop-zone:hover, .drop-zone.dragover {
    border-color: #007aff; background: #f0f7ff;
  }
  .drop-zone h2 { font-size: 22px; font-weight: 600; margin-bottom: 8px; }
  .drop-zone p { color: #86868b; margin-bottom: 22px; font-size: 14px; }
  .drop-zone input[type="file"] { display: none; }
  .alert { padding: 10px 14px; border-radius: 6px; font-size: 13px; margin-top: 16px; }
  .alert.error { background: #ffebee; color: #c62828; }
  .alert.success { background: #e8f5e9; color: #2e7d32; }

  /* Editor pane */
  #editor-pane {
    flex: 1; display: grid; grid-template-columns: minmax(0, 1fr) 420px;
    gap: 14px; padding: 14px; min-height: 0;
  }

  .panel {
    background: white; border-radius: 10px; padding: 14px;
    display: flex; flex-direction: column; min-height: 0;
  }

  .page-tabs {
    display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; flex-shrink: 0;
  }
  .page-tab {
    padding: 5px 11px; border-radius: 6px; background: #f0f0f3;
    cursor: pointer; font-size: 13px; user-select: none;
  }
  .page-tab.active { background: #007aff; color: white; }

  .preview-scroll { flex: 1; overflow: auto; display: flex; justify-content: center; }
  .preview-canvas { position: relative; display: inline-block; }
  .preview-canvas img {
    display: block; max-width: 100%; height: auto;
    box-shadow: 0 1px 4px rgba(0,0,0,0.08);
    transition: opacity 0.15s;
  }
  .preview-canvas.loading img { opacity: 0.55; }
  .preview-spinner {
    position: absolute; top: 14px; right: 14px; padding: 4px 10px;
    border-radius: 99px; background: rgba(0,0,0,0.65); color: white;
    font-size: 11px; font-weight: 500; opacity: 0;
    transition: opacity 0.15s; pointer-events: none;
  }
  .preview-canvas.loading .preview-spinner { opacity: 1; }
  .preview-stale-banner {
    background: #fff8e7; color: #7a5800; border: 1px solid #ffd66b;
    padding: 6px 10px; border-radius: 6px; font-size: 12px;
    margin-bottom: 8px; display: none;
  }
  .preview-stale-banner.visible { display: block; }
  .span-overlay {
    position: absolute; border: 1px solid transparent; cursor: pointer;
    transition: background 0.1s, border-color 0.1s;
  }
  .span-overlay:hover {
    background: rgba(0, 122, 255, 0.12);
    border-color: rgba(0, 122, 255, 0.45);
  }
  .span-overlay.active {
    background: rgba(0, 122, 255, 0.18);
    border-color: #007aff;
  }
  .span-overlay.dirty {
    background: rgba(255, 159, 10, 0.15);
    border-color: rgba(255, 159, 10, 0.7);
  }

  .spans-header {
    display: flex; align-items: baseline; justify-content: space-between;
    margin-bottom: 10px; flex-shrink: 0;
  }
  .spans-header h3 {
    font-size: 12px; text-transform: uppercase; color: #86868b;
    letter-spacing: 0.6px; font-weight: 600;
  }
  .spans-header .count { font-size: 12px; color: #86868b; }

  .spans-list { flex: 1; overflow-y: auto; }
  .span-card {
    padding: 9px 11px; border-radius: 7px; border: 1px solid transparent;
    margin-bottom: 6px; transition: all 0.1s;
  }
  .span-card:hover { background: #fafafa; }
  .span-card.active { border-color: #007aff; background: #f0f7ff; }
  .span-card.dirty { border-color: #ff9f0a; background: #fff8e7; }
  .span-meta {
    font-size: 11px; color: #86868b; margin-bottom: 5px;
    display: flex; align-items: center; gap: 6px; font-feature-settings: "tnum";
  }
  .color-swatch {
    width: 11px; height: 11px; border-radius: 2px;
    border: 1px solid rgba(0,0,0,0.1); display: inline-block; flex-shrink: 0;
  }
  .span-input {
    width: 100%; padding: 6px 9px; border: 1px solid #d1d1d6;
    border-radius: 5px; font-size: 13px; font-family: inherit;
    background: white;
  }
  .span-input:focus { outline: none; border-color: #007aff; }

  .toolbar {
    display: flex; gap: 10px; align-items: center;
    padding-top: 12px; border-top: 1px solid #e5e5e7; margin-top: 10px;
    flex-shrink: 0;
  }
  .toolbar .status { color: #86868b; font-size: 13px; flex: 1; }
  .toolbar .status.error { color: #c62828; }
  .toolbar .status.success { color: #2e7d32; }
</style>
</head>
<body>

<header>
  <h1>PDF Editor</h1>
  <span class="file-info" id="file-info">Inspect, edit and download PDFs while keeping the original look.</span>
  <button class="btn secondary" id="new-pdf-btn" hidden>Open another PDF</button>
</header>

<main id="upload-pane">
  <div class="drop-zone" id="drop-zone">
    <h2>Drop a PDF here</h2>
    <p>or click to browse</p>
    <input type="file" id="file-input" accept="application/pdf">
    <button class="btn" id="browse-btn">Choose PDF</button>
    <div class="alert error" id="upload-error" hidden></div>
  </div>
</main>

<main id="editor-pane" hidden>
  <section class="panel preview-section">
    <div class="page-tabs" id="page-tabs"></div>
    <div class="preview-stale-banner" id="preview-banner">
      Showing original — preview is updating&hellip;
    </div>
    <div class="preview-scroll">
      <div class="preview-canvas" id="preview-canvas">
        <img id="page-img" alt="">
        <div class="preview-spinner">updating preview&hellip;</div>
      </div>
    </div>
  </section>

  <section class="panel spans-section">
    <div class="spans-header">
      <h3>Editable text on this page</h3>
      <span class="count" id="span-count"></span>
    </div>
    <div class="spans-list" id="spans-list"></div>
    <div class="toolbar">
      <span class="status" id="save-status">No changes yet</span>
      <button class="btn" id="save-btn" disabled>Save &amp; Download</button>
    </div>
  </section>
</main>

<script>
"use strict";

const state = {
  session: null,
  pages: [],
  currentPage: 0,
  edits: new Map(),  // key = "page:bbox" -> {page, bbox, new_text}
  previewDpi: 144,
};

// ---------- upload ----------
const dropZone   = document.getElementById('drop-zone');
const fileInput  = document.getElementById('file-input');
const browseBtn  = document.getElementById('browse-btn');
const uploadErr  = document.getElementById('upload-error');

browseBtn.onclick = (e) => { e.stopPropagation(); fileInput.click(); };
dropZone.onclick = () => fileInput.click();
fileInput.onchange = () => fileInput.files[0] && uploadFile(fileInput.files[0]);

['dragenter', 'dragover'].forEach(ev => dropZone.addEventListener(ev, (e) => {
  e.preventDefault(); dropZone.classList.add('dragover');
}));
['dragleave', 'drop'].forEach(ev => dropZone.addEventListener(ev, (e) => {
  e.preventDefault(); dropZone.classList.remove('dragover');
}));
dropZone.addEventListener('drop', (e) => {
  e.preventDefault();
  const f = e.dataTransfer.files[0];
  if (f) uploadFile(f);
});

async function uploadFile(file) {
  uploadErr.hidden = true;
  if (!file.name.toLowerCase().endsWith('.pdf')) {
    return showError("That doesn't look like a PDF.");
  }
  const fd = new FormData();
  fd.append('pdf', file);
  try {
    const r = await fetch('/upload', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) return showError(data.error || 'Upload failed');
    loadEditor(data, file.name);
  } catch (e) {
    showError('Network error: ' + e.message);
  }
}
function showError(msg) { uploadErr.textContent = msg; uploadErr.hidden = false; }

// ---------- editor boot ----------
function loadEditor(data, filename) {
  state.session    = data.session;
  state.pages      = data.pages;
  state.previewDpi = data.preview_dpi;
  state.currentPage = 0;
  state.edits = new Map();

  document.getElementById('file-info').textContent =
    `${filename}  ·  ${data.pages.length} page${data.pages.length === 1 ? '' : 's'}`;
  document.getElementById('upload-pane').hidden = true;
  document.getElementById('editor-pane').hidden = false;
  document.getElementById('new-pdf-btn').hidden = false;

  renderPageTabs();
  renderPage(0);
}

function renderPageTabs() {
  const wrap = document.getElementById('page-tabs');
  wrap.innerHTML = '';
  if (state.pages.length <= 1) return;
  state.pages.forEach((p, i) => {
    const tab = document.createElement('div');
    tab.className = 'page-tab' + (i === state.currentPage ? ' active' : '');
    tab.textContent = `Page ${i + 1}`;
    tab.onclick = () => renderPage(i);
    wrap.appendChild(tab);
  });
}

function renderPage(pno) {
  state.currentPage = pno;
  renderPageTabs();
  const page = state.pages[pno];

  // Render spans list once per page change. The image src may swap many times
  // (each preview update) — only overlays should re-render on img load.
  renderSpansList(page);

  const img = document.getElementById('page-img');
  img.onload = () => renderOverlays(page, img);

  // If we have edits for this page, show the edited preview right away;
  // otherwise show the original.
  if (hasEditsOnPage(pno)) {
    requestPreviewUpdate(true);
  } else {
    setPageImageSrc(`/preview/${state.session}/${pno}.png?t=${Date.now()}`);
  }
}

function hasEditsOnPage(pno) {
  for (const e of state.edits.values()) if (e.page === pno) return true;
  return false;
}

// Track the current blob URL so we can revoke it when swapping to a new one.
let currentPreviewBlobUrl = null;
function setPageImageSrc(src) {
  const img = document.getElementById('page-img');
  if (currentPreviewBlobUrl) {
    URL.revokeObjectURL(currentPreviewBlobUrl);
    currentPreviewBlobUrl = null;
  }
  img.src = src;
}
function setPageImageBlob(blob) {
  const url = URL.createObjectURL(blob);
  if (currentPreviewBlobUrl) URL.revokeObjectURL(currentPreviewBlobUrl);
  currentPreviewBlobUrl = url;
  document.getElementById('page-img').src = url;
}

function spanKey(span) {
  return span.page + ':' + span.bbox.map(x => x.toFixed(2)).join(',');
}

// ---------- overlays on the rendered page ----------
function renderOverlays(page, img) {
  const canvas = document.getElementById('preview-canvas');
  canvas.querySelectorAll('.span-overlay').forEach(el => el.remove());

  const scale = img.clientWidth / page.width;
  page.spans.forEach((span, idx) => {
    const div = document.createElement('div');
    div.className = 'span-overlay';
    if (state.edits.has(spanKey(span))) div.classList.add('dirty');
    div.dataset.idx = idx;
    const [x0, y0, x1, y1] = span.bbox;
    div.style.left   = (x0 * scale) + 'px';
    div.style.top    = (y0 * scale) + 'px';
    div.style.width  = ((x1 - x0) * scale) + 'px';
    div.style.height = ((y1 - y0) * scale) + 'px';
    div.title = span.text;
    div.onclick = () => focusSpan(idx);
    canvas.appendChild(div);
  });
}

// Re-position overlays on window resize so they stay aligned with the image.
let resizeTimer = null;
window.addEventListener('resize', () => {
  if (resizeTimer) clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (!state.session) return;
    const img = document.getElementById('page-img');
    if (img.complete) renderOverlays(state.pages[state.currentPage], img);
  }, 80);
});

// ---------- spans list ----------
function renderSpansList(page) {
  const list = document.getElementById('spans-list');
  list.innerHTML = '';
  document.getElementById('span-count').textContent =
    `${page.spans.length} span${page.spans.length === 1 ? '' : 's'}`;

  // Sort spans by reading order: top-to-bottom, then left-to-right.
  const ordered = page.spans.map((s, i) => ({s, i})).sort((a, b) => {
    if (Math.abs(a.s.bbox[1] - b.s.bbox[1]) > 4) return a.s.bbox[1] - b.s.bbox[1];
    return a.s.bbox[0] - b.s.bbox[0];
  });

  ordered.forEach(({s: span, i: idx}) => {
    const card = document.createElement('div');
    card.className = 'span-card';
    card.dataset.idx = idx;
    const k = spanKey(span);
    if (state.edits.has(k)) card.classList.add('dirty');

    const meta = document.createElement('div');
    meta.className = 'span-meta';
    const swatch = document.createElement('span');
    swatch.className = 'color-swatch';
    swatch.style.background = span.color_hex;
    meta.appendChild(swatch);
    const label = document.createElement('span');
    label.textContent =
      `${span.font} · ${span.size}pt · ${span.color_hex} · ${span.style}`;
    meta.appendChild(label);

    const input = document.createElement('input');
    input.className = 'span-input';
    input.type = 'text';
    input.value = state.edits.get(k)?.new_text ?? span.text;
    input.onfocus = () => activateSpan(idx);
    input.oninput = (e) => recordEdit(span, e.target.value, card);

    card.appendChild(meta);
    card.appendChild(input);
    card.onclick = (e) => { if (e.target === card) activateSpan(idx); };
    list.appendChild(card);
  });
  updateSaveButton();
}

function activateSpan(idx) {
  document.querySelectorAll('.span-overlay.active, .span-card.active')
    .forEach(el => el.classList.remove('active'));
  document.querySelectorAll(`[data-idx="${idx}"]`)
    .forEach(el => el.classList.add('active'));
  const card = document.querySelector(`.span-card[data-idx="${idx}"]`);
  if (card) card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}
function focusSpan(idx) {
  activateSpan(idx);
  const input = document.querySelector(`.span-card[data-idx="${idx}"] .span-input`);
  if (input) { input.focus(); input.select(); }
}

function recordEdit(span, newText, card) {
  const k = spanKey(span);
  const overlay = document.querySelector(
    `.span-overlay[data-idx="${card.dataset.idx}"]`);
  if (newText === span.text) {
    state.edits.delete(k);
    card.classList.remove('dirty');
    overlay && overlay.classList.remove('dirty');
  } else {
    state.edits.set(k, { page: span.page, bbox: span.bbox, new_text: newText });
    card.classList.add('dirty');
    overlay && overlay.classList.add('dirty');
  }
  updateSaveButton();
  requestPreviewUpdate();
}

// ---------- live preview ----------
let previewTimer = null;
let previewController = null;
let previewSeq = 0;  // monotonic — used to ignore stale responses

function requestPreviewUpdate(immediate = false) {
  if (previewTimer) clearTimeout(previewTimer);
  previewTimer = setTimeout(updatePreview, immediate ? 0 : 600);
}

async function updatePreview() {
  const pno = state.currentPage;
  const canvas = document.getElementById('preview-canvas');
  // Send only edits relevant to *any* page — server applies all then renders
  // the requested page. Sending all is fine; same payload as save.
  const edits = Array.from(state.edits.values());

  if (edits.length === 0) {
    // No edits anywhere — show the cached original PNG.
    canvas.classList.remove('loading');
    setPageImageSrc(`/preview/${state.session}/${pno}.png?t=${Date.now()}`);
    return;
  }

  // Cancel a still-in-flight previous request.
  if (previewController) previewController.abort();
  previewController = new AbortController();
  const mySeq = ++previewSeq;

  canvas.classList.add('loading');
  try {
    const r = await fetch(
      `/preview-edited/${state.session}/${pno}.png`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ edits }),
        signal: previewController.signal,
      }
    );
    if (mySeq !== previewSeq) return;  // a newer request started
    if (!r.ok) {
      console.error('preview failed', r.status);
      canvas.classList.remove('loading');
      return;
    }
    const blob = await r.blob();
    if (mySeq !== previewSeq) return;
    setPageImageBlob(blob);
  } catch (e) {
    if (e.name !== 'AbortError') console.error('preview error', e);
  } finally {
    if (mySeq === previewSeq) canvas.classList.remove('loading');
  }
}

function updateSaveButton() {
  const btn = document.getElementById('save-btn');
  const status = document.getElementById('save-status');
  status.className = 'status';
  const n = state.edits.size;
  btn.disabled = n === 0;
  status.textContent = n === 0 ? 'No changes yet'
    : `${n} edit${n === 1 ? '' : 's'} pending`;
}

// ---------- save & download ----------
document.getElementById('save-btn').onclick = async () => {
  const btn = document.getElementById('save-btn');
  const status = document.getElementById('save-status');
  btn.disabled = true;
  status.className = 'status';
  status.textContent = 'Saving...';

  try {
    const r = await fetch(`/save/${state.session}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ edits: Array.from(state.edits.values()) }),
    });
    const data = await r.json();
    if (!r.ok) {
      status.classList.add('error');
      status.textContent = 'Error: ' + (data.error || 'save failed');
      btn.disabled = false;
      return;
    }
    // Trigger download without navigating away.
    const a = document.createElement('a');
    a.href = data.download_url;
    a.download = 'edited.pdf';
    document.body.appendChild(a);
    a.click();
    a.remove();

    status.classList.add('success');
    let msg = `Applied ${data.applied} of ${data.requested} edit(s). Downloading…`;
    if (data.warnings && data.warnings.length) {
      msg += ` (${data.warnings.length} warning${data.warnings.length === 1 ? '' : 's'})`;
    }
    status.textContent = msg;
    btn.disabled = false;
  } catch (e) {
    status.classList.add('error');
    status.textContent = 'Network error: ' + e.message;
    btn.disabled = false;
  }
};

document.getElementById('new-pdf-btn').onclick = () => location.reload();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
