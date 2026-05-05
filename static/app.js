"use strict";

const state = {
  session: null,
  filename: null,
  pages: [],
  currentPage: 0,
  edits: new Map(),     // key = "page:bbox" -> {page, bbox, new_text, original}
  adds: [],             // [{page, x, y, text, fontsize, color, bold, italic, font_name}]
  imageInserts: [],     // [{page, x, y, width, height, image_b64, dataUrl}]
  previewDpi: 144,
  zoom: 1.0,            // 1.0 = fit-to-container, otherwise multiplier
  viewMode: 'edited',   // 'edited' or 'original' (compare toggle)
  history: [],          // undo stack: each entry is {edits: Map, adds: Array}
  historyIdx: -1,
  searchQuery: '',
};

// ---------- theme (dark mode) ----------
function applyTheme(theme) {
  if (theme === 'dark' || theme === 'light') {
    document.body.dataset.theme = theme;
  } else {
    delete document.body.dataset.theme;  // follow OS preference
  }
}
applyTheme(localStorage.getItem('pdf-editor-theme') || 'auto');
document.getElementById('theme-toggle').onclick = () => {
  const cur = document.body.dataset.theme;
  const next = cur === 'dark' ? 'light' : (cur === 'light' ? 'auto' : 'dark');
  if (next === 'auto') {
    localStorage.removeItem('pdf-editor-theme');
    applyTheme('auto');
  } else {
    localStorage.setItem('pdf-editor-theme', next);
    applyTheme(next);
  }
};

// ---------- mobile pane toggle ----------
function setMobilePane(which) {
  document.body.dataset.mobilePane = which;
  document.getElementById('mob-tab-preview').classList.toggle('active', which === 'preview');
  document.getElementById('mob-tab-edit').classList.toggle('active', which === 'edit');
}
document.body.dataset.mobilePane = 'preview';
document.getElementById('mob-tab-preview').onclick = () => setMobilePane('preview');
document.getElementById('mob-tab-edit').onclick    = () => setMobilePane('edit');

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
  state.filename   = filename;
  state.pages      = data.pages;
  state.previewDpi = data.preview_dpi;
  state.currentPage = 0;
  state.edits = new Map();
  state.adds = [];
  state.imageInserts = [];
  state.history = [{edits: new Map(), adds: [], images: []}];
  state.historyIdx = 0;
  state.searchQuery = '';

  document.getElementById('file-info').textContent =
    `${filename}  ·  ${data.pages.length} page${data.pages.length === 1 ? '' : 's'}`;
  document.getElementById('upload-pane').hidden = true;
  document.getElementById('editor-pane').hidden = false;
  document.getElementById('new-pdf-btn').hidden = false;
  document.getElementById('mobile-tabs').hidden = false;
  document.getElementById('span-search').value = '';
  document.getElementById('diff-summary').hidden = true;

  // Switch on the multi-page rail layout when it'll actually be used.
  if (data.pages.length > 1) {
    document.body.dataset.multipage = '1';
  } else {
    delete document.body.dataset.multipage;
  }

  renderThumbnails();
  renderPageTabs();
  renderPage(0);
}

function renderThumbnails() {
  const rail = document.getElementById('thumb-rail');
  rail.innerHTML = '';
  if (state.pages.length <= 1) return;
  state.pages.forEach((p, i) => {
    const wrap = document.createElement('div');
    wrap.className = 'thumb' + (i === state.currentPage ? ' active' : '');
    wrap.dataset.page = i;
    const img = document.createElement('img');
    img.src = `/thumb/${state.session}/${i}.png`;
    img.alt = `page ${i + 1}`;
    img.loading = 'lazy';
    const label = document.createElement('div');
    label.className = 'thumb-label';
    label.textContent = `Page ${i + 1}`;
    wrap.appendChild(img);
    wrap.appendChild(label);
    wrap.onclick = () => renderPage(i);
    rail.appendChild(wrap);
  });
}

