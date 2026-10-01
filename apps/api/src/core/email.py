"""
core/email.py — outbound transactional email, through Resend.

Infrastructure rather than a module, for the same reason `storage.py` and
`llm_client.py` are: it owns no tables and no routes. One function, one provider,
plain `httpx` — Resend is a single JSON POST, so there is no SDK to pin.

The marketing contact form sends through Resend from the Next.js server using its
own copy of the same key; this is the API's path for mail the *platform* sends
(verification codes today). Both read `RESEND_API_KEY` and a sender on a domain
verified in Resend — an unverified sender is refused with a 403.

Errors never carry the provider's response body upward: it can echo the recipient
and the key's prefix, and the callers turn any failure into a short, user-safe
message.
"""

from __future__ import annotations

import logging

import httpx

from src.core.config import settings

logger = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"


class EmailError(RuntimeError):
    """The message could not be handed to the provider."""


async def send_email(*, to: str, subject: str, html: str, text: str) -> None:
    if not settings.email_enabled:
        raise EmailError("email delivery is not configured (RESEND_API_KEY / EMAIL_FROM)")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                RESEND_ENDPOINT,
                headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
                json={"from": settings.EMAIL_FROM, "to": [to], "subject": subject, "html": html, "text": text},
            )
    except httpx.HTTPError as exc:
        raise EmailError(f"could not reach the mail provider: {type(exc).__name__}") from exc

    if response.status_code >= 400:
        # Logged for the operator (unverified domain, bad key); never returned.
        logger.error("resend responded %s: %s", response.status_code, response.text[:300])
        raise EmailError(f"mail provider refused the message ({response.status_code})")
