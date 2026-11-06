from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.deps import get_current_user
from app.core.security import create_access_token, create_refresh_token, decode_token, hash_password, verify_password
from app.database import get_db
from app.models.user import User
from app.schemas.user import Token, UserCreate, UserOut, UserRegister

router = APIRouter(prefix="/api/auth", tags=["auth"])

REFRESH_COOKIE = "kai_refresh"


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        path="/api/auth",
    )


@router.post("/login", response_model=Token)
async def login(
    response: Response,
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.username == form.username, User.is_active == True))  # noqa: E712
    user = result.scalar_one_or_none()
    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="아이디 또는 비밀번호가 올바르지 않습니다.")

    user.last_login = datetime.now(timezone.utc)
    await db.commit()

    _set_refresh_cookie(response, create_refresh_token(user.id))
    return Token(access_token=create_access_token(user.id))


@router.post("/refresh", response_model=Token)
async def refresh(
    response: Response,
    kai_refresh: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="세션이 만료되었습니다. 다시 로그인해주세요.",
    )
    if not kai_refresh:
        raise credentials_exception

    user_id = decode_token(kai_refresh, token_type="refresh")
    if user_id is None:
        raise credentials_exception

    result = await db.execute(select(User).where(User.id == user_id, User.is_active == True))  # noqa: E712
    user = result.scalar_one_or_none()
    if user is None:
        raise credentials_exception

    _set_refresh_cookie(response, create_refresh_token(user.id))
    return Token(access_token=create_access_token(user.id))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response):
    response.delete_cookie(key=REFRESH_COOKIE, path="/api/auth")



@router.get("/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user)):
    return current_user


@router.get("/check-username")
async def check_username(username: str, db: AsyncSession = Depends(get_db)):
    """아이디(이메일 prefix) 중복 확인 - 공개 엔드포인트"""
    existing = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    return {"available": existing is None}


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(payload: UserRegister, db: AsyncSession = Depends(get_db)):
    """회원가입 - 공개 엔드포인트. 생성된 계정은 관리자 승인 후 활성화됩니다."""
    if payload.password != payload.password_confirm:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="비밀번호가 일치하지 않습니다.")
    if len(payload.password) < 6:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="비밀번호는 6자 이상이어야 합니다.")

    username = payload.username.strip().lower()
    if not username:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="아이디를 입력해주세요.")

    email = f"{username}@mygroup.com"

    dup = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if dup:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이미 사용 중인 아이디입니다.")

    user = User(
        username=username,
        email=email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role="user",
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    current_password: str,
    new_password: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(current_password, current_user.hashed_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="현재 비밀번호가 올바르지 않습니다.")
    current_user.hashed_password = hash_password(new_password)
    await db.commit()
