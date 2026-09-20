from fastapi import APIRouter

from app.api.v1 import health

api_router = APIRouter()
api_router.include_router(health.router)

# Versioned product endpoints (S9) mount here under /api/v1.
v1_router = APIRouter(prefix="/api/v1")
