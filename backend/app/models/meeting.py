from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    meeting_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    location: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    attendees: Mapped[str] = mapped_column(Text, nullable=False, default="")
    agenda: Mapped[str] = mapped_column(Text, nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="ko")

    # Processing state: queued | asr_processing | llm_processing | done | failed
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # File
    file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # AI results
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 화자 분리 발화 기록: [{"start": float, "end": float, "speaker": str, "text": str}]
    segments: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 화자 라벨(SPEAKER_00) -> 표시 이름 매핑
    speaker_names: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Ownership
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    owner: Mapped["User"] = relationship("User", back_populates="meetings")  # noqa: F821
    decisions: Mapped[list["Decision"]] = relationship(
        "Decision", back_populates="meeting", cascade="all, delete-orphan", order_by="Decision.order"
    )
    action_items: Mapped[list["ActionItem"]] = relationship(
        "ActionItem", back_populates="meeting", cascade="all, delete-orphan", order_by="ActionItem.order"
    )


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    meeting: Mapped["Meeting"] = relationship("Meeting", back_populates="decisions")


class ActionItem(Base):
    __tablename__ = "action_items"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    assignee: Mapped[str | None] = mapped_column(String(128), nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending | done
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    meeting: Mapped["Meeting"] = relationship("Meeting", back_populates="action_items")