function updateThumbnailActive() {
  document.querySelectorAll('.thumb').forEach((el) => {
    el.classList.toggle('active',
      parseInt(el.dataset.page, 10) === state.currentPage);
  });
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
  updateThumbnailActive();
  const page = state.pages[pno];

  // Show the OCR prompt when this page has no extractable text yet.
  const showOcr = (page.spans.length === 0);
  document.getElementById('ocr-prompt').hidden = !showOcr;
  document.getElementById('ocr-status').textContent = '';

  // Render spans list once per page change. The image src may swap many times
  // (each preview update) — only overlays should re-render on img load.
  renderSpansList(page);

  const img = document.getElementById('page-img');
  img.onload = () => { applyZoom(); renderOverlays(page, img); };

  // If we have edits for this page and we're in edited mode, show the edited
  // preview right away; otherwise show the original.
  if (state.viewMode === 'edited' && hasEditsOnPage(pno)) {
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
  canvas.querySelectorAll('.span-overlay, .add-overlay, .image-overlay')
        .forEach(el => el.remove());

  const scale = img.clientWidth / page.width;

  // Existing-span overlays
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

  // Pending-add overlays (only on this page) — drag to move, click to delete
  state.adds.forEach((a, idx) => {
    if (a.page !== state.currentPage) return;
    const div = document.createElement('div');
    div.className = 'add-overlay';
    div.dataset.addIdx = idx;
    div.style.left   = (a.x * scale) + 'px';
    div.style.top    = ((a.y - a.fontsize) * scale) + 'px';
    div.style.width  = (Math.max(a.text.length * a.fontsize * 0.5, 30) * scale) + 'px';
    div.style.height = (a.fontsize * 1.4 * scale) + 'px';
    div.title = `${a.text}\n(drag to move; click to remove)`;
    bindDraggable(div, 'add', idx, a.text);
    canvas.appendChild(div);
  });

  // Pending image-insert overlays (only on this page) — drag to move, click to delete
  state.imageInserts.forEach((ii, idx) => {
    if (ii.page !== state.currentPage) return;
    const div = document.createElement('div');
    div.className = 'image-overlay';
    div.dataset.imageIdx = idx;
    div.style.left   = (ii.x * scale) + 'px';
    div.style.top    = (ii.y * scale) + 'px';
    div.style.width  = (ii.width  * scale) + 'px';
    div.style.height = (ii.height * scale) + 'px';
    if (ii.dataUrl) {
      const inner = document.createElement('img');
      inner.src = ii.dataUrl;
      inner.draggable = false;  // prevent native HTML5 drag-image behavior
      div.appendChild(inner);
    }
    div.title = '(drag to move; click to remove)';
    bindDraggable(div, 'image', idx, 'this inserted image');
    canvas.appendChild(div);
  });
}

// ---------- drag-to-move for add-text and image overlays ----------
//
// One global listener pair on the document handles all overlays. We
// distinguish click-without-movement (delete) from drag (reposition) using a
// 4-pixel threshold; below that we treat as a click.

let _dragState = null;

function bindDraggable(el, kind, idx, label) {
  el.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;  // left button only
    e.preventDefault();
    e.stopPropagation();
    _dragState = {
      el, kind, idx, label,
      startMouseX: e.clientX,
      startMouseY: e.clientY,
      startElemLeft: parseFloat(el.style.left)  || 0,
      startElemTop:  parseFloat(el.style.top)   || 0,
      moved: false,
    };
    el.style.userSelect = 'none';
  });
}

document.addEventListener('mousemove', (e) => {
  const ds = _dragState;
  if (!ds) return;
  const dx = e.clientX - ds.startMouseX;
  const dy = e.clientY - ds.startMouseY;
  if (!ds.moved && Math.hypot(dx, dy) > 3) ds.moved = true;
  if (ds.moved) {
    ds.el.style.left = (ds.startElemLeft + dx) + 'px';
    ds.el.style.top  = (ds.startElemTop  + dy) + 'px';
  }
});

