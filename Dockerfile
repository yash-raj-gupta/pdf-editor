# PDF Editor — production image used by Fly.io / any Docker host.
#
# Two-stage build so the final image doesn't carry uv or build tooling.
# Total image size ~250 MB (PyMuPDF wheel is ~120 MB on its own).
#
# Build:  docker build -t pdf-editor .
# Run:    docker run --rm -p 5050:5050 \
#           -e PDF_EDITOR_PASSWORD="..." \
#           -e PDF_EDITOR_SECRET_KEY="$(openssl rand -hex 32)" \
#           -e PDF_EDITOR_SECURE_COOKIE=1 \
#           pdf-editor

# ---- builder ---------------------------------------------------------------
FROM python:3.12-slim AS builder

# uv is the fastest way to materialise a frozen dep set.
COPY --from=ghcr.io/astral-sh/uv:0.5.1 /uv /usr/local/bin/uv

WORKDIR /app

# Install deps into a venv we'll copy to the runtime image.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ---- runtime ---------------------------------------------------------------
FROM python:3.12-slim

# OCR is optional. Keep it because the editor surfaces a friendly error if
# this binary is missing, but PDFs that need OCR will Just Work when present.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy resolved deps and project source.
COPY --from=builder /app/.venv /app/.venv
COPY pdf_editor/   /app/pdf_editor/
COPY templates/    /app/templates/
COPY static/       /app/static/
COPY fonts/        /app/fonts/
COPY *.py          /app/

# PATH so we can run gunicorn from the venv directly.
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Defaults that platforms (Fly.io, Render) override at runtime.
ENV PORT=5050 \
    WEB_CONCURRENCY=2 \
    PDF_SESSIONS_DIR=/data/sessions

# Run as a non-root user.
RUN useradd --system --uid 1000 --create-home pdf \
    && mkdir -p /data/sessions \
    && chown -R pdf:pdf /data /app
USER pdf

EXPOSE 5050

# Gunicorn config:
#   --timeout 60   — kill workers stuck on a hostile PDF
#   --workers      — set via WEB_CONCURRENCY env
#   --access-logfile/--error-logfile -  send to stdout/stderr for the platform
CMD gunicorn server:app \
    --bind "0.0.0.0:${PORT}" \
    --workers "${WEB_CONCURRENCY}" \
    --timeout 60 \
    --access-logfile - \
    --error-logfile -
