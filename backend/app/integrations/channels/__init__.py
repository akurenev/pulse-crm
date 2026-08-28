"""Provider adapters implementing one internal messaging contract."""

from app.integrations.channels.base import (
    AdapterHealth,
    AttachmentReference,
    ChannelAdapter,
    NormalizedInboundMessage,
    OutboundMessage,
    SendResult,
)
from app.integrations.channels.email import EmailAdapter, EmailEnvelope
from app.integrations.channels.max import MaxAdapter
from app.integrations.channels.telegram import TelegramAdapter

__all__ = [
    "AdapterHealth",
    "AttachmentReference",
    "ChannelAdapter",
    "EmailAdapter",
    "EmailEnvelope",
    "MaxAdapter",
    "NormalizedInboundMessage",
    "OutboundMessage",
    "SendResult",
    "TelegramAdapter",
]
