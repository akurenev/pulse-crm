from fastapi import APIRouter

from app.api import auth, crm, deal_exports, events, push, security_controls
from app.integrations import (
    admin_api,
    amocrm_api,
    attachments_api,
    consents_api,
    operations_api,
    subscription_import_api,
)
from app.integrations import api as integrations_api

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(auth.users_router)
api_router.include_router(deal_exports.router)
api_router.include_router(crm.router)
api_router.include_router(events.router)
api_router.include_router(push.router)
api_router.include_router(security_controls.router)
api_router.include_router(integrations_api.router)
api_router.include_router(attachments_api.router)
api_router.include_router(consents_api.router)
api_router.include_router(admin_api.router)
api_router.include_router(amocrm_api.router)
api_router.include_router(operations_api.router)
api_router.include_router(subscription_import_api.router)
