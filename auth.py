"""Shared-password authentication.

Reads the expected password from `PDF_EDITOR_PASSWORD`. When unset, the
app runs in DEV mode (no auth) — this is signposted in the production
launcher so you can't accidentally ship without a password.

`register(app, limiter)` wires the routes and the before-request guard
onto the given Flask app. Public paths (login, healthz, /static/*) bypass
the guard. Authenticated state is stored in Flask's signed-cookie session.
"""

from __future__ import annotations

import hmac
import os

from flask import (Flask, jsonify, redirect, render_template, request,
                   session, url_for)
from flask_limiter import Limiter


PASSWORD = os.environ.get("PDF_EDITOR_PASSWORD") or ""
DEV_NOAUTH = not PASSWORD  # when no password is set, run wide-open (dev only)
PUBLIC_PATHS = ("/", "/login", "/healthz", "/favicon.ico",
                "/robots.txt", "/sitemap.xml")


def register(app: Flask, limiter: Limiter) -> None:
    """Add login/logout routes and the before-request guard to `app`."""

    @app.before_request
    def _require_auth():
        if DEV_NOAUTH:
            return
        path = request.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return
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
        return render_template("login.html", next=next_url, error=None)

    @app.post("/login")
    @limiter.limit("10 per minute")
    def login_post():
        submitted = (request.form.get("password") or "").encode("utf-8")
        expected = PASSWORD.encode("utf-8")
        next_url = request.form.get("next", "/") or "/"
        if expected and hmac.compare_digest(submitted, expected):
            session.clear()
            session["authed"] = True
            return redirect(next_url)
        return render_template("login.html", next=next_url,
                               error="Wrong password."), 401

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))
