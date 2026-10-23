"""Personal owner-scoped mailboxes and independently shared CRM snapshots.

Revision ID: 4d6f8a0b2c15
Revises: c08f5a7d1e92
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "4d6f8a0b2c15"
down_revision: str | None = "c08f5a7d1e92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mail_accounts",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("username", sa.String(length=320), nullable=False),
        sa.Column("imap_host", sa.String(length=253), nullable=False),
        sa.Column("imap_port", sa.Integer(), nullable=False),
        sa.Column("imap_security", sa.String(length=16), nullable=False),
        sa.Column("smtp_host", sa.String(length=253), nullable=False),
        sa.Column("smtp_port", sa.Integer(), nullable=False),
        sa.Column("smtp_security", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("sync_days", sa.Integer(), nullable=False),
        sa.Column("encrypted_credentials", sa.LargeBinary(), nullable=True),
        sa.Column("credentials_key_id", sa.String(length=100), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=300), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_mail_accounts_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_mail_accounts_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_accounts")),
        sa.UniqueConstraint(
            "workspace_id", "owner_id", "email", name="uq_mail_account_owner_email"
        ),
    )
    op.create_index(
        "ix_mail_accounts_owner",
        "mail_accounts",
        ["workspace_id", "owner_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_mail_accounts_status", "mail_accounts", ["status", "last_synced_at"], unique=False
    )
    op.create_table(
        "mail_folders",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("display_name", sa.String(length=512), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("unread_count", sa.Integer(), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("uidvalidity", sa.BigInteger(), nullable=True),
        sa.Column("last_uid", sa.BigInteger(), nullable=False),
        sa.Column(
            "sync_state",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["mail_accounts.id"],
            name=op.f("fk_mail_folders_account_id_mail_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_mail_folders_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_folders")),
        sa.UniqueConstraint("account_id", "name", name="uq_mail_folder_account_name"),
    )
    op.create_index(
        "ix_mail_folders_account", "mail_folders", ["workspace_id", "account_id"], unique=False
    )
    op.create_table(
        "mail_messages",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("folder_id", sa.Uuid(), nullable=False),
        sa.Column("subject", sa.String(length=998), nullable=False),
        sa.Column("from_address", sa.String(length=320), nullable=False),
        sa.Column("from_name", sa.String(length=320), nullable=True),
        sa.Column(
            "to_addresses",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "cc_addresses",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "bcc_addresses",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("internet_message_id", sa.String(length=998), nullable=True),
        sa.Column("in_reply_to", sa.String(length=998), nullable=True),
        sa.Column(
            "references",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("thread_key", sa.String(length=998), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("is_read", sa.Boolean(), nullable=False),
        sa.Column("is_starred", sa.Boolean(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=300), nullable=True),
        sa.Column("imap_uid", sa.BigInteger(), nullable=True),
        sa.Column("imap_uidvalidity", sa.BigInteger(), nullable=True),
        sa.Column(
            "metadata_json",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["mail_accounts.id"],
            name=op.f("fk_mail_messages_account_id_mail_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["folder_id"],
            ["mail_folders.id"],
            name=op.f("fk_mail_messages_folder_id_mail_folders"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_mail_messages_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_messages")),
        sa.UniqueConstraint(
            "folder_id", "imap_uidvalidity", "imap_uid", name="uq_mail_message_uid"
        ),
    )
    op.create_index(
        "ix_mail_messages_account_created",
        "mail_messages",
        ["workspace_id", "account_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_mail_messages_folder", "mail_messages", ["folder_id", "created_at", "id"], unique=False
    )
    op.create_index(
        "ix_mail_messages_internet_id",
        "mail_messages",
        ["account_id", "internet_message_id"],
        unique=False,
    )
    op.create_table(
        "mail_attachments",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["mail_messages.id"],
            name=op.f("fk_mail_attachments_message_id_mail_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_mail_attachments_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_attachments")),
        sa.UniqueConstraint("object_key", name="uq_mail_attachment_object_key"),
    )
    op.create_index(
        "ix_mail_attachments_message",
        "mail_attachments",
        ["workspace_id", "message_id"],
        unique=False,
    )
    op.create_table(
        "mail_links",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("deal_id", sa.Uuid(), nullable=False),
        sa.Column("note_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["deal_id"], ["deals.id"], name=op.f("fk_mail_links_deal_id_deals"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["mail_messages.id"],
            name=op.f("fk_mail_links_message_id_mail_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["note_id"],
            ["activity_events.id"],
            name=op.f("fk_mail_links_note_id_activity_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_mail_links_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_links")),
        sa.UniqueConstraint("message_id", "deal_id", name="uq_mail_link_message_deal"),
    )


def downgrade() -> None:
    op.drop_table("mail_links")
    op.drop_table("mail_attachments")
    op.drop_table("mail_messages")
    op.drop_table("mail_folders")
    op.drop_table("mail_accounts")
