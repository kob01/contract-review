"""API 路由汇总。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import contracts, health, human_review, knowledge, redlines, reviews

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(contracts.router)
api_router.include_router(reviews.router)
api_router.include_router(human_review.router)
api_router.include_router(redlines.router)
api_router.include_router(knowledge.router)
