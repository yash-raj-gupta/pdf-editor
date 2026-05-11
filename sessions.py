"""Session lifecycle: per-session tmp dirs, age-based sweeper, per-IP caps.

A "session" is a single uploaded PDF and any edits the user has made on it.
Each session gets its own directory under SESSIONS_DIR named with a 32-hex
UUID. The sweeper runs in a daemon thread, deleting dirs whose mtime is
older than `ttl_seconds`. Active requests touch the dir's mtime so an
in-use session doesn't expire mid-session.

Per-IP active-session counts live in process memory (a dict of deques).
This means caps are per-process; with multiple gunicorn workers, an
attacker could get N×workers sessions before being throttled. For
multi-worker enforcement, swap this for Redis. For a 2-worker single-host
deploy, the in-memory cap is good enough.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from flask import abort

log = logging.getLogger(__name__)


# Where session dirs live. Defaults to OS temp; on Fly.io / a real server
# point this at a mounted persistent volume so sessions survive worker
# restarts within their TTL window.
SESSIONS_DIR: Path = Path(
    os.environ.get("PDF_SESSIONS_DIR")
    or (Path(tempfile.gettempdir()) / "pdf-editor-sessions")
)
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


# ---- per-IP tracking -------------------------------------------------------

_ip_session_index: dict[str, deque[str]] = defaultdict(deque)
_ip_index_lock = threading.Lock()


def track(sid: str, ip: str) -> None:
    """Record that `ip` just created a new session `sid`."""
    with _ip_index_lock:
        q = _ip_session_index[ip]
        q.append(sid)
        # Drop ids whose dirs are gone (sweeper may have deleted them).
        while q and not (SESSIONS_DIR / q[0]).is_dir():
            q.popleft()


def count_for_ip(ip: str) -> int:
    """Count the active sessions for `ip` (gc'ing dead ids)."""
    with _ip_index_lock:
        q = _ip_session_index[ip]
        alive = deque(s for s in q if (SESSIONS_DIR / s).is_dir())
        _ip_session_index[ip] = alive
        return len(alive)


# ---- directory helpers -----------------------------------------------------

def session_dir(sid: str) -> Path:
    """Validate `sid` and return its directory, or 4xx via abort()."""
    if len(sid) != 32 or any(c not in "0123456789abcdef" for c in sid):
        abort(400, "invalid session id")
    p = SESSIONS_DIR / sid
    if not p.is_dir():
        abort(404, "session not found")
    # Touch mtime so in-use sessions don't expire while being edited.
    try:
        p.touch()
    except OSError:
        pass
    return p


def delete(sid: str) -> bool:
    """Best-effort delete. Used by `Open another PDF` so the user gets
    their per-IP slot back without waiting for the sweeper."""
    p = SESSIONS_DIR / sid
    if not p.is_dir():
        return False
    shutil.rmtree(p, ignore_errors=True)
    return True


# ---- sweeper ---------------------------------------------------------------

def sweep_old(ttl_seconds: int) -> int:
    """Delete session dirs older than `ttl_seconds`. Returns count removed."""
    cutoff = time.time() - ttl_seconds
    removed = 0
    if not SESSIONS_DIR.is_dir():
        return 0
    for entry in SESSIONS_DIR.iterdir():
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except FileNotFoundError:
            pass
    return removed


def start_sweeper(ttl_seconds: int, interval_seconds: int = 300) -> None:
    """Spawn a daemon thread that runs `sweep_old` on a loop. Failures are
    logged but never escape — the thread should outlive any single bad
    iteration so we don't leak sessions silently."""
    def loop() -> None:
        while True:
            try:
                n = sweep_old(ttl_seconds)
                if n:
                    log.info("session sweeper deleted %d dir(s)", n)
            except Exception:
                log.exception("session sweep failed")
            time.sleep(interval_seconds)
    t = threading.Thread(target=loop, name="pdf-session-sweeper", daemon=True)
    t.start()
