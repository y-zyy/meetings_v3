from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AdminGlossaryTerm(Base):
    """전역 용어사전 (관리자만 관리, 모든 사용자 STT 후처리에 적용)"""
    __tablename__ = "admin_glossary_terms"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    term: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    author: Mapped["User"] = relationship("User")  # noqa: F821


class AdminCorrectionRule(Base):
    """음성인식 교정 규칙 (관리자 관리, Rule-based 후처리에 적용)"""
    __tablename__ = "admin_correction_rules"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    wrong: Mapped[str] = mapped_column(String(255), nullable=False)
    correct: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    author: Mapped["User"] = relationship("User")  # noqa: F821


class UserGlossaryTerm(Base):
    """사용자 개인 용어사전 (사용자가 직접 관리, 본인 STT 후처리에 적용)"""
    __tablename__ = "user_glossary_terms"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    term: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    user: Mapped["User"] = relationship("User")  # noqa: F821
