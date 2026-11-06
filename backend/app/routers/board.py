from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.deps import get_current_user, get_admin_user
from app.database import get_db
from app.models.board import Notice, QnAPost, QnAReply
from app.models.user import User

router = APIRouter(prefix="/api/board", tags=["board"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class NoticeCreate(BaseModel):
    title: str
    content: str
    is_pinned: bool = False

class NoticeOut(BaseModel):
    id: int
    title: str
    content: str
    is_pinned: bool
    author_name: str
    created_at: datetime

class QnAPostCreate(BaseModel):
    title: str
    content: str
    is_secret: bool = False

class QnAReplyCreate(BaseModel):
    content: str

class QnAReplyOut(BaseModel):
    id: int
    content: str
    author_name: str
    created_at: datetime

class QnAPostOut(BaseModel):
    id: int
    title: str
    content: Optional[str]
    is_secret: bool
    author_name: str
    created_at: datetime
    reply_count: int
    has_reply: bool
    is_mine: bool
    replies: list[QnAReplyOut] = []

class QnAListItem(BaseModel):
    id: int
    title: str
    is_secret: bool
    author_name: str
    created_at: datetime
    reply_count: int
    has_reply: bool
    is_mine: bool


# ── Notices ───────────────────────────────────────────────────────────────────

@router.get("/notices", response_model=list[NoticeOut])
async def list_notices(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    result = await db.execute(
        select(Notice).options(selectinload(Notice.author))
        .order_by(desc(Notice.is_pinned), desc(Notice.created_at))
    )
    notices = result.scalars().all()
    return [
        NoticeOut(
            id=n.id, title=n.title, content=n.content,
            is_pinned=n.is_pinned, author_name=n.author.full_name or n.author.username,
            created_at=n.created_at,
        )
        for n in notices
    ]


@router.post("/notices", response_model=NoticeOut, status_code=status.HTTP_201_CREATED)
async def create_notice(
    body: NoticeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    notice = Notice(title=body.title, content=body.content, is_pinned=body.is_pinned, author_id=current_user.id)
    db.add(notice)
    await db.commit()
    await db.refresh(notice)
    await db.refresh(notice, ["author"])
    return NoticeOut(
        id=notice.id, title=notice.title, content=notice.content,
        is_pinned=notice.is_pinned, author_name=notice.author.full_name or notice.author.username,
        created_at=notice.created_at,
    )


@router.delete("/notices/{notice_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notice(notice_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(get_admin_user)):
    notice = await db.get(Notice, notice_id)
    if not notice:
        raise HTTPException(status_code=404, detail="Not found")
    await db.delete(notice)
    await db.commit()


# ── Q&A ───────────────────────────────────────────────────────────────────────

@router.get("/qna", response_model=list[QnAListItem])
async def list_qna(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    result = await db.execute(
        select(QnAPost).options(selectinload(QnAPost.author), selectinload(QnAPost.replies))
        .order_by(desc(QnAPost.created_at))
    )
    posts = result.scalars().all()
    items = []
    for p in posts:
        is_mine = p.author_id == current_user.id
        items.append(QnAListItem(
            id=p.id,
            title=p.title if (not p.is_secret or is_mine or current_user.role == "admin") else "🔒 비밀글입니다",
            is_secret=p.is_secret,
            author_name=p.author.full_name or p.author.username,
            created_at=p.created_at,
            reply_count=len(p.replies),
            has_reply=len(p.replies) > 0,
            is_mine=is_mine,
        ))
    return items


@router.post("/qna", response_model=QnAPostOut, status_code=status.HTTP_201_CREATED)
async def create_qna(
    body: QnAPostCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    post = QnAPost(title=body.title, content=body.content, is_secret=body.is_secret, author_id=current_user.id)
    db.add(post)
    await db.commit()
    await db.refresh(post)
    await db.refresh(post, ["author"])
    return QnAPostOut(
        id=post.id, title=post.title, content=post.content,
        is_secret=post.is_secret, author_name=post.author.full_name or post.author.username,
        created_at=post.created_at, reply_count=0, has_reply=False, is_mine=True, replies=[],
    )


@router.get("/qna/{post_id}", response_model=QnAPostOut)
async def get_qna(post_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    result = await db.execute(
        select(QnAPost).options(selectinload(QnAPost.author), selectinload(QnAPost.replies).selectinload(QnAReply.author))
        .where(QnAPost.id == post_id)
    )
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Not found")

    is_mine = post.author_id == current_user.id
    can_view = not post.is_secret or is_mine or current_user.role == "admin"
    if not can_view:
        raise HTTPException(status_code=403, detail="비밀글입니다")

    replies = [
        QnAReplyOut(id=r.id, content=r.content, author_name=r.author.full_name or r.author.username, created_at=r.created_at)
        for r in post.replies
    ]
    return QnAPostOut(
        id=post.id, title=post.title, content=post.content,
        is_secret=post.is_secret, author_name=post.author.full_name or post.author.username,
        created_at=post.created_at, reply_count=len(replies), has_reply=len(replies) > 0,
        is_mine=is_mine, replies=replies,
    )


@router.delete("/qna/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_qna(post_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    post = await db.get(QnAPost, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Not found")
    if post.author_id != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="권한이 없습니다")
    await db.delete(post)
    await db.commit()


@router.post("/qna/{post_id}/reply", response_model=QnAReplyOut, status_code=status.HTTP_201_CREATED)
async def create_reply(
    post_id: int,
    body: QnAReplyCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    post = await db.get(QnAPost, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Not found")
    reply = QnAReply(post_id=post_id, content=body.content, author_id=current_user.id)
    db.add(reply)
    await db.commit()
    await db.refresh(reply)
    await db.refresh(reply, ["author"])
    return QnAReplyOut(id=reply.id, content=reply.content, author_name=reply.author.full_name or reply.author.username, created_at=reply.created_at)


@router.delete("/qna/{post_id}/reply/{reply_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_reply(post_id: int, reply_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(get_admin_user)):
    reply = await db.get(QnAReply, reply_id)
    if not reply or reply.post_id != post_id:
        raise HTTPException(status_code=404, detail="Not found")
    await db.delete(reply)
    await db.commit()
