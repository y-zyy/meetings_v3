"""Admin endpoint for reading and updating system settings (ASR / LLM endpoints)."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_admin_user
from app.database import get_db
from app.services.runtime_settings import OVERRIDABLE_KEYS, get_effective_settings_async

router = APIRouter(prefix="/api/admin/settings", tags=["admin-settings"])


class SettingsPatch(BaseModel):
    ASR_API_URL: str | None = None
    ASR_API_KEY: str | None = None
    ASR_FILE_FIELD: str | None = None
    ASR_RESPONSE_FIELD: str | None = None
    ASR_TIMEOUT: str | None = None
    LLM_API_BASE_URL: str | None = None
    LLM_API_KEY: str | None = None
    LLM_MODEL: str | None = None
    LLM_TIMEOUT: str | None = None
    LLM_MAX_TOKENS: str | None = None
    OPENAI_API_KEY: str | None = None


@router.get("")
async def get_settings(
    db: AsyncSession = Depends(get_db),
    _=Depends(get_admin_user),
):
    effective = await get_effective_settings_async(db)
    # Mask key values — show only whether they are set
    masked = dict(effective)
    for key in ("OPENAI_API_KEY", "ASR_API_KEY", "LLM_API_KEY"):
        if masked.get(key):
            masked[key] = "********"
    return masked


@router.patch("")
async def update_settings(
    payload: SettingsPatch,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_admin_user),
):
    from sqlalchemy import select
    from app.models.setting import SystemSetting

    updates = payload.model_dump(exclude_none=True)
    for key, value in updates.items():
        if key not in OVERRIDABLE_KEYS:
            continue
        result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
        row = result.scalar_one_or_none()
        if row is None:
            db.add(SystemSetting(key=key, value=value))
        else:
            row.value = value

    await db.commit()

    effective = await get_effective_settings_async(db)
    masked = dict(effective)
    for key in ("OPENAI_API_KEY", "ASR_API_KEY", "LLM_API_KEY"):
        if masked.get(key):
            masked[key] = "********"
    return masked
