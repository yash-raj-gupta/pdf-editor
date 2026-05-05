# PDF Editor — Architecture & Roadmap

A small Flask + PyMuPDF web app that lets a user upload a PDF, edit text in
place while preserving the original font/size/color, add new text, drop in
images (signature, logo), run OCR on scanned pages, and download the result.

This document is the source of truth for what's done, how it's wired, and
what to do as traffic grows. Keep it up to date.

---

## 1. What's done today

**Core editor (`pdf_editor/`)**
- Inspect: extracts every text span with font name, size, color (hex + rgb),
  bold/italic flags, bounding box, baseline origin, ascender/descender.
- Edit: redacts the original span (strips it from the content stream, not
  just paints over it) then re-inserts the new text at the original baseline
  using the original embedded font when its glyph table covers the new
  characters.
- Per-character font fallback: when the embedded subset is missing a glyph
  (e.g. you typed `M` but Stripe's heading subset only has `April`'s
  letters), only that character drops to a system fallback font. The rest
  of the line stays in the original face.
- System font matching: on first use, scans `/System/Library/Fonts`,
  `/Library/Fonts`, `~/Library/Fonts` (and Linux equivalents), plus the
  project's `fonts/` dir, indexes by PostScript name and family. So
  `Inter-SemiBold` in the PDF resolves to your installed Inter SemiBold if
  present.
- Typographic adaptation: if the original used U+2010 hyphen / NBSP / smart
  quotes, ASCII variants in the new text are mapped to the same characters
  the original used, so subset fonts don't render a hyphen as a `.notdef`
  box.
- Auto-fit: replacement text wider than the original bbox gets the font
  size shrunk down to ≥70% of the original to fit; below that, leave it
  to overflow (and warn).
- Add new text at any (x,y) with optional bbox redaction underneath.
- Insert images (PNG/JPEG) at any rect — used for signatures and logos.

**Server (`server.py`)**
- `POST /upload` — multipart upload, 32 MB cap, returns spans + page sizes.
- `GET /preview/<sid>/<n>.png` — renders the original at 144 DPI.
- `GET /thumb/<sid>/<n>.png` — low-DPI page thumbnail for the rail.
- `POST /preview-edited/<sid>/<n>.png` — renders the page with the user's
  pending edits/adds/images applied (no save).
- `POST /save/<sid>` — applies all changes, writes `edited.pdf`, returns a
  download URL.
- `GET /download/<sid>` — serves the edited PDF as an attachment.
- `POST /ocr/<sid>/<n>` — runs PyMuPDF's tesseract integration on a page
  with no extractable text; returns word-level spans.
