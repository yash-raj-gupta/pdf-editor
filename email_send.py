"""Send transactional emails via Resend's REST API.

Stdlib-only — no new dependency. The caller owns the template (HTML +
plain-text). All callers go through `send_email()`; `send_magic_link()`
is a convenience that builds the magic-link template.

Env vars:
  RESEND_API_KEY   the secret API key from resend.com
  EMAIL_FROM       sender address on a verified domain
                   (e.g. "PDFsmith <auth@yashrajgupta.com>")

Failures log a useful message but never raise — the caller decides
whether to surface "couldn't send email right now" to the user, retry,
or fall back to a different auth path.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

_RESEND_API_URL = "https://api.resend.com/emails"
_TIMEOUT_SECONDS = 15


def is_configured() -> bool:
    """True when both env vars needed to send are set."""
    return bool(os.environ.get("RESEND_API_KEY")) and bool(
        os.environ.get("EMAIL_FROM"))


def send_email(*, to: str, subject: str, html: str,
               text: str | None = None) -> bool:
    """POST one email to Resend. Returns True on success, False otherwise.

    The plain-text version is optional but recommended — some inbox
    providers downrank HTML-only mail as a spam signal.
    """
    api_key = os.environ.get("RESEND_API_KEY")
    sender = os.environ.get("EMAIL_FROM")
    if not api_key or not sender:
        log.error("RESEND_API_KEY or EMAIL_FROM not set; cannot send email")
        return False

    payload: dict = {
        "from": sender,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _RESEND_API_URL, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            data = resp.read()
            if resp.status not in (200, 201, 202):
                log.error("Resend %d: %s", resp.status, data[:200])
                return False
            log.info("Resend accepted email to %s", to)
            return True
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            err_body = ""
        log.error("Resend HTTP %d sending to %s: %s", e.code, to, err_body)
        return False
    except (urllib.error.URLError, OSError) as e:
        log.error("Resend network error sending to %s: %s", to, e)
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
