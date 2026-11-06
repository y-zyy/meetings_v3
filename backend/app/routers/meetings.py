"""Meeting CRUD + file upload + status polling + export."""

import asyncio
import os
import re
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.core.deps import get_current_user
from app.database import get_db
from app.models.meeting import ActionItem, Decision, Meeting
from app.models.user import User
from app.schemas.meeting import (
    ActionItemOut,
    ActionItemUpdate,
    MeetingCreate,
    MeetingDetail,
    MeetingListItem,
    MeetingStatus,
    MeetingUpdate,
)

from app.services.speaker import format_segments_text  # noqa: E402

router = APIRouter(prefix="/api/meetings", tags=["meetings"])

_MAX_BYTES = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
_ALLOWED_EXTS = {".mp3", ".wav", ".m4a", ".webm", ".mp4", ".ogg", ".flac"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _check_owner(meeting: Meeting, user: User):
    if meeting.created_by != user.id and user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="접근 권한이 없습니다.")


async def _load_meeting(meeting_id: int, db: AsyncSession, *, with_relations=False) -> Meeting:
    if with_relations:
        stmt = (
            select(Meeting)
            .where(Meeting.id == meeting_id)
            .options(selectinload(Meeting.decisions), selectinload(Meeting.action_items))
        )
    else:
        stmt = select(Meeting).where(Meeting.id == meeting_id)
    result = await db.execute(stmt)
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="회의를 찾을 수 없습니다.")
    return meeting


# ── upload ────────────────────────────────────────────────────────────────────

