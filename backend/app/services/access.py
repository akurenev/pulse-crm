from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import Company, Contact, Deal, Membership, Role, Task, User
from app.security import AuthContext


def is_employee(context: AuthContext) -> bool:
    return context.role is Role.employee


def deal_access_condition(context: AuthContext, deal: Any = Deal) -> Any:
    if not is_employee(context):
        return sa.true()
    return deal.assignee_id == context.user_id


def task_access_condition(context: AuthContext, task: Any = Task) -> Any:
    if not is_employee(context):
        return sa.true()
    return task.assignee_id == context.user_id


def contact_access_condition(context: AuthContext, contact: Any = Contact) -> Any:
    if not is_employee(context):
        return sa.true()
    return contact.assignee_id == context.user_id


def company_access_condition(context: AuthContext, company: Any = Company) -> Any:
    if not is_employee(context):
        return sa.true()
    own_deal = aliased(Deal)
    visible_contact = aliased(Contact)
    linked_to_own_deal = sa.exists(
        sa.select(own_deal.id).where(
            own_deal.workspace_id == context.workspace_id,
            own_deal.company_id == company.id,
            own_deal.deleted_at.is_(None),
            own_deal.assignee_id == context.user_id,
        )
    )
    linked_to_visible_contact = sa.exists(
        sa.select(visible_contact.id).where(
            visible_contact.workspace_id == context.workspace_id,
            visible_contact.company_id == company.id,
            visible_contact.deleted_at.is_(None),
            contact_access_condition(context, visible_contact),
        )
    )
    return sa.or_(linked_to_own_deal, linked_to_visible_contact)


def forbid_employee(context: AuthContext) -> None:
    if is_employee(context):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")


def _not_found(entity: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{entity} not found")


async def ensure_deal_access(
    db: AsyncSession,
    context: AuthContext,
    entity_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Deal:
    query = sa.select(Deal).where(
        Deal.id == entity_id,
        Deal.workspace_id == context.workspace_id,
        Deal.deleted_at.is_(None),
        deal_access_condition(context),
    )
    if for_update:
        query = query.with_for_update()
    entity = await db.scalar(query)
    if entity is None:
        raise _not_found("deal")
    return entity


async def ensure_contact_access(
    db: AsyncSession,
    context: AuthContext,
    entity_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Contact:
    query = sa.select(Contact).where(
        Contact.id == entity_id,
        Contact.workspace_id == context.workspace_id,
        Contact.deleted_at.is_(None),
        contact_access_condition(context),
    )
    if for_update:
        query = query.with_for_update()
    entity = await db.scalar(query)
    if entity is None:
        raise _not_found("contact")
    return entity


async def ensure_task_access(
    db: AsyncSession,
    context: AuthContext,
    entity_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Task:
    query = sa.select(Task).where(
        Task.id == entity_id,
        Task.workspace_id == context.workspace_id,
        task_access_condition(context),
    )
    if for_update:
        query = query.with_for_update()
    entity = await db.scalar(query)
    if entity is None:
        raise _not_found("task")
    return entity


async def ensure_company_access(
    db: AsyncSession,
    context: AuthContext,
    entity_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Company:
    query = sa.select(Company).where(
        Company.id == entity_id,
        Company.workspace_id == context.workspace_id,
        Company.deleted_at.is_(None),
        company_access_condition(context),
    )
    if for_update:
        query = query.with_for_update()
    entity = await db.scalar(query)
    if entity is None:
        raise _not_found("company")
    return entity


async def notification_target_access_allowed(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    recipient_id: uuid.UUID | None,
    target_entity_type: str | None,
    target_entity_id: uuid.UUID | None,
) -> bool:
    """Authorize a materialized internal notification target at delivery time."""

    if recipient_id is None:
        return False
    role = await db.scalar(
        sa.select(Membership.role)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.workspace_id == workspace_id,
            Membership.user_id == recipient_id,
            User.is_active.is_(True),
        )
    )
    if role is None:
        return False
    if role is not Role.employee:
        return True
    if target_entity_id is None:
        return False
    if target_entity_type == "deal":
        return bool(
            await db.scalar(
                sa.select(Deal.id).where(
                    Deal.id == target_entity_id,
                    Deal.workspace_id == workspace_id,
                    Deal.deleted_at.is_(None),
                    Deal.assignee_id == recipient_id,
                )
            )
        )
    if target_entity_type == "task":
        return bool(
            await db.scalar(
                sa.select(Task.id).where(
                    Task.id == target_entity_id,
                    Task.workspace_id == workspace_id,
                    Task.assignee_id == recipient_id,
                )
            )
        )
    return False
