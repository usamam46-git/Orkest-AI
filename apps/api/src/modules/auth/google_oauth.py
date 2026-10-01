"""
modules/auth/google_oauth.py — the Google half of "Sign in with Google".

Only the protocol lives here: build the consent URL, trade a code for tokens,
read the profile. Nothing in this file touches the database or issues our own
tokens — that is `AuthService`'s job, and keeping the two apart is what lets the
service be tested with `fetch_profile` stubbed instead of a fake Google.

## Authorization-code flow with PKCE, server side

The browser is sent to Google; Google sends it back to OUR callback with a
single-use `code`; the server — holding the client secret — exchanges that code.
No token from Google ever reaches the browser. PKCE is added on top even though
a confidential client does not strictly need it: it binds the code to the login
attempt that started it, so a code intercepted from a redirect cannot be redeemed.

## Why the profile comes from `userinfo`, not by decoding the id_token

The access token arrives straight from Google's token endpoint over TLS in
response to a request carrying our secret, and `userinfo` is then called with it
over TLS as well. Both hops are authenticated by the connection itself, so there
is no signature to verify and no JWKS to cache, and no place for a forged token to
enter. Decoding the id_token instead would need the signing keys and an issuer/
audience/expiry check, which is more code for the same trust.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from src.core.config import settings

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
SCOPES = "openid email profile"


class GoogleOAuthError(RuntimeError):
    """The code exchange or profile read failed. Carries no Google response body."""


@dataclass(frozen=True)
class GoogleProfile:
    sub: str
    email: str
    email_verified: bool
    name: str
    picture: str | None


def code_challenge(verifier: str) -> str:
    """S256 PKCE challenge: base64url(sha256(verifier)) with no padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorization_url(state: str, verifier: str) -> str:
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
        # Always show the account chooser. Without it a browser signed into one
        # Google account silently signs in as that one, which is wrong the moment
        # someone has a work and a personal account.
        "prompt": "select_account",
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


async def fetch_profile(code: str, verifier: str) -> GoogleProfile:
    """Exchange the authorization code, then read the signed-in user's profile."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            token_resp = await client.post(
                TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                    "grant_type": "authorization_code",
                    "code_verifier": verifier,
                },
            )
            if token_resp.status_code != 200:
                raise GoogleOAuthError(f"token exchange returned {token_resp.status_code}")
            access_token = token_resp.json().get("access_token")
            if not access_token:
                raise GoogleOAuthError("token response had no access_token")

            info_resp = await client.get(USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"})
            if info_resp.status_code != 200:
                raise GoogleOAuthError(f"userinfo returned {info_resp.status_code}")
            info = info_resp.json()
    except httpx.HTTPError as exc:
        raise GoogleOAuthError(f"could not reach Google: {type(exc).__name__}") from exc

    sub, email = info.get("sub"), info.get("email")
    if not sub or not email:
        raise GoogleOAuthError("profile is missing sub or email")
    return GoogleProfile(
        sub=str(sub),
        email=str(email).strip().lower(),
        # Google sends a real boolean, but be strict about it: only the literal
        # True counts, so a string "false" can never be read as truthy.
        email_verified=info.get("email_verified") is True,
        name=str(info.get("name") or "").strip(),
        picture=info.get("picture"),
    )
