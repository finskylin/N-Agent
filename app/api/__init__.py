"""
API Routes Package
"""
from fastapi import APIRouter
from .chat_v4 import router as chat_v4_router
from .stock import router as stock_router
from .feedback import router as feedback_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(chat_v4_router)
api_router.include_router(stock_router)
api_router.include_router(feedback_router)
