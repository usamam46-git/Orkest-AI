"""
tests/test_email_verification.py — sign-up proves the address before anyone gets in.

The mail provider is never contacted: `send_email` is replaced by a recorder, and
the code is read back out of the message the same way a person reads it. What is
exercised for real is everything that is OURS — the stored hash, the attempt cap,
single use, the cooldown, and who gets a session when.

The refusals matter most. A code that can be replayed, guessed past five tries,
or used to learn which addresses have accounts is worse than no verification.
"""

import asyncio
import re
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from src.core.config import settings
from src.core.email import EmailError
from src.db.database import async_session_maker
from src.modules.auth import google_oauth
from src.modules.auth import service as auth_service
from src.modules.auth.email_templates import verification_email
from src.modules.auth.google_oauth import GoogleProfile
from src.modules.auth.models import User

PASSWORD = "StrongPassword123!"


@pytest.fixture
def outbox(monkeypatch) -> list[dict]:
    """Verification required, mail configured, and every message recorded."""
    monkeypatch.setattr(settings, "REQUIRE_EMAIL_VERIFICATION", True)
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(settings, "EMAIL_FROM", "Orkest <contact@example.test>")
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.test")
    sent: list[dict] = []

    async def fake_send(*, to: str, subject: str, html: str, text: str) -> None:
        sent.append({"to": to, "subject": subject, "html": html, "text": text})

    monkeypatch.setattr(auth_service, "send_email", fake_send)
    return sent


def _email() -> str:
    return f"person_{uuid.uuid4().hex[:8]}@example.com"


def _code(message: dict) -> str:
    match = re.search(r"\b(\d{6})\b", message["text"])
    assert match, message["text"]
    return match.group(1)


async def _register(client: AsyncClient, email: str, password: str = PASSWORD, org: str = "Acme"):
    return await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Pat Example", "organization_name": org},
    )


async def _verify(client: AsyncClient, email: str, code: str):
    return await client.post("/api/v1/auth/verify-email", json={"email": email, "code": code})


async def _user(email: str) -> User | None:
    async with async_session_maker() as s:
        return (await s.execute(select(User).where(User.email == email))).scalar_one_or_none()


# --- registering ------------------------------------------------------------


async def test_register_sends_a_code_and_issues_no_session(client: AsyncClient, outbox):
    email = _email()
    resp = await _register(client, email)

    assert resp.status_code == 201
    body = resp.json()
    assert body["verification_required"] is True
    assert body["email"] == email
    assert body["access_token"] is None
    assert "refresh_token" not in resp.headers.get("set-cookie", "")

    assert len(outbox) == 1 and outbox[0]["to"] == email
    assert len(_code(outbox[0])) == 6
    assert (await _user(email)).email_verified_at is None


async def test_login_before_verifying_is_refused_and_resends_nothing_within_the_cooldown(client: AsyncClient, outbox):
    email = _email()
    await _register(client, email)

    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "email_not_verified"
    assert len(outbox) == 1  # the code from sign-up is under a minute old: no second mail


async def test_a_wrong_password_never_reveals_that_the_account_is_unverified(client: AsyncClient, outbox):
    email = _email()
    await _register(client, email)
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": "not-the-password"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid email or password"


# --- verifying --------------------------------------------------------------


async def test_the_right_code_verifies_and_signs_the_user_in(client: AsyncClient, outbox):
    email = _email()
    await _register(client, email)

    resp = await _verify(client, email, _code(outbox[0]))
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]
    assert "refresh_token=" in resp.headers["set-cookie"]
    assert (await _user(email)).email_verified_at is not None

    me = await client.get("/api/v1/organizations/members/me", headers={"Authorization": f"Bearer {resp.json()['access_token']}"})
    assert me.status_code == 200 and me.json()["role_name"] == "Owner"

    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200


async def test_a_wrong_code_is_refused(client: AsyncClient, outbox):
    email = _email()
    await _register(client, email)
    right = _code(outbox[0])
    wrong = "000000" if right != "000000" else "111111"

    resp = await _verify(client, email, wrong)
    assert resp.status_code == 400
    assert (await _user(email)).email_verified_at is None


async def test_five_wrong_guesses_burn_the_code_even_for_the_right_one(client: AsyncClient, outbox):
    email = _email()
    await _register(client, email)
    right = _code(outbox[0])
    wrong = "000000" if right != "000000" else "111111"

    for _ in range(5):
        assert (await _verify(client, email, wrong)).status_code == 400
    assert (await _verify(client, email, right)).status_code == 400
    assert (await _user(email)).email_verified_at is None


