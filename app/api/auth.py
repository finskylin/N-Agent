"""
Auth API
Minimal User Authentication - 简化版登录
"""
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from app.db.database import get_db
from app.db.models_db import User
from app.utils.auth_utils import (
    Token, UserDTO, get_password_hash, verify_password,
    create_access_token, get_current_user
)
from app.config import settings
from loguru import logger

router = APIRouter(prefix="/auth", tags=["Auth"])

class UserCreate(BaseModel):
    username: str
    password: str
    nickname: str = None

class LoginRequest(BaseModel):
    """JSON 格式登录请求"""
    username: str
    password: str

@router.post("/register", response_model=UserDTO)
async def register(user: UserCreate, db: AsyncSession = Depends(get_db)):
    # Check existing
    result = await db.execute(select(User).where(User.username == user.username))
    if result.scalars().first():
        raise HTTPException(
            status_code=400,
            detail="Username already registered"
        )

    # Create user
    hashed_pw = get_password_hash(user.password)
    db_user = User(
        username=user.username,
        password_hash=hashed_pw,
        nickname=user.nickname or user.username
    )
    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)
    logger.info(f"User registered: {db_user.username}")
    return db_user

@router.post("/login", response_model=Token)
async def login(login_req: LoginRequest, db: AsyncSession = Depends(get_db)):
    """
    简化登录接口 - 支持 JSON 格式

    请求格式:
    {
        "username": "kyj",
        "password": "123"
    }
    """
    # Find user
    result = await db.execute(select(User).where(User.username == login_req.username))
    user = result.scalars().first()

    # Verify password
    if not user or not verify_password(login_req.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Create token
    access_token = create_access_token(
        data={"sub": user.username, "id": user.id}
    )
    logger.info(f"User logged in: {user.username}")
    return {"access_token": access_token, "token_type": "bearer"}

@router.get("/me", response_model=UserDTO)
async def read_users_me(current_user: User = Depends(get_current_user)):
    return current_user


async def get_current_user_id(request: "Request" = None) -> str:
    """
    获取当前用户 ID（简化版，不依赖数据库）

    从 X-User-ID header 获取，默认返回 "1"

    Returns:
        用户 ID 字符串
    """
    from fastapi import Request
    if request is None:
        return "1"

    user_id = request.headers.get("x-user-id", "1")
    return str(user_id)


# 重新定义支持 Request 注入的版本
from fastapi import Request as FastAPIRequest

async def get_current_user_id_from_request(request: FastAPIRequest) -> str:
    """
    从 Request 获取用户 ID（用于依赖注入）
    """
    return request.headers.get("x-user-id", "1")
