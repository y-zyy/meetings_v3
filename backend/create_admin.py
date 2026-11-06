"""
Admin recovery script.
Run inside the api container:
  docker compose exec api python create_admin.py [username] [password] [email]
"""

import asyncio
import sys

from app.config import settings
from app.database import AsyncSessionLocal
from app.core.security import hash_password
from app.models.user import User  # noqa: F401 — register model
from sqlalchemy import select


async def main(username: str, password: str, email: str) -> None:
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if existing:
            existing.hashed_password = hash_password(password)
            existing.role = "admin"
            existing.is_active = True
            await db.commit()
            print(f"[OK] '{username}' 계정을 admin으로 복구했습니다.")
        else:
            user = User(
                username=username,
                email=email,
                full_name="관리자",
                hashed_password=hash_password(password),
                role="admin",
            )
            db.add(user)
            await db.commit()
            print(f"[OK] '{username}' admin 계정을 새로 생성했습니다.")


if __name__ == "__main__":
    username = sys.argv[1] if len(sys.argv) > 1 else settings.ADMIN_USERNAME
    password = sys.argv[2] if len(sys.argv) > 2 else settings.ADMIN_PASSWORD
    email    = sys.argv[3] if len(sys.argv) > 3 else settings.ADMIN_EMAIL
    asyncio.run(main(username, password, email))