document.addEventListener('mouseup', (e) => {
  const ds = _dragState;
  if (!ds) return;
  _dragState = null;
  ds.el.style.userSelect = '';

  if (!ds.moved) {
    // Click without movement -> delete the overlay (existing behaviour).
    if (!confirm(`Remove ${ds.kind === 'add' ? `added text "${ds.label}"` : ds.label}?`)) {
      return;
    }
    if (ds.kind === 'add')   state.adds.splice(ds.idx, 1);
    else                     state.imageInserts.splice(ds.idx, 1);
    pushHistory();
    const page = state.pages[state.currentPage];
    renderOverlays(page, document.getElementById('page-img'));
    updateSaveButton();
    requestPreviewUpdate(true);
    return;
  }

  // Drag committed -> convert pixel deltas to PDF deltas and update state.
  const img = document.getElementById('page-img');
  const page = state.pages[state.currentPage];
  const scale = img.clientWidth / page.width;
  const dx_pdf = (e.clientX - ds.startMouseX) / scale;
  const dy_pdf = (e.clientY - ds.startMouseY) / scale;

  if (ds.kind === 'add') {
    state.adds[ds.idx].x += dx_pdf;
    state.adds[ds.idx].y += dy_pdf;
  } else {
    state.imageInserts[ds.idx].x += dx_pdf;
    state.imageInserts[ds.idx].y += dy_pdf;
  }
  pushHistory();
  renderOverlays(page, img);   // snap to PDF-coord-derived position
  requestPreviewUpdate(true);
});

// Re-apply zoom (which also re-renders overlays) on window resize so the
// image and bbox overlays stay aligned with the new container width.
let resizeTimer = null;
window.addEventListener('resize', () => {
  if (resizeTimer) clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (!state.session) return;
    const img = document.getElementById('page-img');
    if (img.complete) applyZoom();
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
  applySearchFilter();
  updateSaveButton();
}

// ---------- search / filter ----------
function applySearchFilter() {
  const q = state.searchQuery.toLowerCase();
  const cards = document.querySelectorAll('.span-card');
  let visible = 0;
  cards.forEach((card) => {
    const idx = parseInt(card.dataset.idx, 10);
    const span = state.pages[state.currentPage].spans[idx];
    const editedText = state.edits.get(spanKey(span))?.new_text ?? span.text;
    const haystack = `${span.text} ${editedText} ${span.font}`.toLowerCase();
    const match = !q || haystack.includes(q);
    card.classList.toggle('hidden-by-search', !match);
    if (match) visible++;
  });
  const el = document.getElementById('span-count');
  const total = state.pages[state.currentPage].spans.length;
  el.textContent = q ? `${visible} of ${total} match` :
    `${total} span${total === 1 ? '' : 's'}`;
}

document.getElementById('span-search').oninput = (e) => {
  state.searchQuery = e.target.value;
  applySearchFilter();
};

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
    // OCR'd spans need extra metadata so the save flow can re-emit them as
    // add_text-with-redaction (since they're not in the PDF's text content,
    // only on the underlying scanned image).
    state.edits.set(k, {
      page: span.page,
      bbox: span.bbox,
      new_text: newText,
      original: span.text,
      is_ocr: !!span.is_ocr,
      origin: span.origin,
      size: span.size,
    });
    card.classList.add('dirty');
    overlay && overlay.classList.add('dirty');
  }
  pushHistory();
  updateSaveButton();
  requestPreviewUpdate();
}

// Convert state.edits + state.adds into the {edits, adds} payload the server
// expects. OCR'd-span edits become add_text-with-redaction calls.
function buildSavePayload() {
  const edits = [];
  const adds  = state.adds.map((a) => ({...a}));
  for (const e of state.edits.values()) {
    if (e.is_ocr) {
      adds.push({
        page: e.page,
        x: (e.origin && e.origin[0]) || e.bbox[0],
        y: (e.origin && e.origin[1]) || e.bbox[3],
        text: e.new_text,
        fontsize: e.size || 11,
        color: [0, 0, 0],
        bold: false,
        italic: false,
        font_name: '',
        redact_bbox: e.bbox,
      });
    } else {
      edits.push(e);
    }
  }
  // image_b64 is sent without the dataUrl prefix; dataUrl is local-only.
  const images = state.imageInserts.map((i) => ({
    page: i.page, x: i.x, y: i.y, width: i.width, height: i.height,
    image_b64: i.image_b64,
  }));
  return {edits, adds, images};
}

