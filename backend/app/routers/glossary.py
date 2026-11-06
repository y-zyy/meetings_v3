"""Glossary endpoints: admin global terms + user personal terms."""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_admin_user, get_current_user
from app.database import get_db
from app.models.glossary import AdminCorrectionRule, AdminGlossaryTerm, UserGlossaryTerm
from app.models.user import User

router = APIRouter(prefix="/api/glossary", tags=["glossary"])


class TermCreate(BaseModel):
    term: str
    description: str = ""


class TermOut(BaseModel):
    id: int
    term: str
    description: str

    model_config = {"from_attributes": True}


class CorrectionRuleCreate(BaseModel):
    wrong: str
    correct: str
    description: str = ""


class CorrectionRuleUpdate(BaseModel):
    wrong: str | None = None
    correct: str | None = None
    description: str | None = None


class CorrectionRuleOut(BaseModel):
    id: int
    wrong: str
    correct: str
    description: str

    model_config = {"from_attributes": True}


# ── Admin global glossary ─────────────────────────────────────────────────────

@router.get("/admin", response_model=dict)
async def list_admin_terms(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    rows = (await db.execute(
        select(AdminGlossaryTerm).order_by(AdminGlossaryTerm.created_at)
    )).scalars().all()
    total = len(rows)
    return {
        "total": total,
        "items": [TermOut.model_validate(r) for r in rows],
    }


@router.post("/admin", response_model=TermOut, status_code=status.HTTP_201_CREATED)
async def create_admin_term(
    payload: TermCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    term = payload.term.strip()
    if not term:
        raise HTTPException(status_code=400, detail="용어를 입력해주세요.")
    obj = AdminGlossaryTerm(term=term, description=payload.description.strip(), created_by=admin.id)
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


@router.delete("/admin/{term_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_admin_term(
    term_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    obj = (await db.execute(select(AdminGlossaryTerm).where(AdminGlossaryTerm.id == term_id))).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="용어를 찾을 수 없습니다.")
    await db.delete(obj)
    await db.commit()


# ── Admin correction rules (Rule-based post-processing) ──────────────────────

@router.get("/admin/rules", response_model=dict)
async def list_correction_rules(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    rows = (await db.execute(
        select(AdminCorrectionRule).order_by(AdminCorrectionRule.created_at)
    )).scalars().all()
    return {
        "total": len(rows),
        "items": [CorrectionRuleOut.model_validate(r) for r in rows],
    }


@router.post("/admin/rules", response_model=CorrectionRuleOut, status_code=status.HTTP_201_CREATED)
async def create_correction_rule(
    payload: CorrectionRuleCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    wrong = payload.wrong.strip()
    correct = payload.correct.strip()
    if not wrong or not correct:
        raise HTTPException(status_code=400, detail="오인식 표현과 올바른 표현을 모두 입력해주세요.")
    obj = AdminCorrectionRule(
        wrong=wrong,
        correct=correct,
        description=payload.description.strip(),
        created_by=admin.id,
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


@router.put("/admin/rules/{rule_id}", response_model=CorrectionRuleOut)
async def update_correction_rule(
    rule_id: int,
    payload: CorrectionRuleUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    obj = (await db.execute(
        select(AdminCorrectionRule).where(AdminCorrectionRule.id == rule_id)
    )).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="교정 규칙을 찾을 수 없습니다.")
    if payload.wrong is not None:
        obj.wrong = payload.wrong.strip()
    if payload.correct is not None:
        obj.correct = payload.correct.strip()
    if payload.description is not None:
        obj.description = payload.description.strip()
    await db.commit()
    await db.refresh(obj)
    return obj


@router.delete("/admin/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_correction_rule(
    rule_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    obj = (await db.execute(
        select(AdminCorrectionRule).where(AdminCorrectionRule.id == rule_id)
    )).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="교정 규칙을 찾을 수 없습니다.")
    await db.delete(obj)
    await db.commit()


# ── User personal glossary ────────────────────────────────────────────────────

@router.get("/user", response_model=dict)
async def list_user_terms(
    db: AsyncSession = Depends(get_db),
    me: User = Depends(get_current_user),
):
    rows = (await db.execute(
        select(UserGlossaryTerm)
        .where(UserGlossaryTerm.user_id == me.id)
        .order_by(UserGlossaryTerm.order, UserGlossaryTerm.created_at)
    )).scalars().all()
    return {
        "total": len(rows),
        "items": [TermOut.model_validate(r) for r in rows],
    }


@router.post("/user", response_model=TermOut, status_code=status.HTTP_201_CREATED)
async def create_user_term(
    payload: TermCreate,
    db: AsyncSession = Depends(get_db),
    me: User = Depends(get_current_user),
):
    term = payload.term.strip()
    if not term:
        raise HTTPException(status_code=400, detail="용어를 입력해주세요.")
    max_order = (await db.execute(
        select(func.coalesce(func.max(UserGlossaryTerm.order), -1))
        .where(UserGlossaryTerm.user_id == me.id)
    )).scalar_one()
    obj = UserGlossaryTerm(
        user_id=me.id,
        term=term,
        description=payload.description.strip(),
        order=max_order + 1,
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


@router.delete("/user/{term_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_term(
    term_id: int,
    db: AsyncSession = Depends(get_db),
    me: User = Depends(get_current_user),
):
    obj = (await db.execute(
        select(UserGlossaryTerm)
        .where(UserGlossaryTerm.id == term_id, UserGlossaryTerm.user_id == me.id)
    )).scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="용어를 찾을 수 없습니다.")
    await db.delete(obj)
    await db.commit()