@router.post("", status_code=status.HTTP_201_CREATED, response_model=MeetingStatus)
async def upload_meeting(
    file: UploadFile,
    title: str = Form(...),
    meeting_date: str | None = Form(None),
    location: str = Form(""),
    attendees: str = Form(""),
    agenda: str = Form(""),
    notes: str = Form(""),
    language: str = Form("ko"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Validate extension
    _, ext = os.path.splitext(file.filename or "")
    if ext.lower() not in _ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 파일 형식입니다. ({ext})")

    # Stream the upload to disk without retaining the whole file in memory.
    uid = uuid.uuid4().hex
    safe_title = re.sub(r'[\\/:*?"<>|]', '_', title).strip() or "untitled"
    dest_dir = os.path.join(settings.UPLOAD_DIR, current_user.username, safe_title)
    os.makedirs(dest_dir, exist_ok=True)

    file_path = os.path.join(dest_dir, f"{uid}{ext.lower()}")
    chunk_size = settings.UPLOAD_CHUNK_SIZE_MB * 1024 * 1024
    total_size = 0
    try:
        with open(file_path, "wb") as destination:
            while chunk := await file.read(chunk_size):
                total_size += len(chunk)
                if total_size > _MAX_BYTES:
                    raise HTTPException(
                        status_code=400,
                        detail=f"파일 크기가 {settings.MAX_UPLOAD_SIZE_MB}MB를 초과합니다.",
                    )
                # 동기 디스크 쓰기가 이벤트 루프를 막지 않도록 스레드에서 수행
                await asyncio.to_thread(destination.write, chunk)
    except Exception:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise
    finally:
        await file.close()

    # Parse date
    from datetime import date as DateType
    parsed_date: DateType | None = None
    if meeting_date:
        try:
            parsed_date = DateType.fromisoformat(meeting_date)
        except ValueError:
            pass

    # Create DB record
    meeting = Meeting(
        title=title,
        meeting_date=parsed_date,
        location=location,
        attendees=attendees,
        agenda=agenda,
        notes=notes,
        language=language,
        status="queued",
        file_path=file_path,
        file_name=file.filename,
        file_size=total_size,
        created_by=current_user.id,
    )
    db.add(meeting)
    await db.commit()
    await db.refresh(meeting)

    # Dispatch Celery task
    from app.workers.tasks import process_meeting
    process_meeting.delay(meeting.id)

    return meeting


# ── list ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=dict)
async def list_meetings(
    q: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    stmt = select(Meeting).where(Meeting.created_by == current_user.id)

    if q:
        stmt = stmt.where(Meeting.title.ilike(f"%{q}%"))
    if status_filter:
        stmt = stmt.where(Meeting.status == status_filter)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.order_by(Meeting.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()

    items = []
    for m in rows:
        items.append({
            "id": m.id,
            "title": m.title,
            "meeting_date": m.meeting_date.isoformat() if m.meeting_date else None,
            "location": m.location,
            "attendees": m.attendees,
            "status": m.status,
            "duration_seconds": m.duration_seconds,
            "file_name": m.file_name,
            "created_at": m.created_at.isoformat(),
        })

    return {"total": total, "page": page, "page_size": page_size, "items": items}


# ── detail ────────────────────────────────────────────────────────────────────

@router.get("/{meeting_id}", response_model=MeetingDetail)
async def get_meeting(
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    meeting = await _load_meeting(meeting_id, db, with_relations=True)
    _check_owner(meeting, current_user)
    return meeting


# ── status polling ────────────────────────────────────────────────────────────

@router.get("/{meeting_id}/status", response_model=MeetingStatus)
async def get_status(
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    meeting = await _load_meeting(meeting_id, db)
    _check_owner(meeting, current_user)
    return meeting


# ── update ────────────────────────────────────────────────────────────────────

@router.patch("/{meeting_id}", response_model=MeetingDetail)
async def update_meeting(
    meeting_id: int,
    payload: MeetingUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    meeting = await _load_meeting(meeting_id, db, with_relations=True)
    _check_owner(meeting, current_user)

    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(meeting, field, value)
    if payload.speaker_names is not None and meeting.segments:
        # 화자 이름이 바뀌면 요약/내보내기에 쓰이는 transcript 텍스트도 함께 갱신
        meeting.transcript = format_segments_text(meeting.segments, meeting.speaker_names)
    await db.commit()
    await db.refresh(meeting)
    return meeting


# ── decisions ─────────────────────────────────────────────────────────────────

@router.put("/{meeting_id}/decisions", response_model=list[dict])
async def replace_decisions(
    meeting_id: int,
    items: list[str],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    meeting = await _load_meeting(meeting_id, db)
    _check_owner(meeting, current_user)

    await db.execute(Decision.__table__.delete().where(Decision.meeting_id == meeting_id))
    new = [Decision(meeting_id=meeting_id, content=c, order=i) for i, c in enumerate(items)]
    db.add_all(new)
    await db.commit()
    return [{"id": d.id, "content": d.content, "order": d.order} for d in new]


# ── action items ──────────────────────────────────────────────────────────────

@router.patch("/{meeting_id}/action-items/{item_id}", response_model=ActionItemOut)
async def update_action_item(
    meeting_id: int,
    item_id: int,
    payload: ActionItemUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    meeting = await _load_meeting(meeting_id, db)
    _check_owner(meeting, current_user)

    result = await db.execute(select(ActionItem).where(ActionItem.id == item_id, ActionItem.meeting_id == meeting_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="액션 아이템을 찾을 수 없습니다.")

    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(item, field, value)
    await db.commit()
    await db.refresh(item)
    return item


# ── delete ────────────────────────────────────────────────────────────────────

@router.delete("/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_meeting(
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    meeting = await _load_meeting(meeting_id, db)
    _check_owner(meeting, current_user)

    # Remove audio file from disk
    if meeting.file_path and os.path.exists(meeting.file_path):
        os.remove(meeting.file_path)

    await db.delete(meeting)
    await db.commit()


# ── export ────────────────────────────────────────────────────────────────────

@router.get("/{meeting_id}/export")
async def export_meeting(
    meeting_id: int,
    fmt: str = Query("docx", pattern="^(docx|pdf|txt)$"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    meeting = await _load_meeting(meeting_id, db, with_relations=True)
    _check_owner(meeting, current_user)

    from app.services import export as export_svc

    from urllib.parse import quote
    safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in meeting.title)[:50]

    if fmt == "docx":
        data = export_svc.build_docx(meeting)
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        filename = f"{safe_title}.docx"
    elif fmt == "pdf":
        data = export_svc.build_pdf(meeting)
        media = "application/pdf"
        filename = f"{safe_title}.pdf"
    else:
        data = export_svc.build_txt(meeting)
        media = "text/plain; charset=utf-8"
        filename = f"{safe_title}.txt"

    encoded_filename = quote(filename, safe="")
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
    )

