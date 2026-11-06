from sqlalchemy import Column, String

from app.database import Base


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key = Column(String(64), primary_key=True)
    value = Column(String(1024), nullable=False, default="")
