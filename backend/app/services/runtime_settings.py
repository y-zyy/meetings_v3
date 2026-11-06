"""Helpers to resolve effective settings: DB overrides take priority over env vars."""

from app.config import settings

# Keys that can be overridden via the admin settings API
OVERRIDABLE_KEYS = [
    "ASR_API_URL",
    "ASR_API_KEY",
    "ASR_FILE_FIELD",
    "ASR_RESPONSE_FIELD",
    "ASR_TIMEOUT",
    "LLM_API_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_TIMEOUT",
    "LLM_MAX_TOKENS",
    "OPENAI_API_KEY",
]


def _env_default(key: str) -> str:
    val = getattr(settings, key, "")
    return str(val) if val is not None else ""


def get_effective_settings_sync(session) -> dict:
    """Read all overridable settings, preferring DB values over env defaults.
    Uses a synchronous SQLAlchemy session (for Celery workers).
    """
    from app.models.setting import SystemSetting

    rows = session.query(SystemSetting).filter(SystemSetting.key.in_(OVERRIDABLE_KEYS)).all()
    db_map = {row.key: row.value for row in rows}

    return {key: db_map.get(key, _env_default(key)) for key in OVERRIDABLE_KEYS}


async def get_effective_settings_async(session) -> dict:
    """Read all overridable settings, preferring DB values over env defaults.
    Uses an async SQLAlchemy session (for FastAPI routes).
    """
    from sqlalchemy import select
    from app.models.setting import SystemSetting

    result = await session.execute(
        select(SystemSetting).where(SystemSetting.key.in_(OVERRIDABLE_KEYS))
    )
    db_map = {row.key: row.value for row in result.scalars().all()}

    return {key: db_map.get(key, _env_default(key)) for key in OVERRIDABLE_KEYS}
