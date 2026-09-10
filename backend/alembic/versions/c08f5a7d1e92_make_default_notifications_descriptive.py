"""make default notifications descriptive

Revision ID: c08f5a7d1e92
Revises: 2e4a6c8d0f13
Create Date: 2026-09-10 12:15:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c08f5a7d1e92"
down_revision: str | None = "2e4a6c8d0f13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_BODY = "В Pulse CRM произошло новое событие. Откройте карточку, чтобы увидеть детали."
DEFAULT_BODY = "{event_summary}"
DEFAULT_SUBJECT = "{event_title}"
EVENT_PRESENTATION = (
    ("lead.created", "Новый лид", "Поступил новый лид."),
    ("deal.assigned", "Сделка назначена", "Вам назначена сделка."),
    ("deal.stage_changed", "Сделка сменила этап", "Проверьте новый этап сделки."),
    ("deal.inactive", "Нет активности по сделке", "По сделке не было активности более семи дней."),
    ("task.due_soon", "Напоминание о задаче", "Срок задачи наступает."),
    ("task.overdue", "Задача просрочена", "Срок задачи уже истёк."),
    ("purchase.due_soon", "Следующая покупка", "Пора связаться с клиентом по следующей покупке."),
    (
        "message.inbound.received",
        "Новое входящее сообщение",
        "Поступило новое сообщение от клиента.",
    ),
)


def upgrade() -> None:
    # Only migrate the former built-in text. Administrators' custom templates
    # remain exactly as configured.
    op.execute(
        sa.text(
            "UPDATE notification_templates "
            "SET body_template = :default_body "
            "WHERE body_template = :legacy_body"
        ).bindparams(default_body=DEFAULT_BODY, legacy_body=LEGACY_BODY)
    )
    op.execute(
        sa.text(
            "UPDATE notification_templates "
            "SET subject_template = :default_subject "
            "WHERE body_template = :default_body AND subject_template IS NULL"
        ).bindparams(default_body=DEFAULT_BODY, default_subject=DEFAULT_SUBJECT)
    )
    # Update the already-visible in-app history as well. It is intentionally
    # restricted to the previous stock text and event types that have a
    # human-readable presentation, so custom notification copy is preserved.
    for event_type, subject, body in EVENT_PRESENTATION:
        op.execute(
            sa.text(
                "UPDATE notification_deliveries "
                "SET subject = CASE WHEN subject IS NULL THEN :subject ELSE subject END, "
                "body = :body "
                "WHERE channel IN ('in_app', 'web_push') AND body = :legacy_body "
                "AND rule_id IN ("
                "SELECT id FROM notification_rules WHERE event_type = :event_type"
                ")"
            ).bindparams(
                event_type=event_type,
                subject=subject,
                body=body,
                legacy_body=LEGACY_BODY,
            )
        )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE notification_templates "
            "SET body_template = :legacy_body "
            "WHERE body_template = :default_body"
        ).bindparams(default_body=DEFAULT_BODY, legacy_body=LEGACY_BODY)
    )
    op.execute(
        sa.text(
            "UPDATE notification_templates "
            "SET subject_template = NULL "
            "WHERE subject_template = :default_subject"
        ).bindparams(default_subject=DEFAULT_SUBJECT)
    )