// ---------- undo / redo ----------
let historyDebounce = null;
function pushHistory() {
  // Coalesce rapid keystrokes into a single history entry — wait 400ms
  // after the last edit before snapshotting.
  if (historyDebounce) clearTimeout(historyDebounce);
  historyDebounce = setTimeout(() => {
    state.history = state.history.slice(0, state.historyIdx + 1);
    state.history.push({
      edits: new Map(state.edits),
      adds: state.adds.map((a) => ({...a})),
      images: state.imageInserts.map((i) => ({...i})),
    });
    state.historyIdx = state.history.length - 1;
    if (state.history.length > 100) {
      state.history = state.history.slice(-100);
      state.historyIdx = state.history.length - 1;
    }
  }, 400);
}

function restoreFromHistory() {
  const snapshot = state.history[state.historyIdx];
  if (!snapshot) return;
  state.edits = new Map(snapshot.edits);
  state.adds  = snapshot.adds.map((a) => ({...a}));
  state.imageInserts = (snapshot.images || []).map((i) => ({...i}));
  // Re-render the spans list so input values reflect the restored state.
  renderSpansList(state.pages[state.currentPage]);
  // Also re-render overlays so their .dirty class matches.
  const img = document.getElementById('page-img');
  if (img.complete) renderOverlays(state.pages[state.currentPage], img);
  updateSaveButton();
  requestPreviewUpdate(true);
}

function undo() {
  if (state.historyIdx > 0) {
    state.historyIdx--;
    restoreFromHistory();
  }
}
function redo() {
  if (state.historyIdx < state.history.length - 1) {
    state.historyIdx++;
    restoreFromHistory();
  }
}

window.addEventListener('keydown', (e) => {
  // Don't interfere with native undo/redo inside the search input field
  if (e.target.tagName === 'INPUT' && e.target.type === 'text') return;
  const meta = e.metaKey || e.ctrlKey;
  if (!meta) return;
  if (e.key === 'z' && !e.shiftKey) { e.preventDefault(); undo(); }
  else if ((e.key === 'z' && e.shiftKey) || e.key === 'y') {
    e.preventDefault(); redo();
  }
});

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

  // If the user has toggled into "show original" compare mode, never overwrite
  // the displayed image with an edited preview — keep the original visible.
  if (state.viewMode === 'original') {
    canvas.classList.remove('loading');
    return;
  }

  // Send only edits relevant to *any* page — server applies all then renders
  // the requested page. Sending all is fine; same payload as save.
  const {edits, adds} = buildSavePayload();

  if (edits.length === 0 && adds.length === 0) {
    // No changes anywhere — show the cached original PNG.
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
        body: JSON.stringify({ edits, adds, images: buildSavePayload().images }),
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
  const nEdits = state.edits.size;
  const nAdds  = state.adds.length;
  const nImgs  = state.imageInserts.length;
  const total  = nEdits + nAdds + nImgs;
  btn.disabled = total === 0;
  if (total === 0) {
    status.textContent = 'No changes yet';
  } else {
    const parts = [];
    if (nEdits) parts.push(`${nEdits} edit${nEdits === 1 ? '' : 's'}`);
    if (nAdds)  parts.push(`${nAdds} new text${nAdds === 1 ? '' : 's'}`);
    if (nImgs)  parts.push(`${nImgs} image${nImgs === 1 ? '' : 's'}`);
    status.textContent = parts.join(' + ') + ' pending';
  }

  // Compare button is only useful when there are pending changes.
  const cmp = document.getElementById('compare-btn');
  cmp.disabled = total === 0;
  if (total === 0 && state.viewMode === 'original') {
    state.viewMode = 'edited';
    cmp.classList.remove('active');
    cmp.textContent = 'Show original';
  }
}

