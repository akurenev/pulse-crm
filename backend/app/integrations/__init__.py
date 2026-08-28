"""Pulse CRM integrations domain.

The package intentionally contains no FastAPI wiring.  Its models and services
can be imported by the single web application once the core router and
migration are ready, while keeping all external side effects behind small
protocols that are straightforward to test.
"""

from app.integrations.models import (
    Attachment,
    ChannelConnection,
    ContactChannelConsent,
    ContactPoint,
    Conversation,
    ExternalEntityMap,
    ExternalIdentity,
    Form,
    FormRateLimitBucket,
    FormSubmission,
    ImportJob,
    InboundEvent,
    Message,
    NotificationDelivery,
    NotificationRule,
    NotificationTemplate,
    PurchaseSchedule,
    WebhookEndpoint,
)

__all__ = [
    "Attachment",
    "ChannelConnection",
    "ContactChannelConsent",
    "ContactPoint",
    "Conversation",
    "ExternalIdentity",
    "ExternalEntityMap",
    "Form",
    "FormRateLimitBucket",
    "FormSubmission",
    "ImportJob",
    "InboundEvent",
    "Message",
    "NotificationDelivery",
    "NotificationRule",
    "NotificationTemplate",
    "PurchaseSchedule",
    "WebhookEndpoint",
]
