"""
Backfill script: fix meeting minutes that already contain literal LaTeX
arrow notation (e.g. "$\\rightarrow$") from before the LLM prompt/sanitizer fix.

Run inside the api container:
  docker compose exec api python fix_latex_arrows.py
"""

import asyncio

from app.database import AsyncSessionLocal
from app.models.user import User  # noqa: F401 — register model (Meeting.owner relationship)
from app.models.meeting import Meeting  # noqa: F401 — register model
from app.services.llm import _sanitize_latex_arrows
from sqlalchemy import select


async def main() -> None:
    async with AsyncSessionLocal() as db:
        meetings = (await db.execute(select(Meeting).where(Meeting.summary.isnot(None)))).scalars().all()
        fixed = 0
        for meeting in meetings:
            cleaned = _sanitize_latex_arrows(meeting.summary)
            if cleaned != meeting.summary:
                meeting.summary = cleaned
                fixed += 1
        if fixed:
            await db.commit()
        print(f"[OK] 검사한 회의록 {len(meetings)}건 중 {fixed}건을 수정했습니다.")


if __name__ == "__main__":
    asyncio.run(main())