// ---------- zoom ----------
function applyZoom() {
  const img = document.getElementById('page-img');
  const page = state.pages[state.currentPage];
  if (!page || !img.naturalWidth) return;

  if (Math.abs(state.zoom - 1.0) < 0.01) {
    // 100% = fit to container width
    img.style.width = '';
    img.style.maxWidth = '100%';
  } else {
    const container = document.querySelector('.preview-scroll');
    const fitWidth = Math.max(100, container.clientWidth - 32);
    img.style.maxWidth = 'none';
    img.style.width = `${Math.round(fitWidth * state.zoom)}px`;
  }
  document.getElementById('zoom-level').textContent =
    `${Math.round(state.zoom * 100)}%`;
  document.getElementById('zoom-out').disabled = state.zoom <= 0.5;
  document.getElementById('zoom-in').disabled  = state.zoom >= 3.0;
  // Re-render overlays at new scale.
  renderOverlays(page, img);
}

function setZoom(level) {
  state.zoom = Math.max(0.5, Math.min(3.0, level));
  applyZoom();
}

document.getElementById('zoom-in').onclick    = () => setZoom(state.zoom + 0.25);
document.getElementById('zoom-out').onclick   = () => setZoom(state.zoom - 0.25);
document.getElementById('zoom-level').onclick = () => setZoom(1.0);

// ---------- compare (toggle original vs edited) ----------
document.getElementById('compare-btn').onclick = () => {
  if (state.edits.size === 0 && state.adds.length === 0) return;
  const btn = document.getElementById('compare-btn');
  if (state.viewMode === 'edited') {
    state.viewMode = 'original';
    btn.classList.add('active');
    btn.textContent = 'Showing original — click for edited';
    setPageImageSrc(`/preview/${state.session}/${state.currentPage}.png?t=${Date.now()}`);
  } else {
    state.viewMode = 'edited';
    btn.classList.remove('active');
    btn.textContent = 'Show original';
    requestPreviewUpdate(true);
  }
};

// ---------- add-text tool ----------
document.getElementById('add-text-btn').onclick = () => {
  const active = document.body.dataset.tool === 'add-text';
  if (active) {
    document.body.removeAttribute('data-tool');
    document.getElementById('add-text-btn').classList.remove('active');
  } else {
    document.body.dataset.tool = 'add-text';
    document.getElementById('add-text-btn').classList.add('active');
  }
};

document.getElementById('preview-canvas').addEventListener('click', (e) => {
  const tool = document.body.dataset.tool;
  if (tool !== 'add-text' && tool !== 'insert-image') return;
  // Don't catch clicks on existing overlays — those mean "edit that span".
  if (e.target.classList.contains('span-overlay') ||
      e.target.classList.contains('add-overlay') ||
      e.target.classList.contains('image-overlay')) return;

  const canvas = document.getElementById('preview-canvas');
  const img = document.getElementById('page-img');
  const rect = img.getBoundingClientRect();
  const px = e.clientX - rect.left;
  const py = e.clientY - rect.top;
  const page = state.pages[state.currentPage];
  const scale = img.clientWidth / page.width;
  const x_pdf = px / scale;
  const y_pdf = py / scale;

  if (tool === 'add-text') {
    spawnAddTextInput(canvas, px, py, x_pdf, y_pdf);
  } else if (tool === 'insert-image') {
    pendingImagePos = { x: x_pdf, y: y_pdf, page: state.currentPage };
    document.getElementById('image-file-input').click();
  }
});

// ---------- insert-image tool ----------
let pendingImagePos = null;

document.getElementById('insert-image-btn').onclick = () => {
  const active = document.body.dataset.tool === 'insert-image';
  document.getElementById('add-text-btn').classList.remove('active');
  if (active) {
    document.body.removeAttribute('data-tool');
    document.getElementById('insert-image-btn').classList.remove('active');
  } else {
    document.body.dataset.tool = 'insert-image';
    document.getElementById('insert-image-btn').classList.add('active');
  }
};

