from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

import anyio
import sqlalchemy as sa
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.models import (
    Invitation,
    Membership,
    Pipeline,
    Role,
    Session,
    Source,
    Stage,
    StageType,
    User,
    Workspace,
)
from app.schemas import (
    AuthResponse,
    BootstrapRequest,
    InvitationAccept,
    InvitationCreate,
    InvitationCreated,
    LoginRequest,
    UserRead,
    WorkspaceRead,
)
from app.security import (
    SESSION_COOKIE,
    CurrentAdmin,
    CurrentMutationUser,
    CurrentUser,
    create_session,
    digest_token,
    hash_password,
    new_token,
    normalize_email,
    set_session_cookie,
    verify_password,
)
from app.services.events import record_domain_event

router = APIRouter(prefix="/auth", tags=["auth"])
users_router = APIRouter(tags=["users"])


def _auth_response(user: User, workspace: Workspace, role: Role, csrf_token: str) -> AuthResponse:
    return AuthResponse(
        user=UserRead(id=user.id, email=user.email, full_name=user.full_name, role=role),
        workspace=WorkspaceRead.model_validate(workspace),
        csrf_token=csrf_token,
    )


@router.post("/bootstrap", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def bootstrap(
    payload: BootstrapRequest,
    request: Request,
    response: Response,
    bootstrap_token: Annotated[str | None, Header(alias="X-Bootstrap-Token")] = None,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    if settings.bootstrap_token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if not bootstrap_token or not secrets.compare_digest(bootstrap_token, settings.bootstrap_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid bootstrap token")
    if (await db.scalar(sa.select(sa.func.count()).select_from(Workspace))) != 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="workspace already exists")

    password_hash = await anyio.to_thread.run_sync(hash_password, payload.password)
    workspace = Workspace(name=payload.workspace_name.strip(), slug=payload.workspace_slug)
    user = User(
        email=normalize_email(str(payload.email)),
        full_name=payload.full_name.strip(),
        password_hash=password_hash,
    )
    db.add_all([workspace, user])
    try:
        await db.flush()
        membership = Membership(workspace_id=workspace.id, user_id=user.id, role=Role.owner)
        pipeline = Pipeline(workspace_id=workspace.id, name="Продажи", position=0)
        db.add_all([membership, pipeline])
        await db.flush()
        db.add_all(
            [
                Stage(
                    workspace_id=workspace.id,
                    pipeline_id=pipeline.id,
                    name="Новая заявка",
                    color="#38BDF8",
                    position=0,
                ),
                Stage(
                    workspace_id=workspace.id,
                    pipeline_id=pipeline.id,
                    name="В работе",
                    color="#FBBF24",
                    position=1,
                ),
                Stage(
                    workspace_id=workspace.id,
                    pipeline_id=pipeline.id,
                    name="Успешно",
                    color="#22C55E",
                    position=2,
                    stage_type=StageType.won,
                ),
                Stage(
                    workspace_id=workspace.id,
                    pipeline_id=pipeline.id,
                    name="Закрыто и не реализовано",
                    color="#94A3B8",
                    position=3,
                    stage_type=StageType.lost,
                ),
            ]
        )
        source_names = {
            "manual": "Вручную",
            "email": "Email",
            "max": "MAX",
            "telegram": "Telegram",
            "webhook": "Webhook",
            "html_form": "HTML-форма",
            "amo_import": "Импорт amoCRM",
        }
        db.add_all(
            [
                Source(workspace_id=workspace.id, key=key, name=name)
                for key, name in source_names.items()
            ]
        )
        _, token, csrf_token = await create_session(
            db, user=user, workspace=workspace, request=request, settings=settings
        )
        record_domain_event(
            db,
            workspace_id=workspace.id,
            event_type="workspace.bootstrapped",
            entity_type="workspace",
            entity_id=workspace.id,
            actor_id=user.id,
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="workspace already exists"
        ) from exc

    set_session_cookie(response, token, settings)
    return _auth_response(user, workspace, Role.owner, csrf_token)


@router.post("/login", response_model=AuthResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    row = (
        await db.execute(
            sa.select(User, Membership, Workspace)
            .join(Membership, Membership.user_id == User.id)
            .join(Workspace, Workspace.id == Membership.workspace_id)
            .where(User.email == normalize_email(str(payload.email)), User.is_active.is_(True))
            .order_by(Membership.created_at)
            .limit(1)
        )
    ).one_or_none()
    valid = False
    if row is not None:
        user, membership, workspace = row
        valid = await anyio.to_thread.run_sync(
            verify_password, user.password_hash, payload.password
        )
    if row is None or not valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    _, token, csrf_token = await create_session(
        db, user=user, workspace=workspace, request=request, settings=settings
    )
    await db.commit()
    set_session_cookie(response, token, settings)
    return _auth_response(user, workspace, membership.role, csrf_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> None:
    await db.execute(
        sa.update(Session)
        .where(Session.id == context.session.id, Session.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    await db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=AuthResponse)
async def me(
    context: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> AuthResponse:
    """Restore a browser session and rotate its CSRF token.

    Only the digest is persisted. Returning a fresh raw token here lets an SPA
    recover safely after reload without exposing the HttpOnly session cookie.
    """

    csrf_token = new_token()
    await db.execute(
        sa.update(Session)
        .where(
            Session.id == context.session.id,
            Session.workspace_id == context.workspace_id,
            Session.revoked_at.is_(None),
        )
        .values(csrf_token_hash=digest_token(csrf_token))
    )
    await db.commit()
    return _auth_response(context.user, context.workspace, context.role, csrf_token=csrf_token)


@router.post("/accept-invitation", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def accept_invitation(
    payload: InvitationAccept,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    invitation = await db.scalar(
        sa.select(Invitation).where(
            Invitation.token_hash == digest_token(payload.token),
            Invitation.accepted_at.is_(None),
            Invitation.expires_at > sa.func.now(),
        )
    )
    if invitation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="invitation is invalid or expired"
        )
    workspace = await db.get(Workspace, invitation.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found")

    user = await db.scalar(sa.select(User).where(User.email == invitation.email))
    if user is None:
        password_hash = await anyio.to_thread.run_sync(hash_password, payload.password)
        user = User(
            email=invitation.email, full_name=payload.full_name.strip(), password_hash=password_hash
        )
        db.add(user)
        await db.flush()
    elif not await anyio.to_thread.run_sync(verify_password, user.password_hash, payload.password):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="an account already exists; provide its current password",
        )

    membership = await db.scalar(
        sa.select(Membership).where(
            Membership.workspace_id == workspace.id, Membership.user_id == user.id
        )
    )
    if membership is None:
        membership = Membership(workspace_id=workspace.id, user_id=user.id, role=invitation.role)
        db.add(membership)
    invitation.accepted_at = datetime.now(UTC)
    _, token, csrf_token = await create_session(
        db, user=user, workspace=workspace, request=request, settings=settings
    )
    record_domain_event(
        db,
        workspace_id=workspace.id,
        event_type="user.joined",
        entity_type="user",
        entity_id=user.id,
        actor_id=user.id,
        payload={"role": membership.role.value},
    )
    await db.commit()
    set_session_cookie(response, token, settings)
    return _auth_response(user, workspace, membership.role, csrf_token)


@users_router.get("/users", response_model=list[UserRead])
async def list_users(
    context: CurrentUser, db: AsyncSession = Depends(get_session)
) -> list[UserRead]:
    rows = (
        await db.execute(
            sa.select(User, Membership.role)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.workspace_id == context.workspace_id, User.is_active.is_(True))
            .order_by(User.full_name)
        )
    ).all()
    return [
        UserRead(id=user.id, email=user.email, full_name=user.full_name, role=role)
        for user, role in rows
    ]


@users_router.post(
    "/invitations", response_model=InvitationCreated, status_code=status.HTTP_201_CREATED
)
async def invite_user(
    payload: InvitationCreate,
    context: CurrentAdmin,
    db: AsyncSession = Depends(get_session),
) -> InvitationCreated:
    if context.role is Role.admin and payload.role is Role.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only the workspace owner can invite administrators",
        )
    email = normalize_email(str(payload.email))
    existing = await db.scalar(
        sa.select(Membership.id)
        .join(User, User.id == Membership.user_id)
        .where(Membership.workspace_id == context.workspace_id, User.email == email)
    )
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="user is already a member")
    raw_token = secrets.token_urlsafe(32)
    invitation = Invitation(
        workspace_id=context.workspace_id,
        email=email,
        role=payload.role,
        token_hash=digest_token(raw_token),
        invited_by_id=context.user_id,
        expires_at=datetime.now(UTC) + timedelta(hours=payload.expires_in_hours),
    )
    db.add(invitation)
    await db.flush()
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="invitation.created",
        entity_type="invitation",
        entity_id=invitation.id,
        actor_id=context.user_id,
        payload={"email": email, "role": payload.role.value},
    )
    await db.commit()
    return InvitationCreated(
        id=invitation.id,
        email=invitation.email,
        role=invitation.role,
        expires_at=invitation.expires_at,
        token=raw_token,
    )
