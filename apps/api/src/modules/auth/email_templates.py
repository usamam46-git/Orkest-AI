"""
modules/auth/email_templates.py — the verification-code email.

Table layout with every style inline: Gmail strips <style> blocks in some contexts
and Outlook ignores most modern CSS, so this is the one place the "no inline
styles" habit is wrong. Two images are hosted by the web app under /email/ (the
mark and the illustration) — email clients block data: URIs and inline SVG, so a
PNG at a public URL is the only portable choice. Everything meaningful (the code,
the greeting, the expiry) is live text, so a client that blocks images still shows
a complete, usable message.

The palette is the product's own: near-black ink, paper-grey page, lime accent.
"""

from __future__ import annotations

from html import escape

INK = "#141414"
INK_SOFT = "#5c5f66"
PAPER = "#f5f5f7"
LIME = "#c8f536"
HAIRLINE = "#e4e4e7"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','Helvetica Neue',Arial,sans-serif"
MONO = "'SF Mono',SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"


def verification_email(*, code: str, full_name: str, base_url: str, expires_minutes: int) -> tuple[str, str, str]:
    """Return (subject, html, text) for a sign-up verification code."""
    base = base_url.rstrip("/")
    first = full_name.strip().split(" ")[0] if full_name.strip() else "there"
    name = escape(first)
    spaced = "&nbsp;".join(code)  # 8&nbsp;6&nbsp;9… keeps the digits readable and un-wrappable

    subject = f"{code} is your Orkest verification code"

    text = (
        f"Hi {first},\n\n"
        f"Your Orkest verification code is {code}.\n"
        f"It expires in {expires_minutes} minutes. Enter it on the sign-up page to finish creating your account.\n\n"
        "Orkest will never ask you for this code by phone or chat. "
        "If you did not create an account, you can ignore this email.\n\n"
        f"{base}\n"
    )

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{escape(subject)}</title>
</head>
<body style="margin:0;padding:0;background:{PAPER};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:{PAPER};">Your verification code is {code}. It expires in {expires_minutes} minutes.</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAPER};">
<tr><td align="center" style="padding:32px 16px;">
  <table role="presentation" width="560" cellpadding="0" cellspacing="0" style="width:100%;max-width:560px;">

    <tr><td style="padding:0 4px 20px 4px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
        <td style="font-family:{FONT};">
          <img src="{base}/email/logo.png" width="28" height="28" alt="" style="display:inline-block;vertical-align:middle;border:0;">
          <span style="display:inline-block;vertical-align:middle;margin-left:8px;font-size:18px;font-weight:700;letter-spacing:-0.02em;color:{INK};">Orkest</span>
        </td>
        <td align="right" style="font-family:{FONT};font-size:13px;color:{INK_SOFT};">Email verification</td>
      </tr></table>
    </td></tr>

    <tr><td style="background:#ffffff;border-radius:20px;overflow:hidden;border:1px solid {HAIRLINE};">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        <tr><td style="height:5px;background:{LIME};font-size:0;line-height:0;">&nbsp;</td></tr>

        <tr><td align="center" style="padding:28px 24px 0 24px;">
          <img src="{base}/email/verify.png" width="472" alt="A verification code, held at the approval gate" style="display:block;width:100%;max-width:472px;height:auto;border:0;">
        </td></tr>

        <tr><td style="padding:24px 36px 0 36px;font-family:{FONT};color:{INK};">
          <p style="margin:0 0 12px 0;font-size:22px;line-height:1.3;font-weight:700;letter-spacing:-0.02em;">Hi {name}, confirm your email.</p>
          <p style="margin:0;font-size:15px;line-height:1.6;color:{INK_SOFT};">One last step before your workspace opens. Enter this code on the sign-up page to prove this address is yours.</p>
        </td></tr>

        <tr><td align="center" style="padding:28px 36px 8px 36px;">
          <table role="presentation" cellpadding="0" cellspacing="0"><tr>
            <td align="center" style="background:{INK};border-radius:14px;padding:18px 30px;font-family:{MONO};font-size:34px;line-height:1;font-weight:700;letter-spacing:6px;color:#ffffff;">{spaced}</td>
          </tr></table>
        </td></tr>

        <tr><td align="center" style="padding:10px 36px 0 36px;font-family:{FONT};font-size:13px;color:{INK_SOFT};">
          Expires in <strong style="color:{INK};">{expires_minutes} minutes</strong>. Single use.
        </td></tr>

        <tr><td style="padding:28px 36px 32px 36px;font-family:{FONT};">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAPER};border-radius:12px;">
            <tr><td style="padding:14px 16px;font-size:13px;line-height:1.55;color:{INK_SOFT};">
              <strong style="color:{INK};">Didn&rsquo;t ask for this?</strong> Someone may have typed your address by mistake. Nothing happens until the code is entered, so you can safely ignore this email.
            </td></tr>
          </table>
        </td></tr>
      </table>
    </td></tr>

    <tr><td align="center" style="padding:24px 16px 0 16px;font-family:{FONT};font-size:12px;line-height:1.6;color:{INK_SOFT};">
      Orkest will never ask for this code by phone or chat.<br>
      Questions? <a href="{base}/#contact" style="color:{INK};text-decoration:underline;">Talk to us</a>
      &nbsp;&middot;&nbsp; <a href="{base}" style="color:{INK};text-decoration:underline;">orkest</a>
    </td></tr>
    <tr><td align="center" style="padding:14px 16px 0 16px;font-family:{FONT};font-size:12px;color:{INK_SOFT};">
      Automation that knows when to ask.
    </td></tr>

  </table>
</td></tr>
</table>
</body>
</html>"""
    return subject, html, text
