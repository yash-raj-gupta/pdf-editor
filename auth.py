"""Authentication.

Two modes, chosen by which env vars are set:

  * magic-link    — when `RESEND_API_KEY` AND `EMAIL_FROM` are set.
                    User enters an email; we mail them a one-tap link.
                    Optional `ALLOWED_EMAILS` env limits who can sign in
                    (comma-separated list, with `*@yourdomain.com`
                    wildcards). When unset, anyone can request a link
                    (rate-limited).
  * password      — when only `PDF_EDITOR_PASSWORD` is set. Single
                    shared password for all users (the legacy flow).

When neither is set the app runs in DEV mode with no auth at all — that
state is signposted in run-prod.sh and refused by server.py's production
fail-closed check.

`register(app, limiter)` wires the routes and the before-request guard
onto the given Flask app. Public paths (`/`, `/login`, `/healthz`,
`/favicon.ico`, `/robots.txt`, `/sitemap.xml`) and `/static/*` bypass
the guard. Authenticated state is stored in Flask's signed-cookie
session.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Iterable

from flask import (Flask, jsonify, redirect, render_template, request,
                   session, url_for)
from flask_limiter import Limiter
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from email_send import is_configured as _email_configured, send_magic_link


log = logging.getLogger(__name__)


# ---- configuration ---------------------------------------------------------

PASSWORD = os.environ.get("PDF_EDITOR_PASSWORD") or ""
# Dev-only "no auth at all" mode: true ONLY when neither a shared password
# nor email-based magic-link is configured. With either set, the
# before_request guard runs and the user must authenticate.
DEV_NOAUTH = not (PASSWORD or _email_configured())
PUBLIC_PATHS = ("/", "/login", "/healthz", "/favicon.ico",
                "/robots.txt", "/sitemap.xml")

# Magic-link config
MAGIC_TTL_SECONDS = int(os.environ.get("MAGIC_LINK_TTL_SECONDS") or 1800)
_MAGIC_SALT = "pdfsmith-magic-link-v1"

# Optional allowlist. Each entry is either a literal email or a
# `*@domain.com` wildcard. Empty = anyone can sign in.
_ALLOWLIST = tuple(
    s.strip().lower() for s in
    (os.environ.get("ALLOWED_EMAILS") or "").split(",")
    if s.strip()
)


def auth_mode() -> str:
    """Return 'magic' | 'password' | 'none' based on what's configured."""
    if _email_configured():
        return "magic"
    if PASSWORD:
        return "password"
    return "none"


def _email_allowed(email: str) -> bool:
    """Check `email` against ALLOWED_EMAILS. When the allowlist is empty
    everyone is allowed (callers may still rate-limit). Wildcard form
    `*@example.com` matches any user at that domain."""
    if not _ALLOWLIST:
        return True
    email = email.lower().strip()
    for entry in _ALLOWLIST:
        if entry == email:
            return True
        if entry.startswith("*@") and email.endswith(entry[1:].lower()):
            return True
    return False


_EMAIL_RE = __import__("re").compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _looks_like_email(s: str) -> bool:
    """Cheap email-shape check — Resend rejects malformed addresses
    anyway, this is just a first cut to give the user a nicer error."""
    return bool(s and _EMAIL_RE.match(s))


# ---- route registration ----------------------------------------------------

