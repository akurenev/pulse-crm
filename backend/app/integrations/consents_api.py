"""Workspace-scoped management of confirmed client notification consents."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.integrations.identity import IdentityNormalizationError, normalize_email_address
from app.integrations.models import ConsentStatus, ContactChannelConsent
from app.models import Contact
from app.security import CurrentMutationUser, CurrentUser
from app.services.events import record_domain_event

router = APIRouter(tags=["contact-consents"])
MAX_EVIDENCE_BYTES = 8 * 1024
SOURCE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,99}$")
PURPOSE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,99}$")


class ConsentChannel(StrEnum):
    email = "email"
    telegram = "telegram"
    max = "max"


class ConsentEvidence(BaseModel):
    source: str = Field(min_length=1, max_length=100)
    evidence: dict[str, Any] = Field(min_length=1, max_length=50)

    @field_validator("source")
    @classmethod
    def valid_source(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not SOURCE_PATTERN.fullmatch(normalized):
            raise ValueError("source must be a lowercase identifier")
        return normalized

    @field_validator("evidence")
    @classmethod
    def valid_evidence(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        except (TypeError, ValueError) as exc:
            raise ValueError("evidence must be JSON serializable") from exc
        if len(encoded) > MAX_EVIDENCE_BYTES:
            raise ValueError("evidence must not exceed 8 KB")
        return value


class ConsentGrant(ConsentEvidence):
    channel: ConsentChannel
    address: str = Field(min_length=1, max_length=512)
    purpose: str = Field(default="notifications", min_length=1, max_length=100)

    @field_validator("address")
    @classmethod
    def valid_address(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped or any(character in stripped for character in "\r\n\x00"):
            raise ValueError("address is invalid")
        return stripped

    @field_validator("purpose")
    @classmethod
    def valid_purpose(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not PURPOSE_PATTERN.fullmatch(normalized):
            raise ValueError("purpose must be a lowercase identifier")
        return normalized


class ConsentRevoke(ConsentEvidence):
    pass


class ConsentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    contact_id: uuid.UUID
    channel: ConsentChannel
    address: str
    normalized_address: str
    purpose: str
    status: ConsentStatus
    source: str
    evidence: dict[str, Any]
    granted_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime


async def _contact_or_404(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    contact_id: uuid.UUID,
) -> Contact:
    contact = await db.scalar(
        sa.select(Contact).where(
            Contact.id == contact_id,
            Contact.workspace_id == workspace_id,
            Contact.deleted_at.is_(None),
        )
    )
    if contact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="contact not found")
    return contact


def _normalized_address(channel: ConsentChannel, address: str) -> str:
    if channel is ConsentChannel.email:
        try:
            return normalize_email_address(address)
        except IdentityNormalizationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
    return address.strip()


def _event_payload(consent: ContactChannelConsent) -> dict[str, Any]:
    return {
        "consent_id": str(consent.id),
        "contact_id": str(consent.contact_id),
        "channel": consent.channel,
        "address": consent.address,
        "normalized_address": consent.normalized_address,
        "purpose": consent.purpose,
        "source": consent.source,
        "evidence": consent.evidence,
    }


@router.get("/contacts/{contact_id}/consents", response_model=list[ConsentRead])
async def list_contact_consents(
    contact_id: uuid.UUID,
    context: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> list[ContactChannelConsent]:
    await _contact_or_404(db, workspace_id=context.workspace_id, contact_id=contact_id)
    return list(
        (
            await db.scalars(
                sa.select(ContactChannelConsent)
                .where(
                    ContactChannelConsent.workspace_id == context.workspace_id,
                    ContactChannelConsent.contact_id == contact_id,
                )
                .order_by(
                    ContactChannelConsent.created_at.desc(),
                    ContactChannelConsent.id.desc(),
                )
            )
        ).all()
    )


@router.post("/contacts/{contact_id}/consents", response_model=ConsentRead)
async def grant_contact_consent(
    contact_id: uuid.UUID,
    payload: ConsentGrant,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> ContactChannelConsent:
    await _contact_or_404(db, workspace_id=context.workspace_id, contact_id=contact_id)
    normalized_address = _normalized_address(payload.channel, payload.address)
    consent = await db.scalar(
        sa.select(ContactChannelConsent)
        .where(
            ContactChannelConsent.workspace_id == context.workspace_id,
            ContactChannelConsent.contact_id == contact_id,
            ContactChannelConsent.channel == payload.channel.value,
            ContactChannelConsent.normalized_address == normalized_address,
            ContactChannelConsent.purpose == payload.purpose,
        )
        .with_for_update()
    )
    now = datetime.now(UTC)
    changed = False
    if consent is None:
        consent = ContactChannelConsent(
            workspace_id=context.workspace_id,
            contact_id=contact_id,
            channel=payload.channel.value,
            address=payload.address,
            normalized_address=normalized_address,
            purpose=payload.purpose,
            status=ConsentStatus.granted,
            evidence=payload.evidence,
            source=payload.source,
            granted_at=now,
        )
        db.add(consent)
        try:
            await db.flush()
        except IntegrityError as exc:
            # A concurrent grant won the unique key. Treat the request as the
            # same idempotent operation and return the committed record.
            await db.rollback()
            consent = await db.scalar(
                sa.select(ContactChannelConsent).where(
                    ContactChannelConsent.workspace_id == context.workspace_id,
                    ContactChannelConsent.contact_id == contact_id,
                    ContactChannelConsent.channel == payload.channel.value,
                    ContactChannelConsent.normalized_address == normalized_address,
                    ContactChannelConsent.purpose == payload.purpose,
                )
            )
            if consent is None:
                raise exc
            return cast(ContactChannelConsent, consent)
        changed = True
    elif consent.status is ConsentStatus.revoked:
        consent.address = payload.address
        consent.status = ConsentStatus.granted
        consent.evidence = payload.evidence
        consent.source = payload.source
        consent.granted_at = now
        consent.revoked_at = None
        changed = True

    if changed:
        record_domain_event(
            db,
            workspace_id=context.workspace_id,
            event_type="contact.consent.granted",
            entity_type="contact",
            entity_id=contact_id,
            actor_id=context.user_id,
            payload=_event_payload(consent),
        )
        await db.commit()
        await db.refresh(consent)
    return consent


@router.post(
    "/contacts/{contact_id}/consents/{consent_id}/revoke",
    response_model=ConsentRead,
)
async def revoke_contact_consent(
    contact_id: uuid.UUID,
    consent_id: uuid.UUID,
    payload: ConsentRevoke,
    context: CurrentMutationUser,
    db: AsyncSession = Depends(get_session),
) -> ContactChannelConsent:
    await _contact_or_404(db, workspace_id=context.workspace_id, contact_id=contact_id)
    consent = await db.scalar(
        sa.select(ContactChannelConsent)
        .where(
            ContactChannelConsent.id == consent_id,
            ContactChannelConsent.workspace_id == context.workspace_id,
            ContactChannelConsent.contact_id == contact_id,
        )
        .with_for_update()
    )
    if consent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="consent not found")
    if consent.status is ConsentStatus.revoked:
        return consent

    consent.status = ConsentStatus.revoked
    consent.revoked_at = datetime.now(UTC)
    event_payload = _event_payload(consent)
    event_payload.update(
        {
            "revocation_source": payload.source,
            "revocation_evidence": payload.evidence,
        }
    )
    record_domain_event(
        db,
        workspace_id=context.workspace_id,
        event_type="contact.consent.revoked",
        entity_type="contact",
        entity_id=contact_id,
        actor_id=context.user_id,
        payload=event_payload,
    )
    await db.commit()
    await db.refresh(consent)
    return consent
