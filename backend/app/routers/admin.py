"""Admin-only endpoints: user management, system stats."""

import os
import shutil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.deps import get_admin_user
from app.core.security import hash_password
from app.database import get_db
from app.models.meeting import Meeting
from app.models.user import User
from app.schemas.user import UserCreate, UserOut, UserUpdate

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ── stats ─────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def get_stats(db: AsyncSession = Depends(get_db), _=Depends(get_admin_user)):
    user_count = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    pending_count = (await db.execute(
        select(func.count()).select_from(User).where(User.is_active == False)  # noqa: E712
    )).scalar_one()
    meeting_count = (await db.execute(select(func.count()).select_from(Meeting))).scalar_one()
    done_count = (await db.execute(select(func.count()).select_from(Meeting).where(Meeting.status == "done"))).scalar_one()
    failed_count = (await db.execute(select(func.count()).select_from(Meeting).where(Meeting.status == "failed"))).scalar_one()
    processing_count = (await db.execute(
        select(func.count()).select_from(Meeting).where(Meeting.status.in_(["queued", "asr_processing", "llm_processing"]))
    )).scalar_one()

    # Disk usage
    upload_dir = settings.UPLOAD_DIR
    total_bytes = 0
    if os.path.exists(upload_dir):
        for dirpath, _, filenames in os.walk(upload_dir):
            for fn in filenames:
                total_bytes += os.path.getsize(os.path.join(dirpath, fn))

    return {
        "user_count": user_count,
        "pending_count": pending_count,
        "meeting_count": meeting_count,
        "done_count": done_count,
        "failed_count": failed_count,
        "processing_count": processing_count,
        "storage_bytes": total_bytes,
    }


# ── users ─────────────────────────────────────────────────────────────────────

@router.get("/users", response_model=dict)
async def list_users(
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _=Depends(get_admin_user),
):
    stmt = select(User)
    if q:
        stmt = stmt.where(User.username.ilike(f"%{q}%") | User.full_name.ilike(f"%{q}%"))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(User.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"total": total, "items": [UserOut.model_validate(u) for u in rows]}


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, db: AsyncSession = Depends(get_db), _=Depends(get_admin_user)):
    existing = (await db.execute(select(User).where(User.username == payload.username))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="이미 존재하는 사용자 이름입니다.")
    user = User(
        username=payload.username,
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    payload: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")

    # Guard: prevent revoking the last active admin's privileges
    demoting = user.role == "admin" and (
        (payload.role is not None and payload.role != "admin")
        or (payload.is_active is not None and not payload.is_active)
    )
    if demoting:
        admin_count = (await db.execute(
            select(func.count()).select_from(User).where(User.role == "admin", User.is_active == True)  # noqa: E712
        )).scalar_one()
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="마지막 관리자 계정의 권한을 변경할 수 없습니다.")

    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(user, field, value)
    await db.commit()
    await db.refresh(user)
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: int, db: AsyncSession = Depends(get_db), current_admin: User = Depends(get_admin_user)):
    if user_id == current_admin.id:
        raise HTTPException(status_code=400, detail="본인 계정은 삭제할 수 없습니다.")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
    if user.role == "admin":
        admin_count = (await db.execute(
            select(func.count()).select_from(User).where(User.role == "admin", User.is_active == True)  # noqa: E712
        )).scalar_one()
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="마지막 관리자 계정은 삭제할 수 없습니다.")
    await db.delete(user)
    await db.commit()


# ── meetings (admin view) ─────────────────────────────────────────────────────

@router.get("/meetings", response_model=dict)
async def list_all_meetings(
    q: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _=Depends(get_admin_user),
):
    stmt = select(Meeting)
    if q:
        stmt = stmt.where(Meeting.title.ilike(f"%{q}%"))
    if status_filter:
        stmt = stmt.where(Meeting.status == status_filter)

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(Meeting.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).scalars().all()

    return {
        "total": total,
        "items": [
            {
                "id": m.id,
                "title": m.title,
                "status": m.status,
                "created_by": m.created_by,
                "created_at": m.created_at.isoformat(),
                "file_size": m.file_size,
            }
            for m in rows
        ],
    }
