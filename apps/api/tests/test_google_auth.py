"""
tests/test_google_auth.py — Sign in with Google.

Google itself is never contacted: `google_oauth.fetch_profile` is the only thing
that talks to it, and the tests replace it with a function returning a canned
profile. What is exercised for real is everything that is OURS — the state
binding, single use, account resolution and the tokens issued afterwards.

The cases that matter are the refusals. A callback with no cookie, a replayed
state, an unverified address and an address already tied to a different Google
account are each a way to sign in as someone else if they are wrong.
"""

import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from src.core.config import settings
from src.db.database import async_session_maker
from src.modules.auth import google_oauth
from src.modules.auth.google_oauth import GoogleOAuthError, GoogleProfile
from src.modules.auth.models import OrgMembership, User
from src.modules.organizations.models import Organization

CALLBACK = "/api/v1/auth/google/callback"


@pytest.fixture
def google_on(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "test-client.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.test")


def _profile(**overrides) -> GoogleProfile:
    base = {
        "sub": f"sub-{uuid.uuid4().hex[:10]}",
        "email": f"person_{uuid.uuid4().hex[:6]}@example.com",
        "email_verified": True,
        "name": "Pat Example",
        "picture": "https://example.test/pat.png",
    }
    base.update(overrides)
    return GoogleProfile(**base)


def _stub_google(monkeypatch, profile: GoogleProfile | Exception):
    async def fake(code: str, verifier: str) -> GoogleProfile:
        assert code and verifier
        if isinstance(profile, Exception):
            raise profile
        return profile

    monkeypatch.setattr(google_oauth, "fetch_profile", fake)


async def _begin(client: AsyncClient) -> str:
    """Hit /google/login and return the `state` Google would echo back."""
    resp = await client.get("/api/v1/auth/google/login", follow_redirects=False)
    assert resp.status_code == 302, resp.text
    return parse_qs(urlparse(resp.headers["location"]).query)["state"][0]


async def _callback(client: AsyncClient, state: str, cookie_state: str | None = None, **extra):
    cookie = state if cookie_state is None else cookie_state
    headers = {"cookie": f"google_oauth_state={cookie}"} if cookie else {}
    params = {"code": "auth-code", "state": state, **extra}
    return await client.get(CALLBACK, params=params, headers=headers, follow_redirects=False)


async def _counts() -> tuple[int, int]:
    async with async_session_maker() as s:
        users = (await s.execute(select(func.count()).select_from(User))).scalar_one()
        orgs = (await s.execute(select(func.count()).select_from(Organization))).scalar_one()
        return users, orgs


# --- PKCE -------------------------------------------------------------------


def test_pkce_challenge_matches_the_rfc7636_example():
    # RFC 7636 appendix B.
    assert google_oauth.code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


# --- availability -----------------------------------------------------------


async def test_providers_reports_google_off_when_unconfigured(client: AsyncClient):
    resp = await client.get("/api/v1/auth/providers")
    assert resp.status_code == 200
    assert resp.json() == {"google": False}


async def test_providers_reports_google_on_when_configured(client: AsyncClient, google_on):
    assert (await client.get("/api/v1/auth/providers")).json() == {"google": True}


async def test_google_routes_404_when_unconfigured(client: AsyncClient):
    assert (await client.get("/api/v1/auth/google/login", follow_redirects=False)).status_code == 404
    assert (await client.get(CALLBACK, params={"code": "x", "state": "y"}, follow_redirects=False)).status_code == 404


# --- starting a sign-in -----------------------------------------------------


