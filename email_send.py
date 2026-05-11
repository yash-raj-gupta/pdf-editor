"""Send transactional emails via Resend's REST API.

Stdlib-only — no new dependency. The caller owns the template (HTML +
plain-text). All callers go through `send_email()`; `send_magic_link()`
is a convenience that builds the magic-link template.

Env vars:
  RESEND_API_KEY   the secret API key from resend.com
  EMAIL_FROM       sender address on a verified domain
                   (e.g. "PDFsmith <auth@yashrajgupta.com>")

Every send attempt logs:
  * the masked API key prefix (so you can confirm it's the right one)
  * the sender + recipient
  * Resend's HTTP status code AND response body on any non-2xx
  * a parsed Resend error code where present (so you can match it
    against https://resend.com/docs/api-reference/errors)

These logs go to the root logger which gunicorn forwards to stdout —
visible in Render's Logs tab. Failures are at WARNING / ERROR so they
stand out in the log stream.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

# Make sure email-send log lines show up in Render even before the rest
# of the app has configured logging. Idempotent — only adds once.
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [email_send] %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = True

_RESEND_API_URL = "https://api.resend.com/emails"
_TIMEOUT_SECONDS = 15


def is_configured() -> bool:
    """True when both env vars needed to send are set."""
    return bool(os.environ.get("RESEND_API_KEY")) and bool(
        os.environ.get("EMAIL_FROM"))


def _masked(api_key: str) -> str:
    """Return e.g. 're_abcd…(34 chars)' so logs can verify the right key
    is loaded without leaking the secret."""
    if not api_key:
        return "<empty>"
    if len(api_key) <= 10:
        return f"{api_key[:3]}…({len(api_key)} chars)"
    return f"{api_key[:6]}…{api_key[-2:]} ({len(api_key)} chars)"


# Common Resend error codes worth surfacing to the log reader. Sourced
# from https://resend.com/docs/api-reference/errors plus observed codes.
_RESEND_HINTS = {
    "validation_error":      "EMAIL_FROM rejected — usually means the domain isn't fully verified yet OR the local part has bad chars. Check Resend → Domains is green.",
    "missing_api_key":       "RESEND_API_KEY not sent in Authorization header.",
    "invalid_api_key":       "RESEND_API_KEY value is wrong or revoked. Rotate it in Resend → API keys.",
    "rate_limit_exceeded":   "Resend free-tier rate limit hit (100/day, 2/sec). Wait or upgrade.",
    "restricted_api_key":    "API key has restricted permissions. Use a full-access key for sending.",
    "domain_not_verified":   "EMAIL_FROM domain isn't verified in Resend. Add the DNS records they show you.",
    "from_address_not_allowed": "EMAIL_FROM doesn't match any verified domain on this account.",
    "not_found":             "Endpoint or resource not found — usually a typo in the request.",
    # Cloudflare codes (Resend sits behind CF) — these show up in the
    # raw text body, not JSON. We special-case them in the handler.
    "cf_1010":               "Cloudflare flagged the User-Agent or IP as bot-like. The fix is to set a real User-Agent header on the request.",
    "cf_1020":               "Cloudflare's WAF blocked the request. Try again or check the source IP.",
}


def _classify_text_error(raw: str) -> tuple[str, str]:
    """Resend sometimes returns plain text (Cloudflare front-door errors)
    instead of JSON. Map them to (code, message) so logs are useful."""
    if not raw:
        return "", ""
    snippet = raw.lower()
    if "error code: 1010" in snippet:
        return "cf_1010", "Cloudflare blocked the request (likely bot User-Agent)."
    if "error code: 1020" in snippet:
        return "cf_1020", "Cloudflare WAF blocked the request."
    return "", ""


def send_email(*, to: str, subject: str, html: str,
               text: str | None = None) -> bool:
    """POST one email to Resend. Returns True on success.

    Failures never raise — they log the full Resend response and return
    False. Callers decide whether to surface a generic error to the user
    or do something more interesting.
    """
    api_key = os.environ.get("RESEND_API_KEY", "")
    sender = os.environ.get("EMAIL_FROM", "")
    if not api_key or not sender:
        log.error("CONFIG: missing RESEND_API_KEY or EMAIL_FROM — "
                  "key=%r from=%r", _masked(api_key), sender)
        return False

    payload: dict = {
        "from": sender,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text

    log.info("SEND attempt: from=%r to=%r subject=%r key=%s",
             sender, to, subject, _masked(api_key))

    body_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _RESEND_API_URL, data=body_bytes, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Resend sits behind Cloudflare; the default urllib UA
            # ("Python-urllib/x.y") is on Cloudflare's bot list and
            # returns a 403 "error code: 1010". A real-looking UA
            # avoids that without affecting Resend's own handling.
            "User-Agent": "PDFsmith/1.0 (+https://pdfsmith.yashrajgupta.com)",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            data = resp.read()
            try:
                parsed = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = {}
            if resp.status in (200, 201, 202):
                msg_id = parsed.get("id") or "<no id>"
                log.info("SEND ok: to=%r status=%d resend_id=%s",
                         to, resp.status, msg_id)
                return True
            log.error("SEND failed: to=%r status=%d body=%s",
                      to, resp.status, data[:500].decode("utf-8", "replace"))
            return False

    except urllib.error.HTTPError as e:
        # Resend returns its error JSON in the response body of 4xx/5xx.
        try:
            err_raw = e.read().decode("utf-8", errors="replace")[:600]
        except Exception:
            err_raw = ""
        # Try to parse as JSON to extract the error code/name; fall back
        # to plain-text matching (Cloudflare front-door errors).
        code = ""
        message = ""
        try:
            err_json = json.loads(err_raw)
            code = err_json.get("name") or err_json.get("error") or ""
            message = err_json.get("message") or ""
        except ValueError:
            code, message = _classify_text_error(err_raw)

        hint = _RESEND_HINTS.get(code, "")
        log.error("SEND failed (HTTP %d): to=%r from=%r code=%r message=%r%s\n"
                  "  raw_body=%s",
                  e.code, to, sender, code, message,
                  f"\n  hint: {hint}" if hint else "",
                  err_raw)
        return False

    except (urllib.error.URLError, OSError) as e:
        log.error("SEND failed (network): to=%r error=%s", to, e)
        return False


def send_magic_link(*, to: str, magic_url: str,
                    ttl_minutes: int = 30) -> bool:
    """Send a PDFsmith magic-link sign-in email."""
    subject = "Your PDFsmith sign-in link"
    html = _MAGIC_LINK_HTML.format(magic_url=magic_url,
                                   ttl_minutes=ttl_minutes)
    text = (
        f"Sign in to PDFsmith\n"
        f"\n"
        f"Click this link to sign in (valid for {ttl_minutes} minutes):\n"
        f"{magic_url}\n"
        f"\n"
        f"If you didn't request this email, you can safely ignore it.\n"
    )
    return send_email(to=to, subject=subject, html=html, text=text)


# All styling is inlined — most email clients ignore <style> blocks and
# external CSS. Keep the markup simple so it renders consistently across
# Gmail, Outlook, Apple Mail.
_MAGIC_LINK_HTML = """\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#fafafa;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1d1d1f;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#fafafa;padding:40px 0;">
    <tr>
      <td align="center">
        <table role="presentation" width="480" cellpadding="0" cellspacing="0" border="0" style="background:white;border-radius:14px;box-shadow:0 1px 3px rgba(0,0,0,0.04);padding:36px 40px;">
          <tr>
            <td>
              <div style="background:linear-gradient(135deg,#007aff 0%,#4d9dff 100%);width:44px;height:44px;border-radius:11px;display:inline-block;text-align:center;line-height:44px;margin-bottom:24px;">
                <span style="color:white;font-weight:700;font-size:18px;">P</span>
              </div>
              <h1 style="font-size:22px;font-weight:700;margin:0 0 12px;letter-spacing:-0.01em;">Sign in to PDFsmith</h1>
              <p style="color:#555;line-height:1.55;margin:0 0 24px;font-size:15px;">
                Click the button below to finish signing in. The link is valid
                for {ttl_minutes} minutes and can only be used once.
              </p>
              <p style="margin:0 0 28px;">
                <a href="{magic_url}"
                   style="display:inline-block;padding:12px 22px;background:#1d1d1f;color:white;border-radius:10px;text-decoration:none;font-weight:600;font-size:15px;">
                  Sign in to PDFsmith
                </a>
              </p>
              <p style="color:#86868b;font-size:13px;line-height:1.55;margin:0 0 8px;">
                Or paste this link into your browser:
              </p>
              <p style="margin:0 0 28px;">
                <a href="{magic_url}" style="color:#007aff;word-break:break-all;font-size:13px;">{magic_url}</a>
              </p>
              <p style="color:#86868b;font-size:12px;line-height:1.55;margin:0;padding-top:18px;border-top:1px solid #f0f0f3;">
                If you didn't request this, you can safely ignore this email.
                Someone may have typed your address by mistake.
              </p>
            </td>
          </tr>
        </table>
        <p style="color:#98989d;font-size:11px;margin:18px 0 0;">PDFsmith — edit PDFs without breaking their fonts.</p>
      </td>
    </tr>
  </table>
</body>
</html>"""
