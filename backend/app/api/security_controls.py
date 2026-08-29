"""Owner-visible status for privileged data-extraction capabilities."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.security import CurrentOwner, SettingsDependency

router = APIRouter(prefix="/admin/security", tags=["security"])


class ExportPolicyRead(BaseModel):
    enabled: bool
    allowed_role: Literal["owner"] = "owner"


@router.get("/export-policy", response_model=ExportPolicyRead)
async def export_policy_status(
    context: CurrentOwner,
    settings: SettingsDependency,
) -> ExportPolicyRead:
    """Return the effective server policy without enabling an export."""

    del context
    return ExportPolicyRead(enabled=settings.crm_export_enabled)