async def test_a_code_works_once(client: AsyncClient, outbox):
    email = _email()
    await _register(client, email)
    code = _code(outbox[0])
    assert (await _verify(client, email, code)).status_code == 200
    assert (await _verify(client, email, code)).status_code == 400


async def test_an_unknown_address_gets_the_same_answer_as_a_wrong_code(client: AsyncClient, outbox):
    real, ghost = _email(), _email()
    await _register(client, real)
    wrong = await _verify(client, real, "000000")
    nobody = await _verify(client, ghost, "000000")
    assert wrong.status_code == nobody.status_code == 400
    assert wrong.json() == nobody.json()


async def test_a_code_is_for_one_address_only(client: AsyncClient, outbox):
    a, b = _email(), _email()
    await _register(client, a)
    await _register(client, b)
    resp = await _verify(client, b, _code(outbox[0]))  # a's code against b's address
    assert resp.status_code == 400


async def test_malformed_codes_are_rejected_before_any_lookup(client: AsyncClient, outbox):
    for bad in ["12345", "1234567", "abcdef", "12 456", ""]:
        resp = await client.post("/api/v1/auth/verify-email", json={"email": _email(), "code": bad})
        assert resp.status_code == 422


# --- resending --------------------------------------------------------------


async def test_resend_is_silent_for_an_address_with_no_account(client: AsyncClient, outbox):
    resp = await client.post("/api/v1/auth/resend-verification", json={"email": _email()})
    assert resp.status_code == 204
    assert outbox == []


async def test_resend_respects_the_cooldown_then_sends_a_new_code(client: AsyncClient, outbox, monkeypatch):
    monkeypatch.setattr(auth_service, "VERIFICATION_COOLDOWN_SECONDS", 1)
    email = _email()
    await _register(client, email)
    first = _code(outbox[0])

    await client.post("/api/v1/auth/resend-verification", json={"email": email})
    assert len(outbox) == 1  # inside the cooldown: nothing sent

    await asyncio.sleep(1.2)
    resp = await client.post("/api/v1/auth/resend-verification", json={"email": email})
    assert resp.status_code == 204
    assert len(outbox) == 2

    second = _code(outbox[1])
    assert (await _verify(client, email, second)).status_code == 200
    if first != second:
        # The first code was replaced when the second was minted, and is spent anyway.
        assert (await _verify(client, email, first)).status_code == 400


async def test_resend_for_an_already_verified_account_sends_nothing(client: AsyncClient, outbox):
    email = _email()
    await _register(client, email)
    await _verify(client, email, _code(outbox[0]))
    await client.post("/api/v1/auth/resend-verification", json={"email": email})
    assert len(outbox) == 1


# --- squatting --------------------------------------------------------------


async def test_re_registering_an_unverified_address_takes_it_over(client: AsyncClient, outbox, monkeypatch):
    monkeypatch.setattr(auth_service, "VERIFICATION_COOLDOWN_SECONDS", 1)
    victim = _email()
    await _register(client, victim, password="AttackerPassword1!")  # a squatter
    await asyncio.sleep(1.2)

    resp = await _register(client, victim, password="TheRealOwner123!")  # the owner arrives
    assert resp.status_code == 201 and resp.json()["verification_required"] is True
    assert len(outbox) == 2

    assert (await _verify(client, victim, _code(outbox[1]))).status_code == 200
    squatter = await client.post("/api/v1/auth/login", json={"email": victim, "password": "AttackerPassword1!"})
    assert squatter.status_code == 401
    owner = await client.post("/api/v1/auth/login", json={"email": victim, "password": "TheRealOwner123!"})
    assert owner.status_code == 200


async def test_a_verified_address_cannot_be_registered_again(client: AsyncClient, outbox):
    email = _email()
    await _register(client, email)
    await _verify(client, email, _code(outbox[0]))
    resp = await _register(client, email, password="SomethingElse123!")
    assert resp.status_code == 400


# --- provider trouble -------------------------------------------------------


async def test_a_mail_failure_is_a_503_and_leaves_no_usable_code(client: AsyncClient, outbox, monkeypatch):
    async def broken(**_):
        raise EmailError("provider refused")

    monkeypatch.setattr(auth_service, "send_email", broken)
    email = _email()
    resp = await _register(client, email)
    assert resp.status_code == 503
    assert "provider" not in resp.text.lower()
    assert (await _verify(client, email, "000000")).status_code == 400


