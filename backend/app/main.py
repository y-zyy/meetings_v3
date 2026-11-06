import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import AsyncSessionLocal, engine
from app.routers import auth, meetings, admin
from app.routers import settings_admin
from app.routers import board
from app.routers import glossary

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="KAI Meetings API", version="1.0.0", docs_url="/api/docs", redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(meetings.router)
app.include_router(admin.router)
app.include_router(settings_admin.router)
app.include_router(board.router)
app.include_router(glossary.router)


@app.on_event("startup")
async def on_startup():
    # Ensure upload directory exists
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)

    # Create tables & seed admin user
    from app.database import Base
    from app.models import user, meeting, setting, board, glossary  # register models

    async with engine.begin() as conn:
        # uvicorn --workers N 이 동시에 기동하면 빈 DB 에서 create_all 이 서로 충돌(UniqueViolation)해
        # 기동이 실패한다. 트랜잭션 단위 advisory lock 으로 한 프로세스씩 실행되게 한다.
        from sqlalchemy import text

        await conn.execute(text("SELECT pg_advisory_xact_lock(72710001)"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_diarization_columns)

    await _seed_admin()


def _ensure_diarization_columns(sync_conn):
    """create_all은 기존 테이블에 컬럼을 추가하지 않으므로 화자 분리 컬럼을 보강한다 (alembic 005와 동일)."""
    from sqlalchemy import inspect, text

    existing = {c["name"] for c in inspect(sync_conn).get_columns("meetings")}
    for col in ("segments", "speaker_names"):
        if col not in existing:
            sync_conn.execute(text(f"ALTER TABLE meetings ADD COLUMN {col} JSON"))


async def _seed_admin():
    from sqlalchemy import select
    from app.models.user import User
    from app.core.security import hash_password

    log = logging.getLogger(__name__)
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(User).where(User.username == settings.ADMIN_USERNAME))).scalar_one_or_none()
        if not existing:
            admin = User(
                username=settings.ADMIN_USERNAME,
                email=settings.ADMIN_EMAIL,
                full_name="관리자",
                hashed_password=hash_password(settings.ADMIN_PASSWORD),
                role="admin",
            )
            db.add(admin)
            await db.commit()
            log.info("Admin user '%s' created.", settings.ADMIN_USERNAME)
        else:
            # Restore admin account if it was deactivated or its role was changed
            changed = False
            if not existing.is_active:
                existing.is_active = True
                changed = True
                log.warning("Admin user '%s' was inactive — re-activating.", settings.ADMIN_USERNAME)
            if existing.role != "admin":
                existing.role = "admin"
                changed = True
                log.warning("Admin user '%s' had wrong role — restoring to 'admin'.", settings.ADMIN_USERNAME)
            if changed:
                await db.commit()


@app.get("/api/health")
async def health():
    return {"status": "ok"}
