import hashlib
import hmac
import json
import logging
import secrets
import uuid
from datetime import UTC, datetime

import redis.asyncio as aioredis
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.cache import (
    blocklist_token,
    invalidate_permissions_cache,
)
from src.core.config import settings
from src.core.email import EmailError, send_email
from src.core.security import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    get_password_hash,
    hash_refresh_token,
    verify_password,
)
from src.modules.auth import google_oauth
from src.modules.auth.email_templates import verification_email
from src.modules.auth.models import OrgMembership, Role, User
from src.modules.auth.schemas import LoginRequest, RegisterRequest, TokenResponse
from src.modules.organizations.models import Organization
from src.modules.workspaces.models import Workspace

logger = logging.getLogger(__name__)

GOOGLE_STATE_TTL_SECONDS = 600

# Email verification. Ten minutes is long enough to switch to a mail app and back and
# short enough that a code sitting in an inbox is not a standing credential; five
# wrong guesses burn it (a 6-digit code is only a million possibilities, so the
# attempt cap — not the entropy — is what makes it safe); sixty seconds between
# sends stops the endpoint being used to mail-bomb an address.
VERIFICATION_TTL_SECONDS = 600
VERIFICATION_COOLDOWN_SECONDS = 60
VERIFICATION_MAX_ATTEMPTS = 5