document.getElementById('image-file-input').addEventListener('change', (e) => {
  const file = e.target.files[0];
  e.target.value = '';
  document.body.removeAttribute('data-tool');
  document.getElementById('insert-image-btn').classList.remove('active');
  if (!file || !pendingImagePos) return;
  const pos = pendingImagePos;
  pendingImagePos = null;

  const reader = new FileReader();
  reader.onload = () => {
    const dataUrl = reader.result;
    const b64 = dataUrl.split(',')[1] || '';
    // Determine intrinsic image dimensions to seed sensible default size.
    const img = new Image();
    img.onload = () => {
      // Default to 30% of page width while preserving aspect ratio,
      // capped to 200pt wide.
      const page = state.pages[pos.page];
      const targetW = Math.min(200, page.width * 0.3);
      const aspect = img.naturalHeight / Math.max(1, img.naturalWidth);
      const w = targetW;
      const h = targetW * aspect;
      state.imageInserts.push({
        page: pos.page,
        x: pos.x,
        y: pos.y,
        width: w,
        height: h,
        image_b64: b64,
        dataUrl,
      });
      pushHistory();
      renderOverlays(state.pages[state.currentPage],
                     document.getElementById('page-img'));
      updateSaveButton();
      requestPreviewUpdate(true);
    };
    img.onerror = () => alert('Could not read this image.');
    img.src = dataUrl;
  };
  reader.readAsDataURL(file);
});

function spawnAddTextInput(canvas, px, py, x_pdf, y_pdf) {
  // Inherit style from the nearest existing span on this page.
  const page = state.pages[state.currentPage];
  let nearest = null;
  let bestDist = Infinity;
  for (const s of page.spans) {
    const cx = (s.bbox[0] + s.bbox[2]) / 2;
    const cy = (s.bbox[1] + s.bbox[3]) / 2;
    const d = Math.hypot(cx - x_pdf, cy - y_pdf);
    if (d < bestDist) { bestDist = d; nearest = s; }
  }
  const fontsize = nearest ? nearest.size : 11;
  const color = nearest ? nearest.color : 0;
  const colorRgb = [
    ((color >> 16) & 0xff) / 255,
    ((color >> 8) & 0xff) / 255,
    (color & 0xff) / 255,
  ];
  const bold = !!(nearest && nearest.style.includes('bold'));
  const italic = !!(nearest && nearest.style.includes('italic'));
  const fontName = nearest ? nearest.font : '';

  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'add-input';
  input.style.left = px + 'px';
  // The canvas-relative top is the *baseline* in PDF, so subtract the font
  // ascent so the visible input box aligns with where the text will appear.
  input.style.top = Math.max(0, py - fontsize) + 'px';
  input.style.fontSize = fontsize + 'px';
  if (bold) input.style.fontWeight = 'bold';
  if (italic) input.style.fontStyle = 'italic';

  canvas.appendChild(input);
  input.focus();

  const commit = (commit) => {
    const txt = input.value.trim();
    input.remove();
    if (!commit || !txt) return;
    state.adds.push({
      page: state.currentPage,
      x: x_pdf,
      y: y_pdf,
      text: txt,
      fontsize: fontsize,
      color: colorRgb,
      bold: bold,
      italic: italic,
      font_name: fontName,
    });
    pushHistory();
    updateSaveButton();
    requestPreviewUpdate(true);
  };

  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter')   { ev.preventDefault(); commit(true); }
    if (ev.key === 'Escape')  { ev.preventDefault(); commit(false); }
  });
  input.addEventListener('blur', () => commit(true));

  // Exit add-text mode after one placement so the user isn't surprised by
  // a second click also creating text.
  document.body.removeAttribute('data-tool');
  document.getElementById('add-text-btn').classList.remove('active');
}

