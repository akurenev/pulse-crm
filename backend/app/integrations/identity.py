"""Conservative contact-point normalization and ambiguity-aware matching."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import StrEnum

import sqlalchemy as sa
from email_validator import EmailNotValidError, validate_email
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.models import ContactPoint, ContactPointKind, ExternalIdentity
from app.models import Contact


class IdentityNormalizationError(ValueError):
    pass


class MatchKind(StrEnum):
    none = "none"
    unique = "unique"
    ambiguous = "ambiguous"


@dataclass(frozen=True, slots=True)
class IdentityMatch:
    kind: MatchKind
    contact_ids: tuple[uuid.UUID, ...]

    @property
    def contact_id(self) -> uuid.UUID | None:
        return self.contact_ids[0] if self.kind is MatchKind.unique else None


def normalize_email_address(value: str) -> str:
    try:
        result = validate_email(value.strip(), check_deliverability=False)
    except EmailNotValidError as exc:
        raise IdentityNormalizationError("invalid email address") from exc
    # CRM matching is intentionally case-insensitive.  We retain the original
    # value separately in ContactPoint for display and outbound delivery.
    return result.normalized.casefold()


def normalize_phone_number(value: str, *, default_country_code: str = "7") -> str:
    raw = value.strip()
    if re.search(r"(?:доб\.?|ext\.?|x)\s*\d+", raw, flags=re.IGNORECASE):
        raw = re.split(r"(?:доб\.?|ext\.?|x)\s*\d+", raw, maxsplit=1, flags=re.IGNORECASE)[0]
    digits = "".join(character for character in raw if character.isdecimal())
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("8") and default_country_code == "7":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = default_country_code + digits
    if not 8 <= len(digits) <= 15:
        raise IdentityNormalizationError("invalid phone number")
    return "+" + digits


def connection_scope(connection_id: uuid.UUID | None) -> str:
    return str(connection_id) if connection_id is not None else "global"


async def sync_contact_points(session: AsyncSession, contact: Contact) -> None:
    """Replace normalized contact points from the contact's canonical fields.

    The operation participates in the surrounding contact transaction, so a
    partially updated identity index is never visible.
    """

    await session.execute(
        sa.delete(ContactPoint).where(
            ContactPoint.workspace_id == contact.workspace_id,
            ContactPoint.contact_id == contact.id,
        )
    )
    email_values = _ordered_values(contact.primary_email, contact.emails)
    phone_values = _ordered_values(contact.primary_phone, contact.phones)
    points: list[ContactPoint] = []
    seen: set[tuple[ContactPointKind, str]] = set()
    for kind, values in (
        (ContactPointKind.email, email_values),
        (ContactPointKind.phone, phone_values),
    ):
        for index, value in enumerate(values):
            normalized = (
                normalize_email_address(value)
                if kind is ContactPointKind.email
                else normalize_phone_number(value)
            )
            identity = (kind, normalized)
            if identity in seen:
                continue
            seen.add(identity)
            points.append(
                ContactPoint(
                    workspace_id=contact.workspace_id,
                    contact_id=contact.id,
                    kind=kind,
                    value=value,
                    normalized_value=normalized,
                    is_primary=index == 0,
                )
            )
    session.add_all(points)
    await session.flush()


def _ordered_values(primary: str | None, values: list[str]) -> list[str]:
    ordered: list[str] = []
    for value in ([primary] if primary else []) + list(values or []):
        stripped = value.strip()
        if stripped and stripped not in ordered:
            ordered.append(stripped)
    return ordered


async def match_contact_point(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    kind: ContactPointKind,
    value: str,
) -> IdentityMatch:
    normalized = (
        normalize_email_address(value)
        if kind is ContactPointKind.email
        else normalize_phone_number(value)
    )
    contact_ids = tuple(
        dict.fromkeys(
            (
                await session.scalars(
                    sa.select(ContactPoint.contact_id).where(
                        ContactPoint.workspace_id == workspace_id,
                        ContactPoint.kind == kind,
                        ContactPoint.normalized_value == normalized,
                    )
                )
            ).all()
        )
    )
    return _match(contact_ids)


async def match_external_identity(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    provider: str,
    external_user_id: str,
    channel_connection_id: uuid.UUID | None,
) -> IdentityMatch:
    contact_ids = tuple(
        dict.fromkeys(
            (
                await session.scalars(
                    sa.select(ExternalIdentity.contact_id).where(
                        ExternalIdentity.workspace_id == workspace_id,
                        ExternalIdentity.provider == provider,
                        ExternalIdentity.connection_scope
                        == connection_scope(channel_connection_id),
                        ExternalIdentity.external_user_id == external_user_id,
                    )
                )
            ).all()
        )
    )
    return _match(contact_ids)


def _match(contact_ids: tuple[uuid.UUID, ...]) -> IdentityMatch:
    if not contact_ids:
        return IdentityMatch(kind=MatchKind.none, contact_ids=())
    if len(contact_ids) == 1:
        return IdentityMatch(kind=MatchKind.unique, contact_ids=contact_ids)
    # The caller must mark the inbound request for review rather than selecting
    # one customer by accident.
    return IdentityMatch(kind=MatchKind.ambiguous, contact_ids=contact_ids)
