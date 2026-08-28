from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import sqlalchemy as sa
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import Membership, Role, Session, User, Workspace

SESSION_COOKIE = "pulse_session"
_password_hasher = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=4, type=Type.ID)


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def digest_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True)
class AuthContext:
    user: User
    workspace: Workspace
    membership: Membership
    session: Session
    via_cookie: bool

    @property
    def workspace_id(self):  # type: ignore[no-untyped-def]
        return self.workspace.id

    @property
    def user_id(self):  # type: ignore[no-untyped-def]
        return self.user.id

    @property
    def role(self) -> Role:
        return self.membership.role


async def create_session(
    db: AsyncSession,
    *,
    user: User,
    workspace: Workspace,
    request: Request,
    settings: Settings,
) -> tuple[Session, str, str]:
    token = new_token()
    csrf_token = new_token()
    record = Session(
        user_id=user.id,
        workspace_id=workspace.id,
        token_hash=digest_token(token),
        csrf_token_hash=digest_token(csrf_token),
        expires_at=datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours),
        user_agent=(request.headers.get("user-agent") or "")[:512] or None,
        ip_address=request.client.host[:64] if request.client else None,
    )
    db.add(record)
    await db.flush()
    return record, token, csrf_token


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


async def get_auth_context(
    request: Request,
) -> AuthContext:
    cookie_token = request.cookies.get(SESSION_COOKIE)
    authorization = request.headers.get("authorization", "")
    bearer_token = (
        authorization[7:].strip() if authorization.lower().startswith("bearer ") else None
    )
    token = bearer_token or cookie_token
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")

    query = (
        sa.select(Session, User, Workspace, Membership)
        .join(User, User.id == Session.user_id)
        .join(Workspace, Workspace.id == Session.workspace_id)
        .join(
            Membership,
            sa.and_(
                Membership.workspace_id == Session.workspace_id,
                Membership.user_id == Session.user_id,
            ),
        )
        .where(
            Session.token_hash == digest_token(token),
            Session.revoked_at.is_(None),
            Session.expires_at > sa.func.now(),
            User.is_active.is_(True),
        )
    )
    async with SessionLocal() as db:
        row = (await db.execute(query)).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid session")
    session_record, user, workspace, membership = row
    return AuthContext(
        user=user,
        workspace=workspace,
        membership=membership,
        session=session_record,
        via_cookie=bearer_token is None,
    )


async def require_csrf(
    request: Request,
    context: Annotated[AuthContext, Depends(get_auth_context)],
) -> AuthContext:
    if context.via_cookie and request.method not in {"GET", "HEAD", "OPTIONS"}:
        csrf_token = request.headers.get("x-csrf-token", "")
        if not csrf_token or not secrets.compare_digest(
            digest_token(csrf_token), context.session.csrf_token_hash
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid CSRF token")
    return context


def require_roles(*roles: Role) -> Callable[..., Coroutine[Any, Any, AuthContext]]:
    async def dependency(
        context: Annotated[AuthContext, Depends(require_csrf)],
    ) -> AuthContext:
        if context.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
        return context

    return dependency


CurrentUser = Annotated[AuthContext, Depends(get_auth_context)]
CurrentMutationUser = Annotated[AuthContext, Depends(require_csrf)]
CurrentAdmin = Annotated[AuthContext, Depends(require_roles(Role.owner, Role.admin))]


SettingsDependency = Annotated[Settings, Depends(get_settings)]
