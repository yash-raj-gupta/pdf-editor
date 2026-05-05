#!/usr/bin/env bash
# Production launcher. Run from the project root.
#
# Required env:   PDF_EDITOR_PASSWORD       shared password (auth)
#                 PDF_EDITOR_SECRET_KEY     used to sign session cookies
# Optional env:   PORT                      default 5050
#                 WEB_CONCURRENCY           gunicorn workers, default 2
#                 PDF_SESSION_TTL_SECONDS   session cleanup TTL, default 3600
#                 PDF_MAX_SESSIONS_PER_IP   active-session cap, default 5

set -euo pipefail

if [[ -z "${PDF_EDITOR_PASSWORD:-}" ]]; then
  echo "FATAL: set PDF_EDITOR_PASSWORD before launching." >&2
  exit 2
fi
if [[ -z "${PDF_EDITOR_SECRET_KEY:-}" ]]; then
  echo "FATAL: set PDF_EDITOR_SECRET_KEY (any 32+ random bytes)." >&2
  exit 2
fi

cd "$(dirname "$0")"
exec uv run gunicorn server:app \
  --bind "0.0.0.0:${PORT:-5050}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --timeout 60 \
  --access-logfile - \
  --error-logfile -