// ---------- save & download ----------
document.getElementById('save-btn').onclick = async () => {
  const btn = document.getElementById('save-btn');
  const status = document.getElementById('save-status');
  const summary = document.getElementById('diff-summary');
  btn.disabled = true;
  status.className = 'status';
  summary.hidden = true;

  const payload = buildSavePayload();
  const totalChanges = payload.edits.length + payload.adds.length + payload.images.length;
  status.textContent = `Applying ${totalChanges} change${totalChanges === 1 ? '' : 's'}…`;

  try {
    const r = await fetch(`/save/${state.session}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const editsArray = payload.edits;
    const addsArray  = payload.adds;
    const imagesArray = state.imageInserts;
    const data = await r.json();
    if (!r.ok) {
      status.classList.add('error');
      status.textContent = 'Error: ' + (data.error || 'save failed');
      btn.disabled = false;
      return;
    }
    // Trigger download without navigating away.
    const downloadName = (state.filename || 'edited.pdf')
      .replace(/\.pdf$/i, '') + '-edited.pdf';
    const a = document.createElement('a');
    a.href = data.download_url;
    a.download = downloadName;
    document.body.appendChild(a);
    a.click();
    a.remove();

    status.classList.add('success');
    status.textContent = `Saved ${data.applied} of ${data.requested} edit(s) — downloading ${downloadName}`;

    // Build the diff summary: list each edit's before→after, plus new adds.
    const editDiffs = editsArray.map((e) => {
      const labelBase = e.original || '';
      const before = labelBase.length > 38 ? labelBase.slice(0, 35) + '…' : labelBase;
      const after  = e.new_text.length > 38 ? e.new_text.slice(0, 35) + '…' : e.new_text;
      return `${before || '(empty)'} → ${after || '(empty)'}`;
    });
    const addDiffs = addsArray.map((a) => {
      const t = a.text.length > 50 ? a.text.slice(0, 47) + '…' : a.text;
      return `+ added: ${t}`;
    });
    const imageDiffs = imagesArray.map((i) =>
      `+ inserted image (page ${i.page + 1}, ${Math.round(i.width)}×${Math.round(i.height)}pt)`);
    const allDiffs = [...editDiffs, ...addDiffs, ...imageDiffs];
    let html = `<strong>Changes saved (${data.applied} of ${data.requested}):</strong>`;
    html += '<ul>' + allDiffs.map((d) => `<li>${escapeHTML(d)}</li>`).join('') + '</ul>';
    if (data.warnings && data.warnings.length) {
      summary.classList.add('warn');
      const warnBits = data.warnings.slice(0, 4).map((w) => `<li>${escapeHTML(w)}</li>`).join('');
      const more = data.warnings.length > 4 ? ` (+${data.warnings.length - 4} more)` : '';
      html += `<div style="margin-top:6px"><strong>Notes:</strong></div>` +
              `<ul>${warnBits}</ul>${more}`;
    } else {
      summary.classList.remove('warn');
    }
    summary.innerHTML = html;
    summary.hidden = false;
    btn.disabled = false;
  } catch (e) {
    status.classList.add('error');
    status.textContent = 'Network error: ' + e.message;
    btn.disabled = false;
  }
};

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// ---------- OCR ----------
document.getElementById('ocr-btn').onclick = async () => {
  const btn = document.getElementById('ocr-btn');
  const status = document.getElementById('ocr-status');
  status.classList.remove('error');
  status.textContent = 'Running OCR (this can take 5–30 seconds)…';
  btn.disabled = true;
  try {
    const r = await fetch(`/ocr/${state.session}/${state.currentPage}`,
                          { method: 'POST' });
    const data = await r.json();
    if (!r.ok) {
      status.classList.add('error');
      status.textContent = data.error || `OCR failed (${r.status})`;
      btn.disabled = false;
      return;
    }
    const newSpans = data.spans || [];
    if (newSpans.length === 0) {
      status.textContent = 'OCR found no text on this page.';
      btn.disabled = false;
      return;
    }
    // Append OCR'd spans to this page's span list and re-render.
    state.pages[state.currentPage].spans.push(...newSpans);
    document.getElementById('ocr-prompt').hidden = true;
    renderSpansList(state.pages[state.currentPage]);
    const img = document.getElementById('page-img');
    if (img.complete) renderOverlays(state.pages[state.currentPage], img);
  } catch (e) {
    status.classList.add('error');
    status.textContent = 'Network error: ' + e.message;
    btn.disabled = false;
  }
};

document.getElementById('new-pdf-btn').onclick = async () => {
  // Delete current session on the server so it doesn't count toward the
  // per-IP cap — best-effort; ignore failure.
  if (state.session) {
    try {
      await fetch(`/sessions/${state.session}`, { method: 'POST' });
    } catch {}
  }
  location.reload();
};
