"""
Auth Utils
JWT Token handling and password hashing
"""
from datetime import datetime, timedelta
from typing import Optional
from jose import jwt, JWTError
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
import logging

from app.config import settings
from app.db.database import get_db
from app.db.models_db import User

# Logging
logger = logging.getLogger(__name__)

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# OAuth2 scheme - auto_error=False allows X-User-ID header fallback
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    username: Optional[str] = None
    user_id: Optional[int] = None

class UserDTO(BaseModel):
    id: int
    username: str
    nickname: Optional[str] = None
    
    class Config:
        from_attributes = True

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.access_token_expire_minutes)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return encoded_jwt

async def get_current_user(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db)
):
    """
    获取当前用户 - 简化认证模式（无用户验证系统）

    只需通过 X-User-ID header 或默认使用 user_id=1
    """
    # 1. 从 X-User-ID header 获取用户 ID
    user_id_str = request.headers.get("x-user-id") or "1"

    try:
        uid = int(user_id_str)
    except ValueError:
        uid = 1

    # 2. 查找或创建用户
    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalars().first()

    if not user:
        # 自动创建用户
        username = request.headers.get("x-user-name") or f"user_{uid}"
        logger.info(f"[Auth] Auto-creating user: id={uid}, username={username}")
        user = User(
            id=uid,
            username=username,
            password_hash="no_auth",
            nickname=username
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    return user
