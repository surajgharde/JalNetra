from fastapi import APIRouter

from app.api.v1 import alerts, health

api_router = APIRouter()
api_router.include_router(health.router)

# Versioned product endpoints mount here under /api/v1 (S8: alerts; S9: the rest).
v1_router = APIRouter(prefix="/api/v1")
v1_router.include_router(alerts.router)