async def test_requiring_verification_without_a_mail_provider_fails_loudly(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_EMAIL_VERIFICATION", True)
    monkeypatch.setattr(settings, "RESEND_API_KEY", "")
    monkeypatch.setattr(settings, "EMAIL_FROM", "")
    monkeypatch.setattr(settings, "DEBUG", False)
    email = _email()
    resp = await _register(client, email)
    assert resp.status_code == 503
    assert await _user(email) is None  # nothing was created


async def test_without_the_requirement_registration_is_unchanged_and_the_address_is_recorded_verified(client: AsyncClient):
    email = _email()
    resp = await _register(client, email)
    assert resp.status_code == 201
    assert resp.json()["access_token"] and resp.json()["verification_required"] is False
    assert (await _user(email)).email_verified_at is not None


# --- Google interplay -------------------------------------------------------


async def test_google_verifying_a_squatted_address_drops_the_squatters_password(client: AsyncClient, outbox, monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "secret")
    victim = _email()
    await _register(client, victim, password="AttackerPassword1!")

    async def fake_profile(code: str, verifier: str) -> GoogleProfile:
        return GoogleProfile(sub="g-sub-1", email=victim, email_verified=True, name="Real Owner", picture=None)

    monkeypatch.setattr(google_oauth, "fetch_profile", fake_profile)
    start = await client.get("/api/v1/auth/google/login", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    done = await client.get(
        "/api/v1/auth/google/callback",
        params={"code": "c", "state": state},
        headers={"cookie": f"google_oauth_state={state}"},
        follow_redirects=False,
    )
    assert done.headers["location"].endswith("/dashboard")

    user = await _user(victim)
    assert user.email_verified_at is not None
    assert user.hashed_password is None
    squatter = await client.post("/api/v1/auth/login", json={"email": victim, "password": "AttackerPassword1!"})
    assert squatter.status_code == 401


# --- the email itself -------------------------------------------------------


def test_the_email_carries_the_code_in_both_parts_and_escapes_the_name():
    subject, html, text = verification_email(
        code="482913", full_name="<script>alert(1)</script> Pat", base_url="https://app.example.test/", expires_minutes=10
    )
    assert "482913" in subject and "482913" in text
    assert "4&nbsp;8&nbsp;2&nbsp;9&nbsp;1&nbsp;3" in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "https://app.example.test/email/logo.png" in html  # no double slash
    assert "10 minutes" in text and "10 minutes" in html


# --- invitations ------------------------------------------------------------


async def test_an_invited_user_must_verify_too_and_lands_in_the_inviting_org(client: AsyncClient, outbox, monkeypatch):
    # The owner signs up with the requirement off so they can invite straight away.
    monkeypatch.setattr(settings, "REQUIRE_EMAIL_VERIFICATION", False)
    owner = await _register(client, _email(), org="Inviting Org")
    headers = {"Authorization": f"Bearer {owner.json()['access_token']}"}
    owner_org = (await client.get("/api/v1/organizations/members/me", headers=headers)).json()

    invitee = _email()
    invite = await client.post("/api/v1/organizations/members", json={"email": invitee, "role_name": "Editor"}, headers=headers)
    assert invite.status_code == 201, invite.text
    token = invite.json()["accept_url"].split("token=", 1)[1]

    monkeypatch.setattr(settings, "REQUIRE_EMAIL_VERIFICATION", True)
    reg = await client.post(
        "/api/v1/auth/register",
        json={"email": invitee, "password": PASSWORD, "full_name": "Invited Person", "invite_token": token},
    )
    assert reg.status_code == 201
    assert reg.json()["verification_required"] is True and reg.json()["access_token"] is None

    verified = await _verify(client, invitee, _code(outbox[-1]))
    assert verified.status_code == 200
    me = await client.get("/api/v1/organizations/members/me", headers={"Authorization": f"Bearer {verified.json()['access_token']}"})
    assert me.json()["role_name"] == "Editor"
    assert owner_org["role_name"] == "Owner"


async def test_in_debug_with_no_provider_the_code_is_logged_not_mailed(client: AsyncClient, monkeypatch, caplog):
    """The local-development path. DEBUG is false in production, where this is unreachable."""
    monkeypatch.setattr(settings, "REQUIRE_EMAIL_VERIFICATION", True)
    monkeypatch.setattr(settings, "RESEND_API_KEY", "")
    monkeypatch.setattr(settings, "EMAIL_FROM", "")
    monkeypatch.setattr(settings, "DEBUG", True)
    email = _email()
    with caplog.at_level("WARNING"):
        resp = await _register(client, email)
    assert resp.status_code == 201 and resp.json()["verification_required"] is True
    logged = next(r.getMessage() for r in caplog.records if "DEV ONLY" in r.getMessage())
    code = re.search(r"is (\d{6})", logged).group(1)
    assert (await _verify(client, email, code)).status_code == 200