async def test_login_redirects_to_google_with_pkce_state_and_a_bound_cookie(client: AsyncClient, google_on):
    resp = await client.get("/api/v1/auth/google/login", follow_redirects=False)
    assert resp.status_code == 302
    url = urlparse(resp.headers["location"])
    assert url.netloc == "accounts.google.com"
    q = parse_qs(url.query)
    assert q["client_id"] == ["test-client.apps.googleusercontent.com"]
    assert q["response_type"] == ["code"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["scope"] == ["openid email profile"]
    assert q["redirect_uri"] == [settings.GOOGLE_REDIRECT_URI]
    cookie = resp.headers["set-cookie"].lower()
    assert f"google_oauth_state={q['state'][0]}".lower() in cookie
    assert "httponly" in cookie and "samesite=lax" in cookie


# --- the state binding ------------------------------------------------------


async def test_callback_without_the_state_cookie_is_refused(client: AsyncClient, google_on, monkeypatch):
    _stub_google(monkeypatch, _profile())
    state = await _begin(client)
    resp = await client.get(CALLBACK, params={"code": "c", "state": state}, follow_redirects=False)
    assert resp.headers["location"].endswith("/login?error=google_state")
    assert await _counts() == (0, 0)


async def test_callback_with_a_mismatched_cookie_is_refused(client: AsyncClient, google_on, monkeypatch):
    _stub_google(monkeypatch, _profile())
    state = await _begin(client)
    resp = await _callback(client, state, cookie_state="someone-elses-state")
    assert resp.headers["location"].endswith("/login?error=google_state")


async def test_an_unissued_state_is_refused(client: AsyncClient, google_on, monkeypatch):
    _stub_google(monkeypatch, _profile())
    resp = await _callback(client, "never-issued")
    assert resp.headers["location"].endswith("/login?error=google_state")


async def test_a_state_can_only_be_used_once(client: AsyncClient, google_on, monkeypatch):
    _stub_google(monkeypatch, _profile())
    state = await _begin(client)
    first = await _callback(client, state)
    assert first.headers["location"].endswith("/dashboard")
    replay = await _callback(client, state)
    assert replay.headers["location"].endswith("/login?error=google_state")


# --- account resolution -----------------------------------------------------


async def test_a_new_google_user_gets_their_own_organization(client: AsyncClient, google_on, monkeypatch):
    profile = _profile(name="Pat Example")
    _stub_google(monkeypatch, profile)
    resp = await _callback(client, await _begin(client))

    assert resp.status_code == 302
    assert resp.headers["location"] == "https://app.example.test/dashboard"
    assert "refresh_token=" in resp.headers["set-cookie"]

    async with async_session_maker() as s:
        user = (await s.execute(select(User).where(User.email == profile.email))).scalar_one()
        assert user.google_sub == profile.sub
        assert user.hashed_password is None
        assert user.avatar_url == profile.picture
        membership = (await s.execute(select(OrgMembership).where(OrgMembership.user_id == user.id))).scalar_one()
        assert membership.status == "active"
        org = (await s.execute(select(Organization).where(Organization.id == membership.organization_id))).scalar_one()
        assert org.name == "Pat Example's organization"


async def test_signing_in_again_reuses_the_account_and_creates_nothing(client: AsyncClient, google_on, monkeypatch):
    _stub_google(monkeypatch, _profile())
    await _callback(client, await _begin(client))
    after_first = await _counts()
    resp = await _callback(client, await _begin(client))
    assert resp.headers["location"].endswith("/dashboard")
    assert await _counts() == after_first == (1, 1)


async def test_a_verified_email_links_to_the_existing_password_account(client: AsyncClient, google_on, monkeypatch):
    email = f"existing_{uuid.uuid4().hex[:6]}@example.com"
    reg = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "StrongPassword123!", "full_name": "Existing", "organization_name": "Acme"},
    )
    assert reg.status_code == 201
    before = await _counts()

    profile = _profile(email=email)
    _stub_google(monkeypatch, profile)
    resp = await _callback(client, await _begin(client))

    assert resp.headers["location"].endswith("/dashboard")
    assert await _counts() == before  # linked, not duplicated: no new user, no new org
    async with async_session_maker() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one()
        assert user.google_sub == profile.sub
        assert user.hashed_password is not None  # the password still works
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": "StrongPassword123!"})
    assert login.status_code == 200


async def test_an_unverified_email_is_refused_and_links_nothing(client: AsyncClient, google_on, monkeypatch):
    email = f"victim_{uuid.uuid4().hex[:6]}@example.com"
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "StrongPassword123!", "full_name": "Victim", "organization_name": "V"},
    )
    before = await _counts()

    _stub_google(monkeypatch, _profile(email=email, email_verified=False))
    resp = await _callback(client, await _begin(client))

    assert resp.headers["location"].endswith("/login?error=google_unverified")
    assert await _counts() == before
    async with async_session_maker() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one()
        assert user.google_sub is None


async def test_an_email_tied_to_a_different_google_account_is_refused(client: AsyncClient, google_on, monkeypatch):
    email = f"owner_{uuid.uuid4().hex[:6]}@example.com"
    _stub_google(monkeypatch, _profile(email=email, sub="original-sub"))
    await _callback(client, await _begin(client))

    _stub_google(monkeypatch, _profile(email=email, sub="a-different-sub"))
    resp = await _callback(client, await _begin(client))

    assert resp.headers["location"].endswith("/login?error=google_conflict")
    async with async_session_maker() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one()
        assert user.google_sub == "original-sub"


async def test_a_user_with_no_active_membership_is_refused(client: AsyncClient, google_on, monkeypatch):
    profile = _profile()
    _stub_google(monkeypatch, profile)
    await _callback(client, await _begin(client))
    async with async_session_maker() as s:
        user = (await s.execute(select(User).where(User.email == profile.email))).scalar_one()
        m = (await s.execute(select(OrgMembership).where(OrgMembership.user_id == user.id))).scalar_one()
        m.status = "suspended"
        await s.commit()

    resp = await _callback(client, await _begin(client))
    assert resp.headers["location"].endswith("/login?error=google_no_org")


# --- failure modes ----------------------------------------------------------


async def test_the_user_cancelling_consent_redirects_quietly(client: AsyncClient, google_on):
    state = await _begin(client)
    resp = await client.get(CALLBACK, params={"error": "access_denied", "state": state}, follow_redirects=False)
    assert resp.headers["location"].endswith("/login?error=google_cancelled")


async def test_a_google_outage_is_a_redirect_not_a_500(client: AsyncClient, google_on, monkeypatch):
    _stub_google(monkeypatch, GoogleOAuthError("could not reach Google"))
    resp = await _callback(client, await _begin(client))
    assert resp.status_code == 302
    assert resp.headers["location"].endswith("/login?error=google_failed")
    assert await _counts() == (0, 0)


async def test_the_token_issued_belongs_to_the_signed_in_user(client: AsyncClient, google_on, monkeypatch):
    """The refresh cookie from the callback must really mint an access token for that user."""
    profile = _profile()
    _stub_google(monkeypatch, profile)
    resp = await _callback(client, await _begin(client))
    refresh = resp.headers["set-cookie"].split("refresh_token=")[1].split(";")[0]

    tokens = await client.post("/api/v1/auth/refresh", headers={"cookie": f"refresh_token={refresh}"})
    assert tokens.status_code == 200
    me = await client.get("/api/v1/organizations/members/me", headers={"Authorization": f"Bearer {tokens.json()['access_token']}"})
    assert me.status_code == 200
    assert me.json()["role_name"] == "Owner"