class GoogleSignInError(Exception):
    """
    A Google sign-in that cannot complete. `code` is a short, user-safe slug the
    router puts in the redirect (`/login?error=<code>`); it never carries Google's
    response or any detail about why an account was refused.
    """

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class AuthService:
    def __init__(self, db: AsyncSession, redis: aioredis.Redis):
        self.db = db
        self.redis = redis

    async def register(self, req: RegisterRequest) -> dict:
        """
        Creates a User, Organization, Workspace, and OrgMembership (Owner role).

        Returns a token pair scoped to the new org — or, when email verification is
        required, `{"verification_required": True, "email": ...}` and NO tokens: the
        account exists but cannot sign in until the emailed code is entered.
        """
        require_verification = settings.REQUIRE_EMAIL_VERIFICATION
        if require_verification and not settings.email_enabled and not settings.DEBUG:
            # Fail loudly rather than skip the check: a deployment that thinks it
            # verifies addresses and does not is worse than one that says so.
            # (DEBUG is the one exception — see `_send_verification_code`.)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Sign-up is temporarily unavailable.",
            )

        # Check if user already exists
        existing = (await self.db.execute(select(User).where(User.email == req.email))).scalar_one_or_none()
        if existing:
            # An UNVERIFIED password account is not an owned address, it is an
            # abandoned (or hostile) attempt. If it blocked re-registration, anyone
            # could squat a victim's address with a password of their own and lock
            # the real owner out. So the new attempt takes the account over — new
            # password, new name, the same org renamed — and sends a fresh code.
            # Only whoever can read that mailbox can then complete it.
            if require_verification and existing.email_verified_at is None and not req.invite_token:
                existing.hashed_password = get_password_hash(req.password)
                existing.full_name = req.full_name
                owned = (
                    (
                        await self.db.execute(
                            select(Organization)
                            .join(OrgMembership, OrgMembership.organization_id == Organization.id)
                            .where(OrgMembership.user_id == existing.id)
                            .order_by(OrgMembership.created_at)
                        )
                    )
                    .scalars()
                    .first()
                )
                if owned is not None and req.organization_name:
                    owned.name = req.organization_name
                await self.db.commit()
                await self._send_verification_code(existing)
                return {"verification_required": True, "email": existing.email}
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered",
            )

        # Get system Owner role (use first() in case dev DB has harmless duplicates)
        owner_role = await self.db.execute(select(Role).where(Role.is_system == True, Role.name == "Owner"))  # noqa: E712
        owner_role_obj = owner_role.scalars().first()
        if not owner_role_obj:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="System roles not seeded",
            )

        # 1. Create User
        user = User(
            email=req.email,
            hashed_password=get_password_hash(req.password),
            full_name=req.full_name,
            # Without the requirement there is nothing to check, so the address is
            # recorded as verified — otherwise turning it on later would lock out
            # every account made while it was off.
            email_verified_at=None if require_verification else datetime.now(UTC),
        )
        self.db.add(user)
        await self.db.flush()

        # 1b. Invitation path — join an existing org instead of creating one.
        #
        # The membership row already exists (created as `invited` when the
        # invitation was minted), so this attaches the new user to it rather
        # than inserting a second one. No Organization and no Workspace are
        # created: the invitee is joining somewhere that already has both, and
        # minting a throwaway org for them is the outcome invitations exist to
        # avoid.
        #
        # The invite link is NOT proof of the mailbox (the inviter copies it out of
        # the UI by hand), so it goes through the same code check as everyone else.
        if req.invite_token:
            from src.modules.organizations.service import MemberService

            membership, _role, org = await MemberService(self.db)._resolve_invitation(req.invite_token)
            if (membership.invited_email or "").lower() != req.email.lower():
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"This invitation was sent to {membership.invited_email}.",
                )
            membership.user_id = user.id
            membership.status = "active"
            await self.db.commit()
            if require_verification:
                await self._send_verification_code(user)
                return {"verification_required": True, "email": user.email}
            return await self._generate_token_response(user.id, org.id)

        # 2-4. Organization, default workspace and the Owner membership
        org = await self._provision_org(user.id, req.organization_name, owner_role_obj.id)
        await self.db.commit()

        if require_verification:
            await self._send_verification_code(user)
            return {"verification_required": True, "email": user.email}

        # Generate tokens
        return await self._generate_token_response(user.id, org.id)

    # ------------------------------------------------------------------
    # Email verification
    # ------------------------------------------------------------------

    @staticmethod
    def _verification_key(email: str) -> str:
        return f"email_verify:{hashlib.sha256(email.lower().encode()).hexdigest()}"

    @staticmethod
    def _verification_cooldown_key(email: str) -> str:
        return f"email_verify_cd:{hashlib.sha256(email.lower().encode()).hexdigest()}"

    @staticmethod
    def _code_mac(email: str, code: str) -> str:
        """Keyed hash of the code. Redis holds this, never the code itself."""
        return hmac.new(settings.SECRET_KEY.encode(), f"{email.lower()}:{code}".encode(), hashlib.sha256).hexdigest()

    async def _send_verification_code(self, user: User) -> bool:
        """
        Mint a code, store its hash, and email it. Returns False if skipped for the
        cooldown (a code was sent under a minute ago — the person already has one).
        Raises 503 if the mail provider refuses; the stored code is discarded so a
        message that never arrived cannot be guessed at.
        """
        acquired = await self.redis.set(self._verification_cooldown_key(user.email), "1", ex=VERIFICATION_COOLDOWN_SECONDS, nx=True)
        if not acquired:
            return False

        code = f"{secrets.randbelow(1_000_000):06d}"
        key = self._verification_key(user.email)
        await self.redis.set(
            key,
            json.dumps({"mac": self._code_mac(user.email, code), "attempts": 0}),
            ex=VERIFICATION_TTL_SECONDS,
        )

        if not settings.email_enabled and settings.DEBUG:
            # Development only, and only with no provider configured: log the code so
            # the whole sign-up flow can be driven locally without mailing anyone.
            # Production runs with DEBUG=false, where this branch is unreachable and
            # an unconfigured provider is a 503 instead.
            logger.warning("DEV ONLY - email verification code for %s is %s", user.email, code)
            return True

        subject, html, text = verification_email(
            code=code,
            full_name=user.full_name,
            base_url=settings.FRONTEND_URL,
            expires_minutes=VERIFICATION_TTL_SECONDS // 60,
        )
        try:
            await send_email(to=user.email, subject=subject, html=html, text=text)
        except EmailError as exc:
            logger.error("verification email to a user failed: %s", exc)
            await self.redis.delete(key, self._verification_cooldown_key(user.email))
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="We could not send the verification email. Please try again in a moment.",
            ) from exc
        return True

    async def verify_email(self, email: str, code: str) -> dict:
        """
        Check a code and, if right, mark the address verified and sign the user in.

        Every failure is the same 400 — unknown address, no code outstanding, wrong
        code, burnt code — so the endpoint cannot be used to ask whether an address
        has an account or a pending sign-up.
        """
        invalid = HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That code is invalid or has expired.")

        key = self._verification_key(email)
        raw = await self.redis.get(key)
        if not raw:
            raise invalid
        data = json.loads(raw)

        if data["attempts"] >= VERIFICATION_MAX_ATTEMPTS:
            await self.redis.delete(key)
            raise invalid

        if not hmac.compare_digest(data["mac"], self._code_mac(email, code)):
            data["attempts"] += 1
            await self.redis.set(key, json.dumps(data), keepttl=True)
            raise invalid

        user = (await self.db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            raise invalid

        await self.redis.delete(key)
        if user.email_verified_at is None:
            user.email_verified_at = datetime.now(UTC)

        membership = (
            (
                await self.db.execute(
                    select(OrgMembership).where(OrgMembership.user_id == user.id, OrgMembership.status == "active").order_by(OrgMembership.created_at)
                )
            )
            .scalars()
            .first()
        )
        await self.db.commit()
        if membership is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User does not belong to any active organizations")
        return await self._generate_token_response(user.id, membership.organization_id)

    async def resend_verification(self, email: str) -> None:
        """
        Send a fresh code if this address has an unverified account. Always returns
        quietly: a different response for "no such account" would turn the endpoint
        into an address-existence oracle. Failures are logged, not surfaced.
        """
        if not settings.REQUIRE_EMAIL_VERIFICATION:
            return
        user = (await self.db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None or user.email_verified_at is not None:
            return
        try:
            await self._send_verification_code(user)
        except HTTPException:
            logger.warning("resend of a verification code failed")

    async def _provision_org(self, user_id: uuid.UUID, org_name: str, owner_role_id: uuid.UUID) -> Organization:
        """
        Create an organization with its default workspace and make `user_id` its Owner.

        Shared by password registration and first-time Google sign-in so the two
        cannot drift: a user who arrives either way gets the same org shape. Flushes
        but does NOT commit — the caller owns the transaction.
        """
        # Slugify the org name trivially for now (production would use a proper slugifier)
        slug = org_name.lower().replace(" ", "-") + "-" + str(uuid.uuid4())[:8]
        org = Organization(name=org_name, slug=slug)
        self.db.add(org)
        await self.db.flush()

        self.db.add(Workspace(organization_id=org.id, name="Default Workspace", is_default=True))
        self.db.add(OrgMembership(organization_id=org.id, user_id=user_id, role_id=owner_role_id, status="active"))
        return org

    async def login(self, req: LoginRequest) -> TokenResponse:
        """
        Verifies credentials and returns a token pair.
        """
        # Fetch user with active memberships
        stmt = select(User).options(selectinload(User.memberships.and_(OrgMembership.status == "active"))).where(User.email == req.email)
        result = await self.db.execute(stmt)
        user = result.scalar_one_or_none()

        if not user or not user.hashed_password:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )

        if not verify_password(req.password, user.hashed_password):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )

        # Password was right but the address was never proven. Send a fresh code (the
        # cooldown makes a second attempt a no-op) and tell the client to show the
        # code screen. Only reachable with the correct password, so it reveals
        # nothing to someone who does not already hold the account.
        if settings.REQUIRE_EMAIL_VERIFICATION and user.email_verified_at is None:
            await self._send_verification_code(user)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="email_not_verified")

        if not user.memberships:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User does not belong to any active organizations",
            )

        # Determine target org
        target_org_id = None
        if req.organization_id:
            try:
                requested_org_uuid = uuid.UUID(req.organization_id)
            except ValueError:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid org ID format")

            for m in user.memberships:
                if m.organization_id == requested_org_uuid:
                    target_org_id = requested_org_uuid
                    break
            if not target_org_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Not a member of the requested organization",
                )
        else:
            # Default to the first active membership
            target_org_id = user.memberships[0].organization_id

        return await self._generate_token_response(user.id, target_org_id)

    # ------------------------------------------------------------------
    # Sign in with Google
    # ------------------------------------------------------------------

    @staticmethod
    def _google_state_key(state: str) -> str:
        return f"google_oauth:{hashlib.sha256(state.encode()).hexdigest()}"

    async def google_begin(self) -> tuple[str, str]:
        """
        Start a sign-in. Returns (consent_url, state).

        The PKCE verifier never leaves the server: it is parked in Redis under a
        hash of `state` for ten minutes, single use. The browser carries only
        `state` (in the URL and in a cookie), which is what lets the callback prove
        that the browser finishing the login is the browser that started it.
        """
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        await self.redis.set(self._google_state_key(state), verifier, ex=GOOGLE_STATE_TTL_SECONDS)
        return google_oauth.build_authorization_url(state, verifier), state

    async def google_complete(self, code: str, state: str, cookie_state: str | None) -> TokenResponse:
        """
        Finish a sign-in and return our own token pair. Raises GoogleSignInError.

        Account resolution, in order:
          1. `google_sub` already known  -> that user.
          2. verified email matches an existing user -> link the Google identity to
             that account (the product decision: one person, one account).
          3. otherwise -> a new user who owns a new organization, like /register.
        """
        # Binding: the cookie must equal the state in the URL (login CSRF), and the
        # state must be one we issued and have not yet spent. GETDEL makes it single
        # use — a replayed callback finds nothing.
        if not cookie_state or not hmac.compare_digest(state.encode(), cookie_state.encode()):
            raise GoogleSignInError("state")
        verifier = await self.redis.getdel(self._google_state_key(state))
        if not verifier:
            raise GoogleSignInError("state")

        try:
            profile = await google_oauth.fetch_profile(code, verifier)
        except google_oauth.GoogleOAuthError as exc:
            raise GoogleSignInError("failed") from exc

        # An unverified address proves nothing about who owns it, and step 2 below
        # would otherwise let someone claim a victim's account by registering that
        # address at Google. Refuse before any lookup by email.
        if not profile.email_verified:
            raise GoogleSignInError("unverified")

        user = (await self.db.execute(select(User).where(User.google_sub == profile.sub))).scalar_one_or_none()

        if user is None:
            user = (await self.db.execute(select(User).where(User.email == profile.email))).scalar_one_or_none()
            if user is not None:
                if user.google_sub and user.google_sub != profile.sub:
                    # The address is already tied to a DIFFERENT Google account.
                    raise GoogleSignInError("conflict")
                user.google_sub = profile.sub
                if user.email_verified_at is None:
                    # Google just proved the address, which is what verification is.
                    # But the account was UNVERIFIED until now, so whoever created it
                    # may not be the owner of this mailbox: drop its password, or a
                    # squatter who pre-registered the address keeps a working login.
                    user.hashed_password = None
                    user.email_verified_at = datetime.now(UTC)
                if not user.avatar_url and profile.picture:
                    user.avatar_url = profile.picture
            else:
                owner_role = (await self.db.execute(select(Role).where(Role.is_system.is_(True), Role.name == "Owner"))).scalars().first()
                if not owner_role:
                    raise GoogleSignInError("failed")
                display_name = profile.name or profile.email.split("@")[0]
                user = User(
                    email=profile.email,
                    hashed_password=None,
                    full_name=display_name,
                    avatar_url=profile.picture,
                    google_sub=profile.sub,
                    email_verified_at=datetime.now(UTC),
                )
                self.db.add(user)
                await self.db.flush()
                org = await self._provision_org(user.id, f"{display_name}'s organization", owner_role.id)
                await self.db.commit()
                return await self._generate_token_response(user.id, org.id)

        membership = (
            (
                await self.db.execute(
                    select(OrgMembership).where(OrgMembership.user_id == user.id, OrgMembership.status == "active").order_by(OrgMembership.created_at)
                )
            )
            .scalars()
            .first()
        )
        await self.db.commit()  # persists a newly linked google_sub / avatar
        if membership is None:
            raise GoogleSignInError("no_org")
        return await self._generate_token_response(user.id, membership.organization_id)

    async def switch_org(self, user_id: uuid.UUID, target_org_id_str: str) -> TokenResponse:
        """
        Issues a new token pair scoped to the target org, assuming the user is a member.
        """
        try:
            target_org_id = uuid.UUID(target_org_id_str)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid org ID format")

        stmt = select(OrgMembership).where(
            OrgMembership.user_id == user_id, OrgMembership.organization_id == target_org_id, OrgMembership.status == "active"
        )
        membership = (await self.db.execute(stmt)).scalar_one_or_none()

        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not a member of the requested organization",
            )

        return await self._generate_token_response(user_id, target_org_id)

    async def refresh_tokens(self, refresh_token: str) -> TokenResponse:
        """
        Validates refresh token and issues a new pair (Refresh Token Rotation).
        """
        key = self._refresh_token_key(refresh_token)
        val = await self.redis.get(key)

        if not val:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh token",
            )

        # Parse user_id and org_id
        try:
            user_id_str, org_id_str = val.split(":")
            user_id = uuid.UUID(user_id_str)
            org_id = uuid.UUID(org_id_str)
        except Exception:
            # Delete corrupted token
            await self.redis.delete(key)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token data")

        # Rotate: delete old token
        await self.redis.delete(key)

        # Issue new token pair
        return await self._generate_token_response(user_id, org_id)

    async def logout(self, access_token: str, refresh_token: str | None = None) -> None:
        """
        Revokes the access token (adds to blocklist) and deletes the refresh token.
        """
        if refresh_token:
            await self.redis.delete(self._refresh_token_key(refresh_token))

        try:
            payload = decode_access_token(access_token)
            jti = payload.get("jti")
            exp = payload.get("exp")
            if jti and exp:
                import time

                now = int(time.time())
                ttl = exp - now
                if ttl > 0:
                    await blocklist_token(self.redis, jti, ttl)
        except Exception:
            # If the token is already expired or invalid, we don't need to blocklist it
            pass

    async def _generate_token_response(self, user_id: uuid.UUID, org_id: uuid.UUID) -> TokenResponse:
        """Helper to generate and store token pair."""
        access_token = create_access_token(str(user_id), str(org_id))
        refresh_token = create_refresh_token()

        ttl_seconds = settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
        key = self._refresh_token_key(refresh_token)
        val = f"{user_id}:{org_id}"
        await self.redis.set(key, val, ex=ttl_seconds)

        # We temporarily put refresh_token in access_token field or a custom dict
        # to pass it to the router, which will extract it and put it in a cookie.
        # But TokenResponse schema doesn't have refresh_token.
        # Let's return a dict and let the router construct the response.
        return {"access_token": access_token, "token_type": "bearer", "refresh_token": refresh_token}  # type: ignore

    @staticmethod
    def _refresh_token_key(refresh_token: str) -> str:
        return f"refresh_token:{hash_refresh_token(refresh_token)}"

    async def update_user_role(self, user_id: uuid.UUID, org_id: uuid.UUID, new_role_id: uuid.UUID) -> None:
        """
        TODO: Hook this into the Members module when built.
        Updates user role and invalidates the permission cache.
        """
        # ... db update logic ...
        await invalidate_permissions_cache(self.redis, str(org_id), str(user_id))
