from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.dependencies import get_current_user, oauth2_scheme
from src.core.rate_limit import RateLimiter
from src.core.redis import get_redis
from src.db.database import get_db_session
from src.modules.auth.models import User
from src.modules.auth.schemas import (
    LoginRequest,
    ProvidersResponse,
    RegisterRequest,
    TokenResponse,
)
from src.modules.auth.service import AuthService, GoogleSignInError

router = APIRouter()


def get_auth_service(
    db: AsyncSession = Depends(get_db_session),
    redis: aioredis.Redis = Depends(get_redis),
) -> AuthService:
    return AuthService(db, redis)


def set_refresh_token_cookie(response: Response, refresh_token: str) -> None:
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=True,  # Should be True in production (HTTPS only)
        samesite="strict",
        max_age=30 * 24 * 60 * 60,  # 30 days
    )


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RateLimiter(requests=3, window=60))],
)
async def register(
    request: RegisterRequest,
    response: Response,
    service: AuthService = Depends(get_auth_service),
):
    """Register a new user, create an organization and default workspace."""
    result = await service.register(request)
    set_refresh_token_cookie(response, result["refresh_token"])
    return TokenResponse(access_token=result["access_token"], token_type=result["token_type"])


@router.post(
    "/login",
    response_model=TokenResponse,
    dependencies=[Depends(RateLimiter(requests=5, window=60))],
)
async def login(
    request: LoginRequest,
    response: Response,
    service: AuthService = Depends(get_auth_service),
):
    """Authenticate a user and return access/refresh tokens."""
    result = await service.login(request)
    set_refresh_token_cookie(response, result["refresh_token"])
    return TokenResponse(access_token=result["access_token"], token_type=result["token_type"])


@router.post("/switch-org/{org_id}", response_model=TokenResponse)
async def switch_org(
    org_id: str,
    response: Response,
    user: User = Depends(get_current_user),
    service: AuthService = Depends(get_auth_service),
):
    """Switch organization context. Issues a new token pair scoped to the new org."""
    result = await service.switch_org(user.id, org_id)
    set_refresh_token_cookie(response, result["refresh_token"])
    return TokenResponse(access_token=result["access_token"], token_type=result["token_type"])


@router.post("/refresh", response_model=TokenResponse)
async def refresh_tokens(
    response: Response,
    refresh_token: Annotated[str | None, Cookie()] = None,
    service: AuthService = Depends(get_auth_service),
):
    """Rotate the refresh token and issue a new access token."""
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token missing",
        )

    result = await service.refresh_tokens(refresh_token)
    set_refresh_token_cookie(response, result["refresh_token"])
    return TokenResponse(access_token=result["access_token"], token_type=result["token_type"])


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    access_token: str = Depends(oauth2_scheme),
    refresh_token: Annotated[str | None, Cookie()] = None,
    service: AuthService = Depends(get_auth_service),
):
    """Revoke access token and clear refresh token cookie."""
    await service.logout(access_token, refresh_token)
    response.delete_cookie(key="refresh_token", httponly=True, secure=True, samesite="strict")


# ---------------------------------------------------------------------------
# Sign in with Google
# ---------------------------------------------------------------------------

# Scoped to the Google routes only, and Lax rather than the Strict the refresh
# cookie uses: the callback arrives as a cross-site top-level GET from
# accounts.google.com, and a Strict cookie is not sent on that request — which
# would fail every sign-in on the one check meant to protect it.
_OAUTH_STATE_COOKIE = "google_oauth_state"
_OAUTH_COOKIE_PATH = "/api/v1/auth/google"


def _login_redirect(error: str) -> RedirectResponse:
    """Send the browser back to the login page with a short error slug."""
    response = RedirectResponse(f"{settings.FRONTEND_URL.rstrip('/')}/login?error=google_{error}", status_code=302)
    response.delete_cookie(_OAUTH_STATE_COOKIE, path=_OAUTH_COOKIE_PATH)
    return response


@router.get("/providers", response_model=ProvidersResponse)
async def providers():
    """Which sign-in methods are configured. Public: the login page needs it signed-out."""
    return ProvidersResponse(google=settings.google_enabled)


@router.get(
    "/google/login",
    dependencies=[Depends(RateLimiter(requests=10, window=60))],
)
async def google_login(service: AuthService = Depends(get_auth_service)):
    """Start Google sign-in: 302 to Google's consent screen."""
    if not settings.google_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    url, state = await service.google_begin()
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        key=_OAUTH_STATE_COOKIE,
        value=state,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=600,
        path=_OAUTH_COOKIE_PATH,
    )
    return response


@router.get(
    "/google/callback",
    dependencies=[Depends(RateLimiter(requests=20, window=60))],
)
async def google_callback(
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
    google_oauth_state: Annotated[str | None, Cookie()] = None,
    service: AuthService = Depends(get_auth_service),
):
    """
    Google redirects here. This is a browser navigation, so every outcome — success
    or failure — is a redirect, never a JSON body the user would be left staring at.
    """
    if not settings.google_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if error:
        # The user pressed Cancel / denied consent. Not a failure worth alarming about.
        return _login_redirect("cancelled")
    if not code or not state:
        return _login_redirect("failed")

    try:
        result = await service.google_complete(code, state, google_oauth_state)
    except GoogleSignInError as exc:
        return _login_redirect(exc.code)

    # Land on the dashboard: AuthGate there trades the refresh cookie for an access
    # token, exactly as it does after a page reload. No token is put in the URL.
    response = RedirectResponse(f"{settings.FRONTEND_URL.rstrip('/')}/dashboard", status_code=302)
    set_refresh_token_cookie(response, result["refresh_token"])
    response.delete_cookie(_OAUTH_STATE_COOKIE, path=_OAUTH_COOKIE_PATH)
    return response