def register(app: Flask, limiter: Limiter) -> None:
    """Add login routes and the before-request guard to `app`."""

    def _signer() -> URLSafeTimedSerializer:
        return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=_MAGIC_SALT)

    @app.before_request
    def _require_auth():
        if DEV_NOAUTH:
            return
        path = request.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return
        if path.startswith("/auth/"):
            return  # magic-link callback is public by definition
        if session.get("authed"):
            return
        if request.method == "GET" and request.accept_mimetypes.accept_html:
            return redirect(url_for("login", next=path))
        return jsonify({"error": "authentication required"}), 401

    @app.get("/login")
    def login():
        next_url = request.args.get("next", "/")
        if session.get("authed"):
            return redirect(next_url)
        return render_template(
            "login.html",
            mode=auth_mode(),
            next=next_url,
            error=None,
            sent_to=request.args.get("sent_to"),
        )

    @app.post("/login")
    @limiter.limit("10 per minute")
    def login_post():
        """One POST endpoint for both modes — branches on which field
        the form submitted."""
        next_url = request.form.get("next", "/") or "/"
        mode = auth_mode()

        if mode == "magic":
            return _magic_send(next_url)

        # ---- password mode (legacy) ----
        submitted = (request.form.get("password") or "").encode("utf-8")
        expected = PASSWORD.encode("utf-8")
        if expected and hmac.compare_digest(submitted, expected):
            session.clear()
            session.permanent = True     # honour PERMANENT_SESSION_LIFETIME
            session["authed"] = True
            return redirect(next_url)
        return render_template("login.html", mode=mode, next=next_url,
                               error="Wrong password."), 401

    def _magic_send(next_url: str):
        """Magic-link mode: validate the email, generate a token, send it.

        Always renders the "check your inbox" state on a valid-shaped
        email regardless of whether the email is on the allowlist —
        that prevents enumeration of who's invited."""
        email = (request.form.get("email") or "").strip().lower()
        log.info(
            "magic-link request: email=%r allowlist=%s ttl_min=%d",
            email,
            "(open — no allowlist set)" if not _ALLOWLIST else list(_ALLOWLIST),
            MAGIC_TTL_SECONDS // 60,
        )
        if not _looks_like_email(email):
            log.info("magic-link rejected (malformed email): %r", email)
            return render_template(
                "login.html", mode="magic", next=next_url,
                error="Please enter a valid email address.",
            ), 400

        if _email_allowed(email):
            token = _signer().dumps({"email": email, "next": next_url})
            magic_url = url_for("auth_magic", t=token, _external=True)
            log.info("magic-link generated for %s — magic_url=%s",
                     email, magic_url)
            ok = send_magic_link(to=email, magic_url=magic_url,
                                 ttl_minutes=MAGIC_TTL_SECONDS // 60)
            if not ok:
                # Email send failed. Specific reason is in the
                # email_send logs above this one — copy from Render's
                # log stream when debugging.
                log.error("magic-link send FAILED for %s — see preceding "
                          "email_send log lines for the Resend error",
                          email)
                return render_template(
                    "login.html", mode="magic", next=next_url,
                    error="Couldn't send the email right now. Try again in a minute.",
                ), 503
            log.info("magic-link sent OK to %s", email)
        else:
            # Quietly drop the request — same UI as success — to avoid
            # leaking who's on the allowlist. The real consequence is
            # the recipient never gets an email and won't be able to
            # sign in. Log it so admin can see attempts.
            log.warning("magic-link blocked (not on allowlist): %r — "
                        "allowlist=%s", email, list(_ALLOWLIST))

        # Same response either way.
        return redirect(url_for("login", sent_to=email, next=next_url))

    @app.get("/auth/magic")
    @limiter.limit("30 per minute")
    def auth_magic():
        """Validate a magic-link token and log the user in."""
        token = request.args.get("t", "")
        if not token:
            return render_template(
                "login.html", mode="magic", next="/",
                error="Sign-in link is missing the token.",
            ), 400

        try:
            data = _signer().loads(token, max_age=MAGIC_TTL_SECONDS)
        except SignatureExpired:
            return render_template(
                "login.html", mode="magic", next="/",
                error="That sign-in link has expired. Request a new one.",
            ), 401
        except BadSignature:
            return render_template(
                "login.html", mode="magic", next="/",
                error="That sign-in link is invalid.",
            ), 401

        email = (data.get("email") or "").lower()
        next_url = data.get("next") or "/"

        # Re-check the allowlist in case it changed between send + click.
        if not _email_allowed(email):
            return render_template(
                "login.html", mode="magic", next="/",
                error="Your email isn't on this app's allowlist.",
            ), 403

        session.clear()
        session.permanent = True     # honour PERMANENT_SESSION_LIFETIME
        session["authed"] = True
        session["email"] = email
        log.info("magic-link sign-in: %s — session valid for %d days",
                 email,
                 int(app.config["PERMANENT_SESSION_LIFETIME"].total_seconds() / 86400))
        return redirect(next_url)

    @app.post("/logout")
    def logout():
        session.clear()
        # Back to the marketing landing — not the bare login form. Lets
        # the user re-read what they're signing into before doing so.
        return redirect("/")
