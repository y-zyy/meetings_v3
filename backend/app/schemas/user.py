from datetime import datetime

from pydantic import BaseModel, EmailStr


class UserCreate(BaseModel):
    username: str
    email: EmailStr
    full_name: str = ""
    password: str
    role: str = "user"


class UserRegister(BaseModel):
    username: str          # 이메일 prefix (예: hong → hong@mygroup.com)
    full_name: str
    password: str
    password_confirm: str


class UserUpdate(BaseModel):
    full_name: str | None = None
    email: EmailStr | None = None
    is_active: bool | None = None
    role: str | None = None


class UserOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    username: str
    email: str
    full_name: str
    role: str
    is_active: bool
    created_at: datetime
    last_login: datetime | None = None


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    user_id: int | None = None