- `POST /sessions/<sid>` — early-deletes a session (used by "Open another
  PDF").
- `GET /healthz` — uptime probe.
- `GET /login`, `POST /login`, `POST /logout` — shared-password auth.

**Production hardening**
- Gunicorn runner (`Procfile`, `run-prod.sh`).
- Auto-deletion of sessions older than `PDF_SESSION_TTL_SECONDS` (default 1h)
  via a background sweeper thread.
- Per-IP active-session cap (`PDF_MAX_SESSIONS_PER_IP`, default 5).
- Rate limiting (Flask-Limiter, in-memory): upload 10/min, save 20/min,
  preview 60/min, login 10/min.
- Shared-password auth with signed `httponly`+`samesite=Lax` cookies;
  constant-time password compare.
- Request body size capped at 32 MB.
- 60 s gunicorn worker timeout.

**Frontend**
- Vanilla JS, no build step. Drag-and-drop upload. Side-by-side preview +
  span list. Click an overlay to focus its input. Live preview with 600 ms
  debounce. Zoom in/out (50–300%). Compare-original toggle.
- Search/filter spans by text. Undo/redo (Cmd-Z / Cmd-Shift-Z) up to 100
  steps. Save status with diff summary after save.
- Mobile layout under 900 px (panes stack, Preview/Edit tab toggle).
- Dark mode (system preference + manual override, persisted).
- Tools: `+ Add text` (click-to-place), `+ Insert image` (click-to-place
  + file picker), OCR prompt for scanned pages.

---

## 2. Architecture

### Module map

```
~/projects/pdf-editor/
├── pdf_editor/             # core editor — pure library, no Flask
│   ├── __init__.py         # public API re-exports
│   ├── types.py            # TextSpan, color/text helpers, flag constants
│   ├── fonts.py            # base14 mapping, SystemFontIndex, typography
│   ├── inspector.py        # PDFInspector  (read-only)
│   └── editor.py           # PDFEditor     (replace/add/save)
├── server.py               # Flask app + route registration
├── auth.py                 # login routes + before_request middleware
├── sessions.py             # session dir mgmt, sweeper, per-IP cap
├── payload.py              # _parse_edits/_parse_adds/_parse_image_inserts
├── templates/
│   ├── index.html          # main UI
│   └── login.html          # password gate
├── static/
│   ├── app.css
│   └── app.js
├── fonts/                  # drop TTFs here for fallback (Inter, etc.)
├── cli.py                  # `pdf-editor inspect/find/edit/replace-at`
├── demo.py                 # synthetic-PDF demo + verification
├── test_embedded_font.py   # round-trip test with embedded TTF
├── pyproject.toml          # uv-managed deps
├── uv.lock
├── Procfile                # `gunicorn server:app …`
└── run-prod.sh             # production launcher (validates env vars)
```

### Request flow (the hot path: edit-and-download)

```
Browser                 Flask                 PDFEditor                Disk
   │                      │                      │                      │
   ├─ POST /upload (PDF) ▶│                      │                      │
   │                      ├─ session_dir.mkdir ─────────────────────────▶│
   │                      ├─ save uploaded PDF ─────────────────────────▶│
   │                      ├─ PDFInspector.iter_spans() ▶│                │
   │                      │                       │                      │
   │◀ JSON {pages,spans} ─┤                                              │
   │                      │                                              │
   ├─ POST /preview-edited│ (debounced 600 ms during typing)             │
   │   {edits,adds,images}│                                              │
   │                      ├─ PDFEditor.replace_spans() ▶│                │
   │                      │                       │  (in-memory only)   │
   │                      ├─ render page → PNG ──▶│                      │
   │◀ PNG  ───────────────┤                                              │
   │                      │                                              │
   ├─ POST /save ────────▶│                                              │
   │                      ├─ PDFEditor.replace_spans() + add_text +      │
   │                      │   insert_image()                             │
   │                      ├─ ed.save(edited.pdf) ──────────────────────▶│
   │◀ JSON {download_url} ┤                                              │
   ├─ GET /download/<sid>▶│                                              │
   │◀ edited.pdf ─────────┤◀── stream from disk ────────────────────────┤
```

### Data lifecycle

- Each upload creates `<TMPDIR>/pdf-editor-sessions/<32-hex-uuid>/`
  containing `original.pdf` and (after save) `edited.pdf`.
- Per-IP active-session list lives in process memory; cleared on restart
  (acceptable — caps are advisory).
- Background sweeper (`sessions._start_sweeper`) deletes session dirs whose
  mtime is older than TTL every 5 min.
- `session_dir(sid)` touches the dir's mtime on each request so an actively
  used session stays alive past TTL.

### Concurrency model

Single Python process per gunicorn worker. Default `WEB_CONCURRENCY=2`.
PDF processing is CPU-bound and PyMuPDF releases the GIL during render, but
Flask is sync — one HTTP request blocks one worker until done. A 30-page
PDF preview takes ~2 s; under load you'll need more workers, not async.

---

## 3. Reliability

### Failure modes and what catches them

| Failure | What happens | What catches it |
|---|---|---|
| Hostile PDF hangs `fitz` | worker stuck | gunicorn `--timeout 60` kills + restarts the worker |
| Session sweeper crashes | leak | wrapped in `try/except`; logs and keeps looping |
| User uploads non-PDF | malformed | content-type sniff + `.pdf` extension check + PDFInspector raises with friendly message |
| Disk fills up | upload fails | sweeper keeps it bounded; OS returns ENOSPC; user sees 500 (TODO: surface friendly) |
| Font extraction fails for an exotic embedded font | fallback to base14 / system font | per-character fallback path + warnings list returned in the save response |
| `apply_redactions` raises mid-batch | half-applied edit | next save retries from the original PDF; we never modify in place |
| User session ID leaked | someone else opens it | UUID is 128 bits, signed cookie required for any API call when auth is on |
| One worker dies | ongoing requests on that worker drop | gunicorn restarts; other workers are unaffected |
| Memory blow-up on big PDF | OOM | 32 MB upload cap + 1500-page-soft-limit (enforce in inspector — TODO) |

### Known limitations

- **Single-machine state.** Sessions live on the machine's tmp dir; can't
  scale to multiple servers without shared storage. Fine for one box.
- **Rate limits are in-memory per worker.** With 2 workers and a 10/min
  upload cap, a determined client can effectively get 20/min. To enforce
  globally, switch Flask-Limiter's `storage_uri` to Redis when worker count
  goes up.
- **No per-user quotas — only per-IP.** A shared NAT (office, café Wi-Fi)
  would have all users share a single IP's cap.
- **PDFs with rasterised text.** OCR works only when tesseract is installed.
  Default macOS / Linux installs don't have it.
- **Right-to-left scripts and complex shaping.** PyMuPDF's `insert_text` is
  glyph-by-glyph; edits to Arabic/Hindi text won't shape ligatures correctly.
- **The auth model is a single shared password.** Fine for family pilot,
  not for a multi-tenant product.

---

## 4. Operational runbook

### Deploy (Fly.io recommended for v1)

```bash
# 1. Generate a real secret key
SECRET=$(openssl rand -hex 32)

# 2. Pick a strong shared password
PASSWORD="<paste here>"

# 3. Local dev
uv run python server.py     # http://127.0.0.1:5050  (no auth)

# 4. Production (single machine)
PDF_EDITOR_PASSWORD="$PASSWORD" \
PDF_EDITOR_SECRET_KEY="$SECRET" \
PDF_EDITOR_SECURE_COOKIE=1 \
WEB_CONCURRENCY=2 \
./run-prod.sh

# 5. Fly.io
fly launch                  # answers: Python, no Postgres, no Redis
fly secrets set PDF_EDITOR_PASSWORD="$PASSWORD" \
                PDF_EDITOR_SECRET_KEY="$SECRET" \
                PDF_EDITOR_SECURE_COOKIE=1
fly deploy
```

Procfile is already correct for Heroku-flavoured platforms. For systemd see
the systemd unit suggestion at the end of this doc.

### Monitor

- `GET /healthz` — JSON `{"ok": true}` if the worker is alive. Hook to your
  uptime monitor (Better Uptime free tier, UptimeRobot, etc.)
- Gunicorn writes access + error logs to stdout. Aggregate via the platform
  (`fly logs`, journald, Loki, Better Stack).
- **Add Sentry** when you have real users — `pip install sentry-sdk`,
  `sentry_sdk.init(dsn=…)` at top of `server.py`. Captures unhandled
  exceptions, slow requests, release tracking.

### Common issues

| Symptom | Likely cause | Fix |
|---|---|---|
| Upload returns 401 in JSON | password not configured / cookie expired | check `PDF_EDITOR_PASSWORD` is set; user re-logs in |
| Upload returns 429 | per-IP cap hit | `POST /sessions/<sid>` to free up a slot, or wait for sweeper |
| Edited PDF has wrong fonts | system font index missed the original | drop the right TTF in `fonts/` and restart |
| Preview takes >10 s | huge page (raster background) | acceptable; gunicorn timeout protects; consider page count cap |
| Disk fills | sweeper not running | check logs for `pdf-session-sweeper` thread, restart worker |
| OCR returns 503 | tesseract not installed | `brew install tesseract` (macOS) or `apt install tesseract-ocr` |

### Backups

The PDFs in `<TMPDIR>/pdf-editor-sessions/` are by design ephemeral. **Do
not back them up.** They contain user PII; auto-delete is a feature.

---

## 5. Roadmap by traffic stage

### Stage 0 — pre-launch (now → first user)

- [x] All editor + production-hardening items in §1.
- [ ] Deploy to Fly.io / Hetzner once Inter is on disk so Stripe receipts
      look right out of the box.
- [ ] Add Sentry once a DSN is available — single import, big payoff.
- [ ] Write a one-pager landing page (separate domain, link to the app).

### Stage 1 — first 10 family / pilot users

- [ ] Replace shared password with magic-link email login (single endpoint,
      use Resend's free tier, ~30 lines).
- [ ] Per-user rate limits keyed on email, not IP.
- [ ] Daily backup of just the auth/user table to S3 (free tier).
- [ ] Capture user feedback in-app — a tiny `?` button that opens a Tally
      form. Don't build your own form system.
- [ ] Drop bundled fonts: ship Inter, Roboto, Open Sans, Lato in `fonts/`
      so the most common Stripe/Google-Forms PDFs match their identified
      font without extra setup. ~6 MB at runtime, well worth it.

### Stage 2 — ~100 active users / day

- [ ] Move sessions to S3-compatible storage (Cloudflare R2 is free for
      low traffic). Lets you run multiple machines.
- [ ] Switch Flask-Limiter to Redis backing (Upstash free tier) so limits
      apply globally, not per-worker.
- [ ] Add a "recent files" view per user — top 10 PDFs with their edits
      preserved between sessions for 7 days. Requires a DB.
- [ ] Move from SQLite (which you'll add for Stage 1 magic links) to
      Postgres on Neon (free tier).
- [ ] Add a "render queue" worker so heavy renders don't block API. Use
      `rq` + Redis. Most renders stay sync; only OCR + 50+-page docs
      enqueue.

### Stage 3 — first paying users / ~1000/day

- [ ] Stripe Billing — usage-based or flat seats. Anonymous tier with
      tighter limits + paid tier with longer history + larger file caps.
- [ ] Audit log: who edited which doc when, retained 30 days. Important
      once you have business customers.
- [ ] Image-replacement (existing image → upload new one, keeping bbox).
      We have insert-image; this needs the page.replace_image route.
- [ ] OCR auto-detection: skip the prompt, run OCR automatically when a
      page has 0 spans + 1 image covering most of it.
- [ ] Right-click on a span to change its style (bold/italic/color),
      not just text.

### Stage 4 — scale (only if it works)

- [ ] Background processing for everything heavy.
- [ ] Region-pinned data residency (EU customers' PDFs in EU).
- [ ] SSO/SAML for enterprise.
- [ ] Audit + soft-delete for compliance contracts.
- [ ] Self-hosted option for paranoid customers.

---

## 6. Future feature backlog (stack-ranked, no commitment)

**High value, modest lift**
- Span-level style edits (bold/italic/color, not just text).
- "Find and replace" across all spans (we have it in the CLI; expose in UI).
- Multi-file batch: drop 10 invoices, replace recipient name in all.

**High value, big lift**
- Form field editing (PDF AcroForm support — different code path entirely).
- Right-to-left + complex shaping (probably needs HarfBuzz integration).
- Annotations: highlights, comments, freehand. Doable with PyMuPDF but a
  large feature surface.
- Signature drawing (canvas → PNG → insert as image; we already have the
  insert path).

**Cost / nice-to-have**
- Side-by-side diff view (split slider showing original vs edited).
- Keyboard shortcuts overlay (`?` opens a cheat sheet).
- Export non-PDF (HTML, Markdown) for content reuse.
- Import: paste an image of a receipt, auto-OCR, give an editable PDF.

---

## 7. Reference

### Environment variables

| Var | Required | Default | Purpose |
|---|---|---|---|
| `PDF_EDITOR_PASSWORD` | prod yes | unset | shared login password; if unset, server runs without auth (DEV mode) |
| `PDF_EDITOR_SECRET_KEY` | prod yes | dev-only | signs session cookies; rotate when leaked |
| `PDF_EDITOR_SECURE_COOKIE` | prod yes | unset | when truthy, sets `Secure` flag on cookies; require HTTPS |
| `PDF_SESSION_TTL_SECONDS` | no | 3600 | how long to keep an idle session |
| `PDF_MAX_SESSIONS_PER_IP` | no | 5 | per-IP cap on concurrent sessions |
| `PORT` | no | 5050 | gunicorn bind port |
| `WEB_CONCURRENCY` | no | 2 | gunicorn workers |

### systemd unit (paste into `/etc/systemd/system/pdf-editor.service`)

```ini
[Unit]
Description=PDF Editor
After=network.target

[Service]
Type=simple
User=pdf
WorkingDirectory=/srv/pdf-editor
EnvironmentFile=/srv/pdf-editor/.env
ExecStart=/srv/pdf-editor/run-prod.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Where to extend

- **New editor primitive** (e.g. "delete a span"): add a method to
  `pdf_editor/editor.py:PDFEditor`, expose via a server route in
  `server.py`, wire a UI button in `templates/index.html` + `static/app.js`.
- **New auth scheme**: replace `auth.py`. The rest of the server interacts
  only with the `session.get('authed')` flag.
- **New storage backend** (e.g. S3): replace `sessions.py`'s functions
  (`session_dir`, `_track_session`, `_sweep_old_sessions`). Keep the
  signatures and the rest of the app doesn't change.
