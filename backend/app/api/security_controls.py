"""Export policy status for roles permitted to download CRM data."""

from fastapi import APIRouter
from pydantic import BaseModel

from app.models import Role
from app.security import CRM_EXPORT_ROLES, CurrentCRMExportUser, SettingsDependency

router = APIRouter(prefix="/admin/security", tags=["security"])


class ExportPolicyRead(BaseModel):
    enabled: bool
    allowed_roles: tuple[Role, ...] = CRM_EXPORT_ROLES


@router.get("/export-policy", response_model=ExportPolicyRead)
async def export_policy_status(
    context: CurrentCRMExportUser,
    settings: SettingsDependency,
) -> ExportPolicyRead:
    """Return the effective server policy without enabling an export."""

    del context
    return ExportPolicyRead(enabled=settings.crm_export_enabled)
